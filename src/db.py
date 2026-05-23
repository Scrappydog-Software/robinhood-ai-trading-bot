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
    loaded_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_tickers_name   ON tickers(name);
CREATE INDEX IF NOT EXISTS idx_tickers_market ON tickers(market);
CREATE INDEX IF NOT EXISTS idx_tickers_type   ON tickers(type);
CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers(active);
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
