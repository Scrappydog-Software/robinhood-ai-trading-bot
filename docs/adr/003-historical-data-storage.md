# ADR-003: Historical Stock Data Storage for Backtesting

**Status:** Proposed
**Date:** 2026-05-23
**Deciders:** Eric Bowen
**Supersedes:** N/A
**Related:** ADR-002 (Massive API Integration, SQLite Database)

## Context

The trading bot makes AI-driven buy/sell/hold decisions but has no persistent record of those decisions or the market data that informed them. There is also no historical daily OHLCV data that could be used for future backtesting.

Two gaps need to be closed:

1. **AI analysis history.** When the trading loop (or a future on-demand "Request Analysis" action) produces a decision for a stock, the decision, rationale, and the enrichment snapshot (RSI, VWAP, moving averages, analyst ratings, current price) should be persisted with a timestamp. Today the latest decisions are written to `data/last-decisions.json` and overwritten each cycle — there is no longitudinal record.

2. **Daily OHLCV history.** To evaluate whether past AI decisions were good, we need the actual price movement that followed each decision. The Massive API's `get_aggs()` endpoint can return up to 2 years of daily bars (open, high, low, close, volume, VWAP) in a single call (~500 rows per ticker). This data should be loadable on-demand per ticker and stored in SQLite.

This ADR covers **only the data storage layer**. Backtesting logic that consumes this data is out of scope.

## Decision

### 1. Table Schema — `stock_analysis`

Stores every AI analysis snapshot: the decision itself plus the enrichment data that informed it.

```sql
CREATE TABLE IF NOT EXISTS stock_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT    NOT NULL,
    analyzed_at     TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    -- AI decision
    decision        TEXT    NOT NULL,          -- 'buy', 'sell', 'hold'
    quantity        REAL    NOT NULL DEFAULT 0,
    rationale       TEXT,
    -- Price at time of analysis
    price           REAL,
    -- Enrichment snapshot
    rsi             REAL,
    vwap            REAL,
    ma_50           REAL,                      -- 50-day moving average
    ma_200          REAL,                      -- 200-day moving average
    analyst_summary TEXT,                      -- JSON string: {buy: N, hold: N, sell: N}
    -- Position context (null for watchlist stocks)
    quantity_held   REAL,
    avg_buy_price   REAL,
    -- Source: 'loop' (automated cycle) or 'manual' (on-demand request)
    source          TEXT    NOT NULL DEFAULT 'loop'
);

CREATE INDEX IF NOT EXISTS idx_analysis_symbol     ON stock_analysis(symbol);
CREATE INDEX IF NOT EXISTS idx_analysis_symbol_date ON stock_analysis(symbol, analyzed_at);
```

**Design choices:**

- **Autoincrement integer PK, not composite key.** A single stock can be analyzed multiple times per day (manual requests plus automated cycles). An autoincrement PK avoids the awkwardness of a composite key that includes sub-second timestamps.
- **`analyzed_at` with index.** Queries will almost always filter by symbol + date range (e.g., "all AAPL analyses in the last 30 days"). The composite index `(symbol, analyzed_at)` covers this.
- **Enrichment columns are scalar, not JSON.** RSI, VWAP, MA-50, MA-200, and price are stored as individual `REAL` columns rather than a JSON blob. This makes them directly queryable for future analytics (e.g., "show me all decisions where RSI > 70").
- **`analyst_summary` as JSON string.** The summary is a small dict (`{buy: N, hold: N, sell: N}`) that is displayed as-is. Normalizing it into separate columns adds little value.
- **`analyst_ratings` (full text) is not stored.** The individual analyst rating texts are verbose and not useful for quantitative backtesting. They can be re-fetched from Robinhood if needed. This keeps the table compact.
- **`source` column.** Distinguishes automated loop decisions from on-demand manual analysis. Useful for filtering in future backtesting.

### 2. Table Schema — `stock_history`

Stores daily OHLCV bars from the Massive API.

```sql
CREATE TABLE IF NOT EXISTS stock_history (
    symbol          TEXT    NOT NULL,
    bar_date        TEXT    NOT NULL,           -- 'YYYY-MM-DD'
    open            REAL    NOT NULL,
    high            REAL    NOT NULL,
    low             REAL    NOT NULL,
    close           REAL    NOT NULL,
    volume          INTEGER NOT NULL,
    vwap            REAL,
    loaded_at       TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (symbol, bar_date)
);

CREATE INDEX IF NOT EXISTS idx_history_symbol ON stock_history(symbol);
```

**Design choices:**

- **Composite PK `(symbol, bar_date)`.** There is exactly one daily bar per symbol per date. This is a natural key and eliminates duplicates on re-load.
- **`bar_date` as TEXT `YYYY-MM-DD`.** SQLite has no native date type. ISO-8601 date strings sort correctly and are human-readable. The `analyzed_at` column in `stock_analysis` uses the same format convention established in the `tickers` table.
- **`vwap` is nullable.** The Massive API may not return VWAP for all tickers (e.g., crypto, OTC). Nullable avoids data loss.
- **`loaded_at` timestamp.** Tracks when the data was fetched, enabling staleness checks. On re-load, `INSERT OR REPLACE` updates this timestamp.
- **No separate index on `bar_date` alone.** Queries always filter by symbol first. The composite PK already serves as an index on `(symbol, bar_date)`. A standalone `bar_date` index would only help cross-stock date queries, which are not planned.

