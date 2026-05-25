# Project Overview

## What It Does

Robinhood AI Trading Bot is an AI-powered stock trading application that connects to a Robinhood brokerage account, analyzes portfolio and watchlist stocks using both rule-based technical indicators and Claude LLM analysis, and optionally executes buy/sell trades automatically. It ships with a local-only Flask web UI for monitoring positions, researching tickers, viewing historical price charts with backtest overlays, and screening stocks by signal strength.

## Architecture

```
                       +-------------------+
                       |     app.py        |  Unified entry point
                       | (python app.py)   |
                       +--------+----------+
                                |
               +----------------+----------------+
               |                                 |
    +----------v-----------+          +----------v-----------+
    |  Trading Loop Thread |          |   Flask Web UI       |
    |  src/trading/loop.py |          |   webui.py           |
    |  (daemon thread)     |          |   127.0.0.1:5001     |
    +----------+-----------+          +----------+-----------+
               |                                 |
               +--------+-------+--------+-------+
                        |       |        |
               +--------v--+ +--v-----+ +v-----------+
               | Robinhood | | Claude | | Massive    |
               | API       | | API    | | API        |
               | (broker)  | | (LLM)  | | (market    |
               |           | |        | |  data)     |
               +-----------+ +--------+ +------------+
                        |       |        |
                        +---+---+---+----+
                            |       |
                     +------v-------v------+
                     |  SQLite (WAL mode)  |
                     |  data/market.db     |
                     +---------------------+
```

### Core Components

| Component | File | Purpose |
|---|---|---|
| **Entry point** | `app.py` | Initializes DB, authenticates Robinhood, starts trading loop thread, starts Flask server |
| **Web UI** | `webui.py` | Flask application with routes, templates, and API endpoints |
| **Trading logic** | `main.py` | Enriches stock data, sends to Claude for decisions, executes trades |
| **Trading loop** | `src/trading/loop.py` | Background daemon thread that runs `trading_bot()` on a configurable interval |
| **Shared state** | `src/state.py` | Thread-safe `TradingState` singleton shared between loop and web UI |
| **Database** | `src/db.py` | SQLite layer for tickers, price history, indicators, signals, analysis, and stats |
| **Signal engine** | `src/signals.py` | Rule-based 7-section technical analysis scoring (no LLM needed) |
| **Market data** | `src/api/massive_client.py` | Fetches tickers, OHLCV bars, snapshots, and computes RSI/VWAP/MAs |
| **Brokerage** | `src/api/robinhood.py` | Authentication, portfolio, watchlists, order execution, analyst ratings |
| **LLM client** | `src/api/claude.py` | Anthropic Claude API wrapper for AI-based trade decisions |

## Key Web Pages

| Route | Page | Description |
|---|---|---|
| `/` | **Dashboard** | Account buying power, portfolio holdings with P&L, all Robinhood watchlists, actionable recommendations from signal engine, trading loop status with start/stop controls |
| `/screener` | **Stock Screener** | All analyzed stocks with latest signal, score, 1-year and 2-year backtest returns, searchable and sortable |
| `/research` | **Research** | Full ticker database from Massive API with search, filters (market, type, active status), and pagination (50 per page) |
| `/history/<symbol>` | **Stock History** | Interactive price chart with technical indicators overlay, buy/sell markers, backtest simulation results, and full data table |

### API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/api/status` | GET | Trading loop status (running, last cycle, market open) |
| `/api/stock/<symbol>` | GET | Aggregated stock detail (position, ticker, history status, stats, recommendation) |
| `/api/stock/<symbol>/analyze` | POST | On-demand Claude LLM analysis for a single stock |
| `/api/stock/<symbol>/load-history` | POST | Fetch 2 years of daily bars from Massive API into SQLite |
| `/api/stock/<symbol>/compute-indicators` | POST | Compute 16 technical indicators on existing history |
| `/api/stock/<symbol>/compute-signals` | POST | Run rule-based signal scoring (no LLM) |
| `/api/stock/<symbol>/backtest-analyze` | POST | Batch-analyze all historical bars via Claude Haiku |
| `/api/loop/start` | POST | Start the background trading loop |
| `/api/loop/stop` | POST | Stop the background trading loop |
| `/api/tickers/load` | POST | Bulk-load all tickers from Massive API |
| `/watchlists/create` | POST | Create a new Robinhood watchlist |
| `/watchlists/<name>/add` | POST | Add a symbol to a watchlist |
| `/watchlists/<name>/remove` | POST | Remove a symbol from a watchlist |

## Data Flow

### Market Data Pipeline

```
Massive API (fetch_daily_bars)
    |
    v
stock_history table (OHLCV + vwap)
    |
    v
compute_indicators() -- 16 technical indicators written back to stock_history
    |
    v
compute_signals() -- 7-section scoring engine writes signal columns + recommendation
    |
    v
compute_backtest_stats() -- simulated returns stored in stock_stats
```

