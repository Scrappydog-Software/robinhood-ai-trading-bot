# ADR-002: Massive API Integration, SQLite Database, and Stock Research Page

**Status:** Proposed
**Date:** 2026-05-23
**Deciders:** Eric Bowen
**Supersedes:** N/A
**Related:** ADR-001 (Unified App Entry Point)

## Context

The trading bot currently has no persistent data store and relies entirely on live Robinhood API calls for stock data. There is no way to browse or research the broader universe of tradable securities — the bot only knows about stocks in the user's portfolio and configured watchlists.

We want to:
1. Integrate the Massive API (stock market data provider, formerly Polygon.io) to access reference data for all tradable tickers.
2. Add a SQLite database to store this reference data locally for fast querying.
3. Add a Stock Research web page to the Flask UI for browsing and searching ticker data.

The Massive API client (`massive` Python SDK) provides `list_tickers()` and `get_ticker_details()` endpoints. The API key (`MASSIVE_API_KEY`) is already present in `config.py.example` as a planned integration point.

## Decision

### 1. Massive API Client — `src/api/massive_client.py`

**Decision:** Create a new module at `src/api/massive_client.py` following the existing pattern of `src/api/robinhood.py` and `src/api/claude.py`.

**Rationale:**
- The `src/api/` package is the established home for external API integrations.
- The module name uses `massive_client` (not `massive`) to avoid shadowing the `massive` pip package itself. Python's import system would otherwise resolve `from src.api import massive` to the wrong module when the top-level `massive` package is also installed.
- The client reads `MASSIVE_API_KEY` from `config.py` using the project's `from config import *` + `try/except NameError` defensive fallback pattern.
- The module instantiates a singleton `RESTClient` at module level, consistent with how `src/api/claude.py` creates its `Anthropic()` client.