### 3. Massive API Call Strategy — `get_aggs()`

**Decision:** Fetch 2 years of daily bars in a single `get_aggs()` call with `limit=50000`.

```python
from datetime import datetime, timedelta

def fetch_daily_history(ticker):
    client = get_client()
    to_date = datetime.now().strftime('%Y-%m-%d')
    from_date = (datetime.now() - timedelta(days=730)).strftime('%Y-%m-%d')
    aggs = client.get_aggs(
        ticker=ticker,
        multiplier=1,
        timespan="day",
        from_=from_date,
        to=to_date,
        limit=50000,
    )
    return aggs
```

**Rationale:**

- 2 years of daily bars is ~500 rows per ticker. The `limit=50000` parameter ensures all bars return in a single API call — no pagination needed.
- Massive API rate limit is 5 calls/min, but since one call suffices per ticker, this is a non-issue for on-demand per-stock loading. Even bulk-loading 5 tickers takes only 1 minute of rate budget.
- The function returns the raw aggregates list. The caller (the Flask endpoint) maps them to `stock_history` rows and calls `db.upsert_stock_history()`.

### 4. API Endpoint — `POST /api/stock/<symbol>/load-history`

**Decision:** Add a new Flask endpoint for on-demand history loading.

```
POST /api/stock/<symbol>/load-history
```

**Behavior:**
1. Call `massive_client.fetch_daily_history(symbol)` to get ~500 daily bars.
2. Map each aggregate to a `stock_history` row: `{symbol, bar_date, open, high, low, close, volume, vwap}`.
3. Call `db.upsert_stock_history(rows)` — uses `INSERT OR REPLACE` on the `(symbol, bar_date)` composite PK so re-loads are idempotent.
4. Return JSON: `{ok: true, symbol: "AAPL", bars_loaded: 502, from_date: "2024-05-23", to_date: "2026-05-23"}`.

**Error handling:**
- If Massive API key is not configured, return `{ok: false, error: "Massive API key not configured"}` (HTTP 503).
- If `get_aggs()` raises (rate limit, invalid ticker, network error), return `{ok: false, error: "..."}` (HTTP 502).
- CSRF-exempt (JSON API endpoint, consistent with existing `/api/*` routes).

### 5. API Endpoint — Analysis Storage Hook

**Decision:** Store analysis snapshots in `stock_analysis` at two points.

**5a. Automated loop — `write_last_decisions()`**

In `main.py`, after the AI decisions are filtered, `write_last_decisions()` already iterates over all decisions. Add a call to `db.insert_stock_analyses()` here, passing the enrichment data from `portfolio_overview` and `watchlist_overview` alongside the decisions.

```python
def write_last_decisions(decisions_data, market_open,
                         portfolio_overview=None, watchlist_overview=None):
    # ... existing JSON/shared-state persistence ...
    # NEW: persist to stock_analysis table
    if decisions_data:
        db.insert_stock_analyses(
            decisions_data,
            portfolio_overview or {},
            watchlist_overview or {},
            source='loop',
        )
```

The `portfolio_overview` and `watchlist_overview` dicts already contain `current_price`, `rsi`, `vwap`, `50_day_mavg_price`, `200_day_mavg_price`, `analyst_summary`, `my_quantity`, and `my_average_buy_price` — exactly the enrichment columns in `stock_analysis`.

**5b. On-demand "Request Analysis" (future endpoint)**