### Trading Loop Flow

```
1. Ensure Robinhood auth token is valid (refresh 5 min before expiry)
2. Check if market is open
3. Fetch account info, portfolio holdings, all watchlist stocks
4. For each stock:
   a. Extract position data from Robinhood
   b. Fetch current price + intraday bars from Massive API
   c. Compute RSI, VWAP, 50-day and 200-day moving averages
   d. Fetch analyst ratings from Robinhood
   e. Check PDT (pattern day trade) restrictions
5. Send all enriched stock data to Claude for buy/sell/hold decisions
6. Filter AI hallucinations (excluded symbols, zero-quantity trades, PDT restrictions)
7. Persist decisions to stock_analysis table and shared state
8. Execute trades via Robinhood (if market open and mode is "auto")
9. Wait for configured interval, then repeat
```

### Interval Timing

- **Market open:** `RUN_INTERVAL_SECONDS` (default 600s / 10 minutes)
- **Market closed:** `AFTER_HOURS_INTERVAL_SECONDS` (default 3600s / 1 hour) -- analysis only, no orders placed
- **On error:** 60-second retry

## How to Run

```bash
# 1. Copy config and fill in credentials
cp config.py.example config.py
# Edit config.py with your API keys and Robinhood credentials

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the application
python app.py
```

The app starts at `http://127.0.0.1:5001` (localhost only). It binds exclusively to 127.0.0.1 because the app has no authentication -- exposing it on 0.0.0.0 would give anyone on the network access to the Robinhood account.

**Legacy start methods** (`python main.py` or `python webui.py` individually) are deprecated but still functional.

## Configuration

All configuration lives in `config.py` (git-ignored). See `config.py.example` for the template.

### Credentials

| Key | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Claude LLM analysis |
| `ROBINHOOD_USERNAME` | Robinhood account username |
| `ROBINHOOD_PASSWORD` | Robinhood account password |
| `ROBINHOOD_MFA_SECRET` | Robinhood TOTP MFA secret (optional, for automated login) |
| `MASSIVE_API_KEY` | Massive (Polygon.io) API key for market data |

### Trading Parameters

| Key | Default | Description |
|---|---|---|
| `MODE` | `"demo"` | Trading mode: `demo` (no real trades), `auto` (executes trades), `manual` (logs intent only) |
| `RUN_INTERVAL_SECONDS` | `600` | Seconds between trading loop cycles when market is open |
| `AFTER_HOURS_INTERVAL_SECONDS` | `3600` | Seconds between cycles when market is closed |
| `PORTFOLIO_LIMIT` | `10` | Maximum number of stocks to hold |
| `TRADE_EXCEPTIONS` | `[]` | Symbols excluded from trading |
| `MIN_SELLING_AMOUNT_USD` | `1.0` | Minimum sell amount (set to `False` to disable) |
| `MAX_SELLING_AMOUNT_USD` | `10.0` | Maximum sell amount (set to `False` to disable) |
| `MIN_BUYING_AMOUNT_USD` | `1.0` | Minimum buy amount (set to `False` to disable) |
| `MAX_BUYING_AMOUNT_USD` | `10.0` | Maximum buy amount (set to `False` to disable) |

### AI and UI Parameters

| Key | Default | Description |
|---|---|---|
| `ANTHROPIC_MODEL_NAME` | `"claude-sonnet-4-5"` | Claude model used for trading decisions |
| `WEBUI_PORT` | `5001` | Local web UI port (not 5000 -- macOS AirPlay Receiver conflicts on port 5000) |
| `LOG_LEVEL` | `"INFO"` | Logging verbosity |

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `robin_stocks` | ~3.4.0 | Robinhood API client (positions, watchlists, orders) |
| `anthropic` | ~0.40.0 | Claude LLM API client |
| `massive` | >=1.0.0 | Polygon.io market data API client (tickers, OHLCV, snapshots) |
| `certifi` | >=2024.0.0 | SSL certificate bundle (macOS Python often lacks system root certs) |
| `pandas` | ~2.2.3 | Data manipulation for RSI/VWAP/MA calculations in Robinhood enrichment |
| `pytz` | ~2024.2 | Timezone handling for market hours detection |
| `pyotp` | ~2.9.0 | TOTP code generation for Robinhood MFA |
| `flask` | ~3.0.0 | Web framework for the local UI |
| `flask-wtf` | ~1.2.0 | CSRF protection for Flask forms |

## Security Notes

- The Flask server binds to `127.0.0.1` only -- never change this to `0.0.0.0`
- CSRF protection is enabled on all POST forms via Flask-WTF
- API endpoints that accept JSON (not browser forms) are CSRF-exempt
- `config.py` containing credentials is git-ignored
- Session secret key is randomly generated on each startup (sessions do not survive restart)
