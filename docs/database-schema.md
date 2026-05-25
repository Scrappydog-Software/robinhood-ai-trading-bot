# Database Schema

## Overview

The application uses a single SQLite database at `data/market.db` with WAL (Write-Ahead Logging) journal mode for concurrent read performance. A `threading.Lock` guards all write operations so the Flask request thread and background trading loop can coexist safely.

The database layer is implemented in `src/db.py`.

## Tables

### `tickers` -- Ticker Reference Data

Stores the full universe of tradeable tickers loaded from the Massive (Polygon.io) API. Populated by the `/api/tickers/load` endpoint, which streams all tickers in batches of 1,000.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `ticker` | TEXT | **PRIMARY KEY** | Stock symbol (e.g., `AAPL`) |
| `name` | TEXT | | Company name (e.g., `Apple Inc.`) |
| `market` | TEXT | | Market identifier (e.g., `stocks`, `otc`, `crypto`) |
| `locale` | TEXT | | Locale code (e.g., `us`) |
| `type` | TEXT | | Security type (e.g., `CS` for common stock, `ETF`, `ADRC`) |
| `active` | INTEGER | | Whether the ticker is actively traded (1 = active, 0 = inactive) |
| `currency_name` | TEXT | | Currency name (e.g., `United States Dollar`) |
| `currency_symbol` | TEXT | | Currency symbol (e.g., `USD`) |
| `base_currency_symbol` | TEXT | | Base currency symbol (for crypto pairs) |
| `base_currency_name` | TEXT | | Base currency name (for crypto pairs) |
| `cik` | TEXT | | SEC CIK number |
| `composite_figi` | TEXT | | Bloomberg composite FIGI identifier |
| `share_class_figi` | TEXT | | Bloomberg share class FIGI identifier |
| `primary_exchange` | TEXT | | Primary exchange (e.g., `XNAS`, `XNYS`) |
| `last_updated_utc` | TEXT | | When the ticker data was last updated upstream |
| `delisted_utc` | TEXT | | Delisting date (if applicable) |
| `source_feed` | TEXT | | Data source feed identifier |
| `loaded_at` | TEXT | NOT NULL, DEFAULT `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` | When the row was loaded into the local DB |

**Indexes:**
- `idx_tickers_name` on `name`
- `idx_tickers_market` on `market`
- `idx_tickers_type` on `type`
- `idx_tickers_active` on `active`

**Populated by:** `upsert_tickers()` (INSERT OR REPLACE), called from `/api/tickers/load` endpoint which fetches all tickers from `massive_client.fetch_all_tickers()`.

---

### `stock_history` -- Daily OHLCV Bars + Indicators + Signals