A future `POST /api/stock/<symbol>/analyze` endpoint will:
1. Fetch Robinhood enrichment data (same flow as the trading loop's per-stock enrichment).
2. Send the enrichment data to Claude for a single-stock AI decision.
3. Store the result in `stock_analysis` with `source='manual'`.
4. Return the analysis to the modal UI.

This endpoint is **not** part of the current implementation scope — the ADR defines the schema and storage hook so the table is populated from the automated loop immediately, and the manual endpoint can be added without schema changes.

### 6. Database Functions — `src/db.py`

Add the following functions to `src/db.py`:

```python
def insert_stock_analyses(decisions, portfolio_overview, watchlist_overview, source='loop'):
    """Batch-insert AI analysis snapshots into stock_analysis."""

def upsert_stock_history(rows):
    """Batch INSERT OR REPLACE rows into the stock_history table."""

def get_stock_analyses(symbol, limit=50, offset=0):
    """Return analysis history for a symbol, newest first."""

def get_stock_history(symbol, from_date=None, to_date=None):
    """Return daily OHLCV bars for a symbol within a date range."""

def get_stock_history_coverage(symbol):
    """Return {min_date, max_date, bar_count} for a symbol, or None if no data."""
```

All write functions use the existing `_write_lock` pattern. All read functions are lock-free (WAL mode).

### 7. Modal UI Changes — `templates/base.html`

**"Load History" button:**
- Rendered inside the stock detail modal, below the Ticker Details section.
- Shows the current coverage (e.g., "History: 2024-05-23 to 2026-05-23 (502 bars)") if data exists.
- Shows "No historical data loaded" with a "Load History" button if no data exists.
- Button triggers `POST /api/stock/<symbol>/load-history` via fetch.
- While loading, button text changes to "Loading..." and is disabled.
- On success, updates the coverage display.

**"Request Analysis" button (future):**
- Placeholder in this ADR. The button will appear in the AI Analysis section of the modal. Implementation will be in a follow-up issue.

### 8. Schema Migration Strategy

**Decision:** Add the two new `CREATE TABLE IF NOT EXISTS` statements to `_SCHEMA_SQL` in `src/db.py`, appended after the existing `tickers` table DDL.

**Rationale:**
- `CREATE TABLE IF NOT EXISTS` is idempotent — existing databases get the new tables on next startup; new databases get all tables.
- No existing tables are altered. This is purely additive.
- `init_db()` already runs `executescript()` on the full `_SCHEMA_SQL` string, so appending new statements is seamless.

## Consequences

### Positive

- **Longitudinal AI decision tracking.** Every analysis (automated and future manual) is persisted with its full context. This enables questions like "what did the bot decide about AAPL last month, and was it right?"
- **Foundation for backtesting.** With `stock_analysis` (decisions + context) and `stock_history` (actual price movement), a backtesting module can compute P&L on hypothetical trades.
- **Minimal new dependencies.** Zero new packages. The Massive API client (`massive`) is already a dependency (ADR-002). SQLite and `threading` are stdlib.
- **Consistent patterns.** New code follows the `src/db.py` wrapper pattern (write lock, WAL mode, `INSERT OR REPLACE`, `_connect()` per operation).
- **No data loss on re-load.** Both tables use idempotent upsert semantics. Re-loading history or re-analyzing a stock updates existing rows or adds new ones — never corrupts.

### Negative

- **`stock_analysis` grows unbounded.** Each automated cycle writes one row per stock (~20-50 stocks per cycle, every `RUN_INTERVAL_SECONDS`). At 10-minute intervals, 8 hours/day, 252 trading days/year, 40 stocks: ~480k rows/year. SQLite handles this fine, but a retention policy (e.g., archive rows older than 1 year) should be considered later.
- **Enrichment data is denormalized.** RSI, VWAP, and moving averages are snapshotted into `stock_analysis` rather than referencing a time-series table. This is intentional — the snapshot captures what the AI *saw* at decision time, which may differ from a later recalculation.

### Risks

- **Massive API `get_aggs()` returns no data for some tickers.** OTC, crypto, or recently-IPO'd tickers may have sparse or no daily bar history. Mitigation: the endpoint reports `bars_loaded: 0` and the UI shows "No data available" — not an error.
- **Schema evolution.** If additional enrichment fields are needed in `stock_analysis` later (e.g., Bollinger bands), an `ALTER TABLE ADD COLUMN` is required. Mitigation: the schema is designed with the current enrichment set, which is stable. Adding nullable columns to SQLite is a non-destructive, instant operation.

## Implementation Plan

### Phase 1 (this issue — data storage only)

1. Add `stock_analysis` and `stock_history` table DDL to `_SCHEMA_SQL` in `src/db.py`.
2. Add `insert_stock_analyses()`, `upsert_stock_history()`, `get_stock_analyses()`, `get_stock_history()`, `get_stock_history_coverage()` to `src/db.py`.
3. Add `fetch_daily_history(ticker)` to `src/api/massive_client.py`.
4. Add `POST /api/stock/<symbol>/load-history` endpoint to `webui.py`.
5. Modify `write_last_decisions()` in `main.py` to accept and pass enrichment data to `db.insert_stock_analyses()`.
6. Add "Load History" button and coverage display to the stock detail modal in `templates/base.html`.
7. Wire `/api/stock/<symbol>` to include history coverage in its response.

### Phase 2 (future — not this issue)

- `POST /api/stock/<symbol>/analyze` endpoint for on-demand single-stock AI analysis.
- "Request Analysis" button in the modal UI.
- Backtesting module that consumes `stock_analysis` + `stock_history`.
- Retention policy for `stock_analysis` (archive or prune old rows).

## File Change Summary

| File | Change |
|------|--------|
| `src/db.py` | Add `stock_analysis` + `stock_history` DDL to `_SCHEMA_SQL`; add 5 new functions |
| `src/api/massive_client.py` | Add `fetch_daily_history(ticker)` |
| `webui.py` | Add `POST /api/stock/<symbol>/load-history` endpoint; extend `api_stock_detail` to include history coverage |
| `main.py` | Modify `write_last_decisions()` to persist enrichment data to `stock_analysis` |
| `templates/base.html` | Add "Load History" button + coverage display to stock detail modal |
| `docs/adr/003-historical-data-storage.md` | **New** — this ADR |
| `docs/adr/003-historical-data-storage.json` | **New** — machine-readable ADR summary |
