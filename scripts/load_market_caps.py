#!/usr/bin/env python3
"""One-time script to populate market_cap for all tickers where it is NULL.

Usage:
    python scripts/load_market_caps.py

Requires MASSIVE_API_KEY in config.py.  Iterates all tickers with NULL
market_cap, fetches details from the Massive API, and updates the row.
Uses a 1-second throttle between API calls (handled by massive_client).
Logs progress every 100 tickers.
"""

import os
import sys
import time

# Ensure the project root is on sys.path so imports work when running directly.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.api import massive_client
from src.db import init_db, _connect, _write_lock
from src.utils import logger


def load_market_caps():
    """Fetch and store market_cap for all tickers where it is currently NULL."""
    init_db()

    # Get all tickers with NULL market_cap
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

    logger.info(f"LoadMarketCaps: {total} tickers need market_cap. Starting...")

    updated = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        ticker = row['ticker']

        try:
            market_cap = massive_client.fetch_market_cap(ticker)

            if market_cap is not None:
                conn = _connect()
                try:
                    with _write_lock:
                        conn.execute(
                            "UPDATE tickers SET market_cap = ? WHERE ticker = ?",
                            (market_cap, ticker)
                        )
                        conn.commit()
                finally:
                    conn.close()
                updated += 1
        except Exception as e:
            logger.error(f"LoadMarketCaps: error for {ticker}: {e}")
            errors += 1

        # Log progress every 100 tickers
        if i % 100 == 0:
            logger.info(
                f"LoadMarketCaps: progress {i}/{total} "
                f"(updated={updated}, errors={errors})"
            )

        # Throttle: 1 second between calls (massive_client._throttle handles
        # the delay internally, but we add a small safety buffer)
        time.sleep(0.1)

    logger.info(
        f"LoadMarketCaps: done. {updated}/{total} tickers updated, "
        f"{errors} errors."
    )
    return updated


if __name__ == '__main__':
    load_market_caps()
