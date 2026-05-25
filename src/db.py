"""SQLite database layer for market data.

Thin wrapper around stdlib ``sqlite3``.  The database file lives at
``data/market.db`` (relative to the project root).  WAL journal mode is
enabled for better concurrent-read performance, and a threading.Lock
guards all write operations so the Flask request thread and any background
loader can coexist safely.
"""

import os
import sqlite3
import threading

from .utils import logger

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
_DB_PATH = os.path.join(_DB_DIR, 'market.db')

# ---------------------------------------------------------------------------
# Write lock — one writer at a time across threads.
# ---------------------------------------------------------------------------
_write_lock = threading.Lock()


def _connect():
    """Open a new connection to the SQLite database.

    Callers are responsible for closing the connection. WAL mode is set on
    every connection so readers never block writers.
    """
    os.makedirs(_DB_DIR, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
_SCHEMA_SQL = """\
CREATE TABLE IF NOT EXISTS tickers (
    ticker              TEXT PRIMARY KEY,
    name                TEXT,
    market              TEXT,
    locale              TEXT,
    type                TEXT,
    active              INTEGER,
    currency_name       TEXT,
    currency_symbol     TEXT,
    base_currency_symbol TEXT,
    base_currency_name  TEXT,
    cik                 TEXT,
    composite_figi      TEXT,
    share_class_figi    TEXT,
    primary_exchange    TEXT,
    last_updated_utc    TEXT,
    delisted_utc        TEXT,
    source_feed         TEXT,
    market_cap          REAL,
    loaded_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tickers_name   ON tickers(name);
CREATE INDEX IF NOT EXISTS idx_tickers_market ON tickers(market);
CREATE INDEX IF NOT EXISTS idx_tickers_type   ON tickers(type);
CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers(active);

CREATE TABLE IF NOT EXISTS stock_analysis (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    analyzed_at     TEXT NOT NULL,
    decision        TEXT NOT NULL,
    quantity        REAL,
    rationale       TEXT,
    price           REAL,
    rsi             REAL,
    vwap            REAL,
    ma_50           REAL,
    ma_200          REAL,
    analyst_summary TEXT,
    held_quantity   REAL,
    held_avg_price  REAL,
    source          TEXT NOT NULL DEFAULT 'loop'
);
CREATE INDEX IF NOT EXISTS idx_stock_analysis_symbol_date ON stock_analysis(symbol, analyzed_at);

CREATE TABLE IF NOT EXISTS stock_history (
    symbol    TEXT NOT NULL,
    bar_date  TEXT NOT NULL,
    open      REAL,
    high      REAL,
    low       REAL,
    close     REAL,
    volume    INTEGER,
    vwap      REAL,
    transactions INTEGER,
    recommendation TEXT,
    sma_10    REAL,
    sma_20    REAL,
    sma_50    REAL,
    sma_200   REAL,
    ema_12    REAL,
    ema_26    REAL,
    rsi_14    REAL,
    macd_line REAL,
    macd_signal REAL,
    macd_histogram REAL,
    bb_upper  REAL,
    bb_lower  REAL,
    bb_width  REAL,
    vol_sma_20 REAL,
    vol_ratio REAL,
    obv       REAL,
    signal_ma TEXT,
    signal_rsi TEXT,
    signal_macd TEXT,
    signal_rsi_macd TEXT,
    signal_bb TEXT,
    signal_volume TEXT,
    signal_synthesis TEXT,
    signal_score INTEGER,
    loaded_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (symbol, bar_date)
);
CREATE INDEX IF NOT EXISTS idx_stock_history_symbol ON stock_history(symbol);

CREATE TABLE IF NOT EXISTS stock_stats (
    symbol              TEXT PRIMARY KEY,
    backtest_return_pct REAL,
    backtest_trades     INTEGER,
    backtest_final      REAL,
    bt_1yr_return_pct   REAL,
    bt_1yr_trades       INTEGER,
    bt_1yr_final        REAL,
    latest_signal       TEXT,
    latest_score        INTEGER,
    history_bars        INTEGER,
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

_MIGRATION_SQL = """\
ALTER TABLE stock_history ADD COLUMN recommendation TEXT;
ALTER TABLE stock_history ADD COLUMN sma_10 REAL;
ALTER TABLE stock_history ADD COLUMN sma_20 REAL;
ALTER TABLE stock_history ADD COLUMN sma_50 REAL;
ALTER TABLE stock_history ADD COLUMN sma_200 REAL;
ALTER TABLE stock_history ADD COLUMN ema_12 REAL;
ALTER TABLE stock_history ADD COLUMN ema_26 REAL;
ALTER TABLE stock_history ADD COLUMN rsi_14 REAL;
ALTER TABLE stock_history ADD COLUMN macd_line REAL;
ALTER TABLE stock_history ADD COLUMN macd_signal REAL;
ALTER TABLE stock_history ADD COLUMN macd_histogram REAL;
ALTER TABLE stock_history ADD COLUMN bb_upper REAL;
ALTER TABLE stock_history ADD COLUMN bb_lower REAL;
ALTER TABLE stock_history ADD COLUMN bb_width REAL;
ALTER TABLE stock_history ADD COLUMN vol_sma_20 REAL;
ALTER TABLE stock_history ADD COLUMN vol_ratio REAL;
ALTER TABLE stock_history ADD COLUMN obv REAL;
ALTER TABLE stock_history ADD COLUMN signal_ma TEXT;
ALTER TABLE stock_history ADD COLUMN signal_rsi TEXT;
ALTER TABLE stock_history ADD COLUMN signal_macd TEXT;
ALTER TABLE stock_history ADD COLUMN signal_rsi_macd TEXT;
ALTER TABLE stock_history ADD COLUMN signal_bb TEXT;
ALTER TABLE stock_history ADD COLUMN signal_volume TEXT;
ALTER TABLE stock_history ADD COLUMN signal_synthesis TEXT;
ALTER TABLE stock_history ADD COLUMN signal_score INTEGER;
CREATE TABLE IF NOT EXISTS stock_stats (symbol TEXT PRIMARY KEY, backtest_return_pct REAL, backtest_trades INTEGER, backtest_final REAL, bt_1yr_return_pct REAL, bt_1yr_trades INTEGER, bt_1yr_final REAL, latest_signal TEXT, latest_score INTEGER, history_bars INTEGER, updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')));
ALTER TABLE stock_stats ADD COLUMN bt_1yr_return_pct REAL;
ALTER TABLE stock_stats ADD COLUMN bt_1yr_trades INTEGER;
ALTER TABLE stock_stats ADD COLUMN bt_1yr_final REAL;
ALTER TABLE stock_stats ADD COLUMN history_bars INTEGER;
ALTER TABLE tickers ADD COLUMN market_cap REAL;
"""


def init_db():
    """Create the schema if it does not already exist.

    Safe to call repeatedly (uses IF NOT EXISTS).  Should be called once
    from app.py's main() before Flask starts.
    """
    conn = _connect()
    try:
        with _write_lock:
            conn.executescript(_SCHEMA_SQL)
            for stmt in _MIGRATION_SQL.strip().split(';'):
                stmt = stmt.strip()
                if stmt:
                    try:
                        conn.execute(stmt)
                    except Exception:
                        pass
            conn.commit()
        logger.info("DB: schema initialised (data/market.db)")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def upsert_tickers(rows):
    """Batch INSERT OR REPLACE rows into the tickers table.

    ``rows`` is an iterable of dicts whose keys match column names.  Missing
    keys are filled with None.  ``loaded_at`` is set automatically by the
    database default.

    Returns the number of rows written.
    """
    cols = [
        'ticker', 'name', 'market', 'locale', 'type', 'active',
        'currency_name', 'currency_symbol', 'base_currency_symbol',
        'base_currency_name', 'cik', 'composite_figi', 'share_class_figi',
        'primary_exchange', 'last_updated_utc', 'delisted_utc', 'source_feed',
        'market_cap',
    ]
    placeholders = ', '.join(['?'] * len(cols))
    sql = f"INSERT OR REPLACE INTO tickers ({', '.join(cols)}) VALUES ({placeholders})"

    conn = _connect()
    count = 0
    try:
        with _write_lock:
            cursor = conn.cursor()
            for row in rows:
                values = tuple(row.get(c) for c in cols)
                cursor.execute(sql, values)
                count += 1
            conn.commit()
    finally:
        conn.close()
    return count


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def search_tickers(query=None, market=None, type_=None, active=None,
                   limit=50, offset=0):
    """Search / filter / paginate the tickers table.

    ``query`` performs a case-insensitive LIKE match on ticker and name.
    ``market``, ``type_``, and ``active`` are exact-match filters.

    Returns a list of sqlite3.Row objects.
    """
    clauses = []
    params = []

    if query:
        clauses.append("(ticker LIKE ? OR name LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if market:
        clauses.append("market = ?")
        params.append(market)
    if type_:
        clauses.append("type = ?")
        params.append(type_)
    if active is not None:
        clauses.append("active = ?")
        params.append(int(active))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM tickers{where} ORDER BY ticker LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return rows


def get_ticker_count(query=None, market=None, type_=None, active=None):
    """Return total count matching the same filters as search_tickers."""
    clauses = []
    params = []

    if query:
        clauses.append("(ticker LIKE ? OR name LIKE ?)")
        like = f"%{query}%"
        params.extend([like, like])
    if market:
        clauses.append("market = ?")
        params.append(market)
    if type_:
        clauses.append("type = ?")
        params.append(type_)
    if active is not None:
        clauses.append("active = ?")
        params.append(int(active))

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT COUNT(*) FROM tickers{where}"

    conn = _connect()
    try:
        (count,) = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return count


def insert_stock_analysis(row):
    """Insert a single row into the stock_analysis table.

    ``row`` is a dict whose keys match column names.  ``id`` and default
    values are handled by the database.

    Returns the rowid of the inserted record.
    """
    cols = [
        'symbol', 'analyzed_at', 'decision', 'quantity', 'rationale',
        'price', 'rsi', 'vwap', 'ma_50', 'ma_200', 'analyst_summary',
        'held_quantity', 'held_avg_price', 'source',
    ]
    placeholders = ', '.join(['?'] * len(cols))
    sql = f"INSERT INTO stock_analysis ({', '.join(cols)}) VALUES ({placeholders})"

    conn = _connect()
    try:
        with _write_lock:
            cursor = conn.cursor()
            values = tuple(row.get(c) for c in cols)
            cursor.execute(sql, values)
            conn.commit()
            return cursor.lastrowid
    finally:
        conn.close()


def upsert_stock_history(symbol, bars):
    """Batch INSERT OR REPLACE OHLCV bars into the stock_history table.

    ``bars`` is an iterable of dicts with keys: bar_date, open, high, low,
    close, volume, vwap, transactions.  ``symbol`` is prepended to each row.

    Returns the number of rows written.
    """
    cols = [
        'symbol', 'bar_date', 'open', 'high', 'low', 'close',
        'volume', 'vwap', 'transactions',
    ]
    placeholders = ', '.join(['?'] * len(cols))
    sql = f"INSERT OR REPLACE INTO stock_history ({', '.join(cols)}) VALUES ({placeholders})"

    conn = _connect()
    count = 0
    try:
        with _write_lock:
            cursor = conn.cursor()
            for bar in bars:
                values = (
                    symbol,
                    bar.get('bar_date'),
                    bar.get('open'),
                    bar.get('high'),
                    bar.get('low'),
                    bar.get('close'),
                    bar.get('volume'),
                    bar.get('vwap'),
                    bar.get('transactions'),
                )
                cursor.execute(sql, values)
                count += 1
            conn.commit()
    finally:
        conn.close()
    return count


def get_stock_history_status(symbol):
    """Return history status for a symbol.

    Returns a dict: {has_data, bar_count, earliest_date, latest_date}.
    """
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS bar_count, MIN(bar_date) AS earliest_date, "
            "MAX(bar_date) AS latest_date FROM stock_history WHERE symbol = ?",
            (symbol.upper(),)
        ).fetchone()
    finally:
        conn.close()

    bar_count = row['bar_count'] if row else 0
    return {
        'has_data': bar_count > 0,
        'bar_count': bar_count,
        'earliest_date': row['earliest_date'] if row else None,
        'latest_date': row['latest_date'] if row else None,
    }


def get_stock_history_bars(symbol):
    """Return all daily OHLCV bars + indicators + signals for a symbol, oldest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT bar_date, open, high, low, close, volume, vwap, transactions, "
            "recommendation, sma_10, sma_20, sma_50, sma_200, ema_12, ema_26, "
            "rsi_14, macd_line, macd_signal, macd_histogram, "
            "bb_upper, bb_lower, bb_width, vol_sma_20, vol_ratio, obv, "
            "signal_ma, signal_rsi, signal_macd, signal_rsi_macd, "
            "signal_bb, signal_volume, signal_synthesis, signal_score "
            "FROM stock_history WHERE symbol = ? ORDER BY bar_date ASC",
            (symbol.upper(),)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_unanalyzed_bars(symbol):
    """Return bars without a recommendation, oldest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT bar_date, open, high, low, close, volume, vwap "
            "FROM stock_history WHERE symbol = ? AND (recommendation IS NULL OR recommendation = '') "
            "ORDER BY bar_date ASC",
            (symbol.upper(),)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_all_bars_for_analysis(symbol):
    """Return ALL bars for a symbol with indicators, oldest first (for full recompute)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT bar_date, open, high, low, close, volume, vwap, "
            "sma_10, sma_50, sma_200, rsi_14, "
            "macd_line, macd_signal, macd_histogram, "
            "bb_upper, bb_lower, bb_width, vol_ratio, obv "
            "FROM stock_history WHERE symbol = ? "
            "ORDER BY bar_date ASC",
            (symbol.upper(),)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def update_bar_recommendations(symbol, updates):
    """Batch update recommendation column for specific bar_dates.

    ``updates`` is a list of dicts: [{'bar_date': '2024-01-02', 'recommendation': 'buy'}, ...]
    """
    conn = _connect()
    try:
        with _write_lock:
            for u in updates:
                conn.execute(
                    "UPDATE stock_history SET recommendation = ? WHERE symbol = ? AND bar_date = ?",
                    (u['recommendation'], symbol.upper(), u['bar_date'])
                )
            conn.commit()
    finally:
        conn.close()
    return len(updates)


def get_stock_analysis_history(symbol, limit=50):
    """Return recent analysis rows for a symbol, newest first."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM stock_analysis WHERE symbol = ? "
            "ORDER BY analyzed_at DESC LIMIT ?",
            (symbol.upper(), limit)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def compute_indicators(symbol):
    """Compute all technical indicators for a symbol's history bars.

    Reads OHLCV data, computes SMA/EMA/RSI/MACD/Bollinger/Volume
    indicators, and writes them back to the database. Pure math —
    no API calls.

    Returns the number of bars updated.
    """
    symbol = symbol.upper()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT bar_date, close, high, low, volume FROM stock_history "
            "WHERE symbol = ? ORDER BY bar_date ASC",
            (symbol,)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    dates = [r['bar_date'] for r in rows]
    closes = [r['close'] or 0 for r in rows]
    highs = [r['high'] or 0 for r in rows]
    lows = [r['low'] or 0 for r in rows]
    volumes = [r['volume'] or 0 for r in rows]
    n = len(closes)

    def _sma(data, period):
        out = [None] * n
        for i in range(period - 1, n):
            out[i] = sum(data[i - period + 1:i + 1]) / period
        return out

    def _ema(data, period):
        out = [None] * n
        if n < period:
            return out
        k = 2 / (period + 1)
        avg = sum(data[:period]) / period
        out[period - 1] = avg
        for i in range(period, n):
            avg = data[i] * k + avg * (1 - k)
            out[i] = avg
        return out

    sma10 = _sma(closes, 10)
    sma20 = _sma(closes, 20)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)

    # RSI(14)
    rsi = [None] * n
    if n > 14:
        gains = []
        losses = []
        for i in range(1, n):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        avg_g = sum(gains[:14]) / 14
        avg_l = sum(losses[:14]) / 14
        rsi[14] = 100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)
        for i in range(14, len(gains)):
            avg_g = (avg_g * 13 + gains[i]) / 14
            avg_l = (avg_l * 13 + losses[i]) / 14
            rsi[i + 1] = 100 if avg_l == 0 else 100 - 100 / (1 + avg_g / avg_l)

    # MACD
    macd_l = [None] * n
    for i in range(n):
        if ema12[i] is not None and ema26[i] is not None:
            macd_l[i] = ema12[i] - ema26[i]
    macd_valid = [x for x in macd_l if x is not None]
    macd_sig = [None] * n
    macd_hist = [None] * n
    if len(macd_valid) >= 9:
        k = 2 / 10
        offset = n - len(macd_valid)
        avg = sum(macd_valid[:9]) / 9
        macd_sig[offset + 8] = avg
        for j in range(9, len(macd_valid)):
            avg = macd_valid[j] * k + avg * (1 - k)
            macd_sig[offset + j] = avg
    for i in range(n):
        if macd_l[i] is not None and macd_sig[i] is not None:
            macd_hist[i] = macd_l[i] - macd_sig[i]

    # Bollinger Bands
    bb_upper = [None] * n
    bb_lower = [None] * n
    bb_width = [None] * n
    for i in range(n):
        if sma20[i] is not None:
            window = closes[max(0, i - 19):i + 1]
            std = (sum((x - sma20[i]) ** 2 for x in window) / len(window)) ** 0.5
            bb_upper[i] = sma20[i] + 2 * std
            bb_lower[i] = sma20[i] - 2 * std
            bb_width[i] = (bb_upper[i] - bb_lower[i]) / sma20[i] * 100 if sma20[i] else None

    # Volume indicators
    vol_sma = _sma(volumes, 20)
    vol_ratio = [None] * n
    for i in range(n):
        if vol_sma[i] and vol_sma[i] > 0:
            vol_ratio[i] = volumes[i] / vol_sma[i]

    # OBV
    obv = [0.0] * n
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            obv[i] = obv[i - 1] + volumes[i]
        elif closes[i] < closes[i - 1]:
            obv[i] = obv[i - 1] - volumes[i]
        else:
            obv[i] = obv[i - 1]

    # Write back
    conn = _connect()
    try:
        with _write_lock:
            for i in range(n):
                conn.execute(
                    "UPDATE stock_history SET "
                    "sma_10=?, sma_20=?, sma_50=?, sma_200=?, "
                    "ema_12=?, ema_26=?, rsi_14=?, "
                    "macd_line=?, macd_signal=?, macd_histogram=?, "
                    "bb_upper=?, bb_lower=?, bb_width=?, "
                    "vol_sma_20=?, vol_ratio=?, obv=? "
                    "WHERE symbol=? AND bar_date=?",
                    (
                        _r(sma10[i]), _r(sma20[i]), _r(sma50[i]), _r(sma200[i]),
                        _r(ema12[i]), _r(ema26[i]), _r(rsi[i]),
                        _r(macd_l[i]), _r(macd_sig[i]), _r(macd_hist[i]),
                        _r(bb_upper[i]), _r(bb_lower[i]), _r(bb_width[i]),
                        _r(vol_sma[i]), _r(vol_ratio[i]), round(obv[i], 2),
                        symbol, dates[i],
                    )
                )
            conn.commit()
    finally:
        conn.close()

    logger.info(f"DB: computed indicators for {symbol} ({n} bars)")

    # Also compute rule-based signals now that indicators are fresh
    compute_signals(symbol)

    return n


def _r(val, decimals=4):
    """Round a value if not None."""
    return round(val, decimals) if val is not None else None


def get_loaded_at():
    """Return the most recent loaded_at timestamp, or None if empty."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(loaded_at) FROM tickers"
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else None


def get_ticker_by_symbol(symbol):
    """Return a single ticker row by exact symbol match, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM tickers WHERE ticker = ?", (symbol.upper(),)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return dict(row)


def compute_signals(symbol):
    """Run the rule-based signal scoring engine on a symbol's history.

    Reads all bars with indicators, computes 7-section signal scores,
    and writes results (signal_ma through signal_score) back to the DB.
    Replaces the LLM-based recommendation with pure Python math.

    Returns the number of bars scored.
    """
    from src.signals import compute_signals_for_bars

    symbol = symbol.upper()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT bar_date, open, high, low, close, volume, vwap, "
            "sma_10, sma_20, sma_50, sma_200, ema_12, ema_26, "
            "rsi_14, macd_line, macd_signal, macd_histogram, "
            "bb_upper, bb_lower, bb_width, vol_sma_20, vol_ratio, obv "
            "FROM stock_history WHERE symbol = ? ORDER BY bar_date ASC",
            (symbol,)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return 0

    bars = [dict(r) for r in rows]
    bars = compute_signals_for_bars(bars)

    conn = _connect()
    try:
        with _write_lock:
            for bar in bars:
                conn.execute(
                    "UPDATE stock_history SET "
                    "signal_ma=?, signal_rsi=?, signal_macd=?, signal_rsi_macd=?, "
                    "signal_bb=?, signal_volume=?, signal_synthesis=?, signal_score=?, "
                    "recommendation=? "
                    "WHERE symbol=? AND bar_date=?",
                    (
                        bar.get('signal_ma'),
                        bar.get('signal_rsi'),
                        bar.get('signal_macd'),
                        bar.get('signal_rsi_macd'),
                        bar.get('signal_bb'),
                        bar.get('signal_volume'),
                        bar.get('signal_synthesis'),
                        bar.get('signal_score'),
                        bar.get('signal_synthesis'),
                        symbol,
                        bar['bar_date'],
                    )
                )
            conn.commit()
    finally:
        conn.close()

    logger.info(f"DB: computed signals for {symbol} ({len(bars)} bars)")

    # Compute and store backtest return stats
    compute_backtest_stats(symbol)

    return len(bars)


def _run_backtest(rows):
    """Run a backtest simulation on a list of bar rows.

    Returns (return_pct, trades, final_value).
    """
    if not rows:
        return 0.0, 0, 100.0

    INITIAL = 100.0
    capital = INITIAL
    shares_held = 0.0
    state = 'waiting'
    trades = 0

    for row in rows:
        sig = (row['signal_synthesis'] or '').lower()
        if state == 'waiting' and sig in ('buy', 'strong_buy'):
            close_price = row['close'] or 0
            if close_price > 0 and capital > 0:
                shares_held = capital / close_price
                state = 'holding'
        elif state == 'holding' and sig in ('sell', 'strong_sell'):
            open_price = row['open'] or 0
            capital = shares_held * open_price
            shares_held = 0.0
            state = 'waiting'
            trades += 1

    if state == 'holding' and rows:
        final = shares_held * (rows[-1]['close'] or 0)
    else:
        final = capital

    return_pct = round((final - INITIAL) / INITIAL * 100, 1) if INITIAL > 0 else 0
    return return_pct, trades, round(final, 2)


def compute_backtest_stats(symbol):
    """Run backtest simulations and store 1-year and 2-year results.

    Always computes a 1-year return (last 252 bars) and a full-history
    return (all available bars, labeled as 2-year if >= 400 bars).
    Stores both in stock_stats so the UI can show accurate labels.

    Called automatically after compute_signals.
    """
    symbol = symbol.upper()
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT bar_date, open, close, signal_synthesis, signal_score "
            "FROM stock_history WHERE symbol = ? ORDER BY bar_date ASC",
            (symbol,)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return

    rows = [dict(r) for r in rows]

    # Full history backtest
    full_pct, full_trades, full_final = _run_backtest(rows)

    # 1-year backtest (last 252 trading days)
    one_year_bars = rows[-252:] if len(rows) > 252 else rows
    yr1_pct, yr1_trades, yr1_final = _run_backtest(one_year_bars)

    # 2-year return is only valid if we have >= 400 bars (~1.5+ years)
    has_2yr = len(rows) >= 400
    bt_2yr_pct = full_pct if has_2yr else None
    bt_2yr_trades = full_trades if has_2yr else None
    bt_2yr_final = full_final if has_2yr else None

    # Latest signal
    latest_signal = None
    latest_score = None
    for row in reversed(rows):
        sig = (row.get('signal_synthesis') or '').lower()
        if sig and sig != 'neutral':
            latest_signal = sig
            latest_score = row.get('signal_score')
            break

    # Store
    conn = _connect()
    try:
        with _write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO stock_stats "
                "(symbol, backtest_return_pct, backtest_trades, backtest_final, "
                "bt_1yr_return_pct, bt_1yr_trades, bt_1yr_final, "
                "latest_signal, latest_score, history_bars, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))",
                (symbol, bt_2yr_pct, bt_2yr_trades, bt_2yr_final,
                 yr1_pct, yr1_trades, yr1_final,
                 latest_signal, latest_score, len(rows))
            )
            conn.commit()
    finally:
        conn.close()

    label_2yr = f" | 2yr: {bt_2yr_pct}%" if has_2yr else ""
    logger.info(f"DB: backtest stats for {symbol}: 1yr: {yr1_pct}% ({yr1_trades} trades){label_2yr}")
    return yr1_pct


def get_stock_stats(symbol):
    """Return stock_stats row for a symbol, or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM stock_stats WHERE symbol = ?",
            (symbol.upper(),)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_all_stock_stats():
    """Return all stock_stats rows as a dict keyed by symbol."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM stock_stats").fetchall()
    finally:
        conn.close()
    return {row['symbol']: dict(row) for row in rows}


def get_distinct_values(column):
    """Return sorted distinct non-null values for a column (for filter dropdowns)."""
    # Whitelist to prevent SQL injection
    allowed = {'market', 'type', 'active'}
    if column not in allowed:
        return []
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM tickers WHERE {column} IS NOT NULL ORDER BY {column}"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def get_screener_stocks():
    """Return all stocks from stock_stats joined with tickers for the screener.

    Includes average daily volume for filtering. Default filters in the UI
    require avg_volume >= 250K and history_bars >= 400.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT s.symbol, t.name, s.latest_signal, s.latest_score, "
            "s.bt_1yr_return_pct, s.backtest_return_pct, s.bt_1yr_trades, "
            "s.backtest_trades, s.history_bars, t.market_cap, t.primary_exchange, "
            "v.avg_vol "
            "FROM stock_stats s "
            "LEFT JOIN tickers t ON s.symbol = t.ticker "
            "LEFT JOIN (SELECT symbol, CAST(AVG(volume) AS INTEGER) as avg_vol "
            "  FROM stock_history WHERE volume > 0 GROUP BY symbol) v ON s.symbol = v.symbol "
            "ORDER BY s.symbol"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_latest_bar_date(symbol):
    """Return the most recent bar_date for a symbol, or None if no history."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(bar_date) FROM stock_history WHERE symbol = ?",
            (symbol.upper(),)
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row and row[0] else None


def get_daily_closes(symbol, limit=200):
    """Return the last N daily close prices for a symbol (oldest first).

    Used for computing moving averages from cached history without
    re-fetching from the API.
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT close FROM stock_history WHERE symbol = ? AND close IS NOT NULL "
            "ORDER BY bar_date DESC LIMIT ?",
            (symbol.upper(), limit)
        ).fetchall()
    finally:
        conn.close()
    # Reverse so oldest is first
    return [r['close'] for r in reversed(rows)]
