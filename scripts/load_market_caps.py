#!/usr/bin/env python3
"""One-time script to populate market_cap for all tickers where it is NULL.

Usage:
    python scripts/load_market_caps.py

Requires MASSIVE_API_KEY in config.py. Uses concurrent requests (15 workers)
to parallelize API calls. Expected runtime: ~10-15 minutes for 12,000+ tickers.
"""

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.api.massive_client import get_client, fetch_ticker_details
from src.db import init_db, _connect, _write_lock
from src.utils import logger

MAX_WORKERS = 15


def _fetch_one(ticker):
    """Fetch market_cap for a single ticker. Returns (ticker, market_cap) or (ticker, None)."""
    try:
        details = fetch_ticker_details(ticker)
        if details and hasattr(details, 'market_cap') and details.market_cap:
            return (ticker, details.market_cap)
    except Exception:
        pass
    return (ticker, None)


def load_market_caps():
    """Fetch and store market_cap for all tickers where it is currently NULL."""
    init_db()

    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ticker FROM tickers WHERE market_cap IS NULL AND active = 1"
        ).fetchall()
    finally:
        conn.close()

    total = len(rows)
    if total == 0:
        logger.info("LoadMarketCaps: all tickers already have market_cap populated.")
        return 0

    logger.info(f"LoadMarketCaps: {total} tickers need market_cap. Using {MAX_WORKERS} workers...")
    tickers = [row['ticker'] for row in rows]

    updated = 0
    errors = 0
    batch = []
    BATCH_SIZE = 50
    start_time = time.time()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(_fetch_one, t): t for t in tickers}

        for i, future in enumerate(as_completed(futures), 1):
            ticker, market_cap = future.result()

            if market_cap is not None:
                batch.append((market_cap, ticker))
                updated += 1
            else:
                errors += 1

            # Flush batch to DB every BATCH_SIZE results
            if len(batch) >= BATCH_SIZE:
                conn = _connect()
                try:
                    with _write_lock:
                        conn.executemany(
                            "UPDATE tickers SET market_cap = ? WHERE ticker = ?",
                            batch
                        )
                        conn.commit()
                finally:
                    conn.close()
                batch = []

            if i % 500 == 0:
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / rate / 60 if rate > 0 else 0
                logger.info(
                    f"LoadMarketCaps: {i}/{total} "
                    f"(updated={updated}, errors={errors}, "
                    f"{rate:.1f}/sec, ETA {eta:.1f}min)"
                )

    # Flush remaining
    if batch:
        conn = _connect()
        try:
            with _write_lock:
                conn.executemany(
                    "UPDATE tickers SET market_cap = ? WHERE ticker = ?",
                    batch
                )
                conn.commit()
        finally:
            conn.close()

    elapsed = time.time() - start_time
    logger.info(
        f"LoadMarketCaps: done in {elapsed/60:.1f}min. "
        f"{updated}/{total} updated, {errors} errors."
    )
    return updated


if __name__ == '__main__':
    load_market_caps()