Stores daily price bars, 16 computed technical indicator columns, 8 signal columns, and a recommendation for each bar. This is the primary data table for technical analysis.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `symbol` | TEXT | NOT NULL, PK (part 1) | Stock symbol |
| `bar_date` | TEXT | NOT NULL, PK (part 2) | Date in `YYYY-MM-DD` format |
| **OHLCV Data** | | | |
| `open` | REAL | | Opening price |
| `high` | REAL | | High price |
| `low` | REAL | | Low price |
| `close` | REAL | | Closing price |
| `volume` | INTEGER | | Trading volume |
| `vwap` | REAL | | Volume-weighted average price (from API) |
| `transactions` | INTEGER | | Number of transactions in the bar |
| **Recommendation** | | | |
| `recommendation` | TEXT | | Overall recommendation label: `strong_buy`, `buy`, `hold`, `sell`, `strong_sell`. Set by `compute_signals()` (equals `signal_synthesis`) or by Haiku backtest analysis |
| **Moving Averages** | | | |
| `sma_10` | REAL | | 10-day simple moving average of close |
| `sma_20` | REAL | | 20-day simple moving average of close |
| `sma_50` | REAL | | 50-day simple moving average of close |
| `sma_200` | REAL | | 200-day simple moving average of close |
| `ema_12` | REAL | | 12-day exponential moving average of close |
| `ema_26` | REAL | | 26-day exponential moving average of close |
| **RSI** | | | |
| `rsi_14` | REAL | | 14-period Relative Strength Index (0-100) |
| **MACD** | | | |
| `macd_line` | REAL | | MACD line (EMA(12) - EMA(26)) |
| `macd_signal` | REAL | | MACD signal line (9-period EMA of MACD line) |
| `macd_histogram` | REAL | | MACD histogram (macd_line - macd_signal) |
| **Bollinger Bands** | | | |
| `bb_upper` | REAL | | Upper Bollinger Band (SMA(20) + 2 * StdDev) |
| `bb_lower` | REAL | | Lower Bollinger Band (SMA(20) - 2 * StdDev) |
| `bb_width` | REAL | | Band width as percentage: (upper - lower) / SMA(20) * 100 |
| **Volume Indicators** | | | |
| `vol_sma_20` | REAL | | 20-day simple moving average of volume |
| `vol_ratio` | REAL | | Today's volume / vol_sma_20 (values > 1.0 = above-average volume) |
| `obv` | REAL | | On-Balance Volume (cumulative running total) |
| **Signal Columns** | | | |
| `signal_ma` | TEXT | | Section 1 signal: Moving Average Crossover result |
| `signal_rsi` | TEXT | | Section 2 signal: RSI result |
| `signal_macd` | TEXT | | Section 3 signal: MACD result |
| `signal_rsi_macd` | TEXT | | Section 4 signal: RSI+MACD dual-confirmation result |
| `signal_bb` | TEXT | | Section 5 signal: Bollinger Bands result |
| `signal_volume` | TEXT | | Section 6 signal: Volume confirmation result |
| `signal_synthesis` | TEXT | | Section 7 signal: Overall synthesized recommendation |
| `signal_score` | INTEGER | | Total numeric score (-14 to +14), sum of all 6 section scores |
| **Metadata** | | | |
| `loaded_at` | TEXT | NOT NULL, DEFAULT `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` | When the bar was loaded |

**Primary Key:** `(symbol, bar_date)`

**Indexes:**
- `idx_stock_history_symbol` on `symbol`

**Populated by:**
1. `upsert_stock_history()` -- inserts OHLCV data from `massive_client.fetch_daily_bars()`
2. `compute_indicators()` -- fills the 16 indicator columns (sma_10 through obv)
3. `compute_signals()` -- fills the 8 signal columns (signal_ma through signal_score) and sets `recommendation`
4. `update_bar_recommendations()` -- can overwrite `recommendation` from Haiku backtest analysis

---

### `stock_analysis` -- AI Decision History

Stores every AI decision made by the trading loop or on-demand analysis. Each row captures the decision, the market context at the time, and the source.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `id` | INTEGER | PRIMARY KEY AUTOINCREMENT | Unique row ID |
| `symbol` | TEXT | NOT NULL | Stock symbol |
| `analyzed_at` | TEXT | NOT NULL | ISO-8601 UTC timestamp of analysis |
| `decision` | TEXT | NOT NULL | AI decision: `strong_buy`, `buy`, `hold`, `sell`, `strong_sell` |
| `quantity` | REAL | | Recommended trade quantity |
| `rationale` | TEXT | | Claude's explanation referencing RSI, VWAP, moving averages, etc. |
| `price` | REAL | | Current price at time of analysis |
| `rsi` | REAL | | RSI value at time of analysis |
| `vwap` | REAL | | VWAP at time of analysis |
| `ma_50` | REAL | | 50-day moving average at time of analysis |
| `ma_200` | REAL | | 200-day moving average at time of analysis |
| `analyst_summary` | TEXT | | JSON-encoded analyst ratings summary from Robinhood |
| `held_quantity` | REAL | | Number of shares held at time of analysis |
| `held_avg_price` | REAL | | Average buy price of held shares |
| `source` | TEXT | NOT NULL, DEFAULT `'loop'` | Source of the analysis: `loop` (automated trading loop) or `on_demand` (user-triggered via web UI) |

**Indexes:**
- `idx_stock_analysis_symbol_date` on `(symbol, analyzed_at)`

**Populated by:**
1. `insert_stock_analysis()` called from `main.write_last_decisions()` during each trading loop cycle (source = `loop`)
2. `insert_stock_analysis()` called from `webui.api_stock_analyze()` for on-demand single-stock analysis (source = `on_demand`)

---

### `stock_stats` -- Per-Symbol Backtest Statistics