**Key functions:**
- `get_client()` — returns the lazily-initialized `RESTClient` instance (lazy to tolerate missing API key when module is imported but not used).
- `fetch_all_tickers()` — wraps `client.list_tickers()`, returns an iterator of Ticker objects. Handles pagination internally (the SDK's iterator does this).
- `fetch_ticker_details(ticker_symbol)` — wraps `client.get_ticker_details()`, returns a single Ticker details object.

**Alternatives considered:**
- *Putting the client in `main.py` or a top-level script.* Rejected — violates the module layout documented in `.superbot/context.md`.
- *Naming the file `massive.py`.* Rejected — would shadow the `massive` pip package.

### 2. SQLite Database — `data/market.db`

**Decision:** Use Python's built-in `sqlite3` module with a thin wrapper at `src/db.py`. Store the database file at `data/market.db`.

**Rationale:**

**Location: `data/` directory**
- The `data/` directory is already gitignored and used for runtime state (`data/last-decisions.json`).
- The database file contains downloaded reference data, not source code — it belongs alongside other runtime artifacts.
- The `.gitignore` already covers `data/` so the `.db` file will not accidentally be committed.

**Access pattern: raw `sqlite3` (no ORM)**
- The project has zero database dependencies today. Adding SQLAlchemy would introduce a significant new dependency (with its own sub-dependencies) for what is initially a single-table read-heavy workload.
- Python's `sqlite3` module is in the standard library — zero new dependencies.
- The wrapper module (`src/db.py`) encapsulates all SQL so the rest of the codebase never writes raw SQL inline.
- If the schema grows beyond 3-4 tables or we need migrations, we can introduce SQLAlchemy at that point (separate ADR).

**Schema — `tickers` table (flat, single table):**
```sql
CREATE TABLE IF NOT EXISTS tickers (
    ticker          TEXT PRIMARY KEY,
    name            TEXT,
    market          TEXT,
    locale          TEXT,
    type            TEXT,
    active          INTEGER,  -- boolean: 1/0
    currency_name   TEXT,
    currency_symbol TEXT,
    base_currency_symbol TEXT,
    base_currency_name   TEXT,
    cik             TEXT,
    composite_figi  TEXT,
    share_class_figi TEXT,
    primary_exchange TEXT,
    last_updated_utc TEXT,
    delisted_utc    TEXT,
    source_feed     TEXT,
    -- Metadata
    loaded_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_tickers_name ON tickers(name);
CREATE INDEX IF NOT EXISTS idx_tickers_market ON tickers(market);
CREATE INDEX IF NOT EXISTS idx_tickers_type ON tickers(type);
CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers(active);
```

**Design choices:**
- **Flat, not normalized.** The `list_tickers()` response is already flat. Normalizing `market`, `type`, `locale`, or `primary_exchange` into lookup tables adds complexity for no querying benefit at this data volume (~15k-50k rows).
- **`ticker` as primary key.** Tickers are unique identifiers in the Massive API. No surrogate key needed.
- **`active` stored as INTEGER.** SQLite has no native boolean; 1/0 is the standard convention.
- **`loaded_at` timestamp.** Tracks when each row was inserted/updated, enabling staleness checks and incremental refresh.
- **No separate `ticker_details` table yet.** The detailed fields (address, branding, description, market_cap, etc.) are fetched per-ticker and are expensive to bulk-load (~50k API calls). Phase 1 loads only the reference list. A `ticker_details` table can be added later when the Stock Research page needs it — fetched on-demand and cached.

**Alternatives considered:**
- *SQLAlchemy ORM.* Rejected for now — overhead disproportionate to a single-table workload. Revisit at 3+ tables.
- *Separate databases for different data domains.* Rejected — one `market.db` file is simpler to manage and back up.
- *JSON file instead of SQLite.* Rejected — ~50k ticker rows need indexed search; JSON would require loading the entire dataset into memory.
- *Storing `ticker_details` in the same table.* Rejected — the details response has 20+ additional fields and requires per-ticker API calls. Mixing bulk-loadable reference data with expensive per-ticker data creates a confusing update model.

### 3. Database Wrapper — `src/db.py`

**Decision:** A single module that owns the database connection, schema creation, and all query functions.

**Key functions:**
- `get_connection()` — returns a `sqlite3.Connection` with `row_factory = sqlite3.Row` for dict-like access. Uses a module-level connection (not pooled — SQLite is single-writer anyway).
- `init_db()` — runs `CREATE TABLE IF NOT EXISTS` and index statements. Called once at startup (from `app.py`) and idempotently from the ticker load script.
- `upsert_tickers(rows)` — bulk insert/update using `INSERT OR REPLACE`. Accepts an iterable of dicts matching the schema.
- `search_tickers(query, market=None, type=None, active_only=True, limit=50, offset=0)` — parameterized search by name or ticker symbol with optional filters. Uses `LIKE` for substring matching (sufficient for ~50k rows; FTS5 can be added later if needed).
- `get_ticker(symbol)` — fetch a single ticker row by primary key.
- `get_ticker_count()` — returns total row count (for UI display).

**Thread safety:**
- SQLite connections are NOT thread-safe by default. The wrapper will create connections with `check_same_thread=False` and use a `threading.Lock` around write operations (same pattern as `src/state.py`'s `TradingState._lock`).
- Read operations (which dominate — the web UI) do not need locking in WAL mode. The wrapper will set `PRAGMA journal_mode=WAL` on connection open.

### 4. Dependency — `massive` package

**Decision:** Add `massive` to `requirements.txt`.

```
massive>=1.0.0
```

**Rationale:**
- The `massive` package is the official SDK for the Massive API. It handles authentication, pagination, and response parsing.
- Version pin uses `>=1.0.0` (floor pin) rather than `~=` (compatible release) because the SDK is maintained by the same organization (`Scrappydog-Software/client-python`) and we want to track upstream changes.

**Config change — `config.py.example`:**
Add the `MASSIVE_API_KEY` entry (already planned):
```python
MASSIVE_API_KEY = "..."                     # Massive API key (stock market data)
```

The defensive fallback in code:
```python
try:
    MASSIVE_API_KEY
except NameError:
    MASSIVE_API_KEY = ""
```

### 5. One-Time Ticker Load — Flask route + CLI fallback

**Decision:** Implement as a Flask route (`POST /api/tickers/load`) with a CLI fallback script (`scripts/load_tickers.py`).

**Flask route (`POST /api/tickers/load`):**
- Triggered from the Stock Research page via a button ("Load/Refresh Tickers").
- Calls `massive_client.fetch_all_tickers()`, pipes results through `db.upsert_tickers()`.
- Returns JSON: `{ok: true, count: N, duration_seconds: X}`.
- CSRF-exempt (JSON API endpoint, same as `/api/loop/*`).
- Long-running (~10-30 seconds depending on ticker count) — acceptable for a local-only UI. No background task queue needed.

**CLI script (`scripts/load_tickers.py`):**
- Standalone script: `python scripts/load_tickers.py`
- Imports `src.api.massive_client` and `src.db` directly.
- Useful for initial setup before starting the app, or for cron-based refresh.
- Prints progress to stdout.

**Alternatives considered:**
- *Flask CLI command (`flask load-tickers`).* Rejected — the project doesn't use Flask CLI commands anywhere and the `app` factory pattern would need rework.
- *Background thread.* Rejected — adds complexity for a one-time operation that takes <30 seconds. The local-only UI can tolerate a brief wait.
- *Automatic load on startup.* Rejected — would slow every app start. The load should be explicit.

### 6. Stock Research Page — `/research`

**Decision:** Add a new Flask route and template for browsing/searching ticker data.

**Route:** `GET /research` (in `webui.py`)

**Template:** `templates/research.html` (extends `base.html`)

**Initial features:**
- **Search bar** — text input that searches by ticker symbol or company name (substring match via `db.search_tickers()`).
- **Filter controls** — dropdown for market (stocks, crypto, otc, fx), dropdown for type (CS, ETF, etc.), checkbox for "active only" (default: checked).
- **Results table** — columns: Ticker, Name, Type, Market, Exchange, Currency, Active, Last Updated.
- **Ticker count** — displays total number of tickers in the database and a prompt to load if zero.
- **Load/Refresh button** — triggers `POST /api/tickers/load` (with a loading spinner).
- **Pagination** — simple prev/next with limit=50 per page.

**Navigation:**
- Add a "Stock Research" link to `base.html`'s header (alongside the existing title). This makes it accessible from all pages.

**No detail view in Phase 1.** Clicking a ticker does not open a detail page yet. That requires `get_ticker_details()` API calls and a `ticker_details` table — planned for a future iteration.

**Alternatives considered:**
- *Client-side search (load all tickers into JS).* Rejected — ~50k rows is too large for client-side filtering to be responsive.
- *HTMX for dynamic search.* Rejected — the project has no JS framework dependencies and adding HTMX would be a new pattern. Standard form submission + server-side rendering is consistent with the existing UI.
- *Separate Flask blueprint.* Rejected — the app has one `webui.py` file with all routes. A blueprint adds structural complexity that isn't justified until the route count is much higher.

### 7. Database Initialization at Startup

**Decision:** Call `db.init_db()` from `app.py`'s `main()` function, before starting the Flask server.

**Rationale:**
- Ensures the schema exists before any web request can hit `/research`.
- Idempotent (`CREATE TABLE IF NOT EXISTS`) — safe to call on every startup.
- No data is loaded automatically — only the schema is created. The user triggers ticker loading explicitly.

## Consequences

### Positive
- **Local search capability.** Users can browse and search the full ticker universe without round-tripping to an external API on every query.
- **Foundation for richer features.** The SQLite database and Massive API client enable future work: ticker detail pages, historical price storage, AI-enriched research, cross-referencing portfolio/watchlist with fundamental data.
- **Zero new heavyweight dependencies.** `sqlite3` is in the standard library. Only `massive` is added to `requirements.txt`.
- **Consistent patterns.** New code follows established conventions (`src/api/`, `from config import *`, defensive `NameError` fallbacks, dark-themed Jinja2 templates).

### Negative
- **Manual ticker load.** Users must explicitly trigger the load. If they forget, the Research page shows no data. Mitigated by a clear prompt on the page.
- **No incremental sync.** The `INSERT OR REPLACE` approach reloads all tickers. For ~50k rows this is fast enough (<30s). If the dataset grows significantly, incremental sync by `last_updated_utc` would be needed.
- **SQLite single-writer limitation.** Concurrent writes (e.g., ticker load while the trading loop writes decisions) could contend. Mitigated by WAL mode and the fact that writes are infrequent.

### Risks
- **Massive API rate limits.** `list_tickers()` paginates through all tickers. If the API rate-limits aggressively, the load could fail partway. Mitigation: the SDK handles pagination; partial loads are safe because `INSERT OR REPLACE` is idempotent.
- **Schema evolution.** Adding columns later requires `ALTER TABLE`. Without a migration framework, this must be done carefully. Mitigation: keep the schema minimal in Phase 1; add a migration tool if/when the schema changes more than once.

## Implementation Plan

### Phase 1 (this issue)
1. Add `massive>=1.0.0` to `requirements.txt`.
2. Add `MASSIVE_API_KEY` to `config.py.example`.
3. Create `src/api/massive_client.py`.
4. Create `src/db.py` with `tickers` table schema.
5. Create `scripts/load_tickers.py`.
6. Add `POST /api/tickers/load` route to `webui.py`.
7. Add `GET /research` route and `templates/research.html`.
8. Add navigation link in `base.html`.
9. Call `db.init_db()` from `app.py`.
10. Update `.superbot/context.md` to reflect new dependencies and modules.

### Phase 2 (future)
- `ticker_details` table with on-demand caching.
- Ticker detail page (`/research/<ticker>`).
- Cross-reference portfolio/watchlist tickers with Massive data.
- Historical price data storage.

## File Change Summary

| File | Change |
|------|--------|
| `requirements.txt` | Add `massive>=1.0.0` |
| `config.py.example` | Add `MASSIVE_API_KEY` |
| `src/api/massive_client.py` | **New** — Massive API client wrapper |
| `src/db.py` | **New** — SQLite database wrapper |
| `scripts/load_tickers.py` | **New** — CLI ticker load script |
| `webui.py` | Add `/research` route, `/api/tickers/load` route |
| `templates/base.html` | Add "Stock Research" nav link in header |
| `templates/research.html` | **New** — Stock Research page template |
| `static/style.css` | Add styles for search/filter controls, research page |
| `app.py` | Add `db.init_db()` call at startup |
| `.superbot/context.md` | Update tech stack, module layout, dependencies |
| `docs/adr/002-massive-api-sqlite-stock-research.md` | **New** — this ADR |