Stores computed backtest results and the latest signal for each analyzed symbol. Used by the screener page, dashboard recommendations, and stock detail views.

| Column | Type | Constraints | Description |
|---|---|---|---|
| `symbol` | TEXT | **PRIMARY KEY** | Stock symbol |
| `backtest_return_pct` | REAL | | 2-year backtest return percentage (NULL if < 400 bars) |
| `backtest_trades` | INTEGER | | Number of completed trades in 2-year backtest (NULL if < 400 bars) |
| `backtest_final` | REAL | | Final portfolio value from 2-year backtest starting with $100 (NULL if < 400 bars) |
| `bt_1yr_return_pct` | REAL | | 1-year backtest return percentage (last 252 bars) |
| `bt_1yr_trades` | INTEGER | | Number of completed trades in 1-year backtest |
| `bt_1yr_final` | REAL | | Final portfolio value from 1-year backtest starting with $100 |
| `latest_signal` | TEXT | | Most recent non-neutral signal_synthesis value |
| `latest_score` | INTEGER | | Signal score corresponding to latest_signal |
| `history_bars` | INTEGER | | Total number of history bars available for this symbol |
| `updated_at` | TEXT | NOT NULL, DEFAULT `strftime('%Y-%m-%dT%H:%M:%SZ', 'now')` | When stats were last computed |

**Populated by:** `compute_backtest_stats()`, which is called automatically at the end of `compute_signals()`. Runs two backtest simulations (1-year and full-history) and stores results via INSERT OR REPLACE.

---

## Key Functions

### `init_db()`

Creates all tables and indexes if they do not already exist. Runs migration statements (ALTER TABLE ADD COLUMN) that silently fail if columns already exist. Called once from `app.py` at startup.

### `compute_indicators(symbol)`

Reads OHLCV data from `stock_history`, computes all 16 technical indicators using pure Python math (no external libraries), and writes them back to the database. Indicators computed:

1. **SMA(10)** -- 10-day simple moving average
2. **SMA(20)** -- 20-day simple moving average
3. **SMA(50)** -- 50-day simple moving average
4. **SMA(200)** -- 200-day simple moving average
5. **EMA(12)** -- 12-day exponential moving average
6. **EMA(26)** -- 26-day exponential moving average
7. **RSI(14)** -- 14-period Relative Strength Index
8. **MACD Line** -- EMA(12) - EMA(26)
9. **MACD Signal** -- 9-period EMA of MACD Line
10. **MACD Histogram** -- MACD Line - MACD Signal
11. **Bollinger Upper Band** -- SMA(20) + 2 * StdDev(20)
12. **Bollinger Lower Band** -- SMA(20) - 2 * StdDev(20)
13. **Bollinger Band Width** -- (Upper - Lower) / SMA(20) * 100
14. **Volume SMA(20)** -- 20-day average volume
15. **Volume Ratio** -- Today's volume / Volume SMA(20)
16. **OBV** -- On-Balance Volume (cumulative)

Automatically calls `compute_signals()` after indicators are written.

### `compute_signals(symbol)`

Delegates to `src/signals.compute_signals_for_bars()` to run the 7-section rule-based scoring engine. Writes signal columns and sets `recommendation` equal to `signal_synthesis`. Automatically calls `compute_backtest_stats()` after signals are computed.

### `compute_backtest_stats(symbol)`

Runs backtest simulations on signal data:
- **1-year backtest:** Last 252 trading bars
- **2-year backtest:** All bars (only stored if >= 400 bars available)

Simulation rules: start with $100, buy at close on buy/strong_buy signal, sell at open on sell/strong_sell signal. Stores results in `stock_stats`.

---

## Data Pipeline Sequence

```
1. upsert_stock_history(symbol, bars)     -- Load raw OHLCV from Massive API
        |
        v
2. compute_indicators(symbol)             -- Compute 16 technical indicators
        |
        v  (called automatically)
3. compute_signals(symbol)                -- Run 7-section signal scoring engine
        |
        v  (called automatically)
4. compute_backtest_stats(symbol)         -- Simulate trades and store returns
```

Each step in the pipeline triggers the next automatically, so calling `compute_indicators()` on a symbol with loaded history will cascade through the entire pipeline.
