#!/usr/bin/env python3
"""Standalone CLI script to load all tickers from the Massive API into SQLite.

Usage:
    python scripts/load_tickers.py

Requires MASSIVE_API_KEY in config.py.  Streams tickers from the API
iterator and batches inserts (1000 rows per batch) for efficiency.
"""

import os
import sys

# Ensure the project root is on sys.path so ``from config import *`` and
# ``from src.…`` imports work when running this script directly.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.api import massive_client
from src.db import init_db, upsert_tickers
from src.utils import logger

BATCH_SIZE = 1000


def _ticker_to_row(t):
    """Convert a Massive Ticker object to a dict matching the tickers table."""
    return {
        'ticker':               getattr(t, 'ticker', None),
        'name':                 getattr(t, 'name', None),
        'market':               getattr(t, 'market', None),
        'locale':               getattr(t, 'locale', None),
        'type':                 getattr(t, 'type', None),
        'active':               int(getattr(t, 'active', False)) if getattr(t, 'active', None) is not None else None,
        'currency_name':        getattr(t, 'currency_name', None),
        'currency_symbol':      getattr(t, 'currency_symbol', None),
        'base_currency_symbol': getattr(t, 'base_currency_symbol', None),
        'base_currency_name':   getattr(t, 'base_currency_name', None),
        'cik':                  getattr(t, 'cik', None),
        'composite_figi':       getattr(t, 'composite_figi', None),
        'share_class_figi':     getattr(t, 'share_class_figi', None),
        'primary_exchange':     getattr(t, 'primary_exchange', None),
        'last_updated_utc':     getattr(t, 'last_updated_utc', None),
        'delisted_utc':         getattr(t, 'delisted_utc', None),
        'source_feed':          getattr(t, 'source_feed', None),
    }


def load_tickers():
    """Fetch all tickers from Massive and upsert into SQLite."""
    init_db()

    logger.info("LoadTickers: fetching tickers from Massive API...")
    tickers_iter = massive_client.fetch_all_tickers(limit=BATCH_SIZE)

    batch = []
    total = 0
    for t in tickers_iter:
        batch.append(_ticker_to_row(t))
        if len(batch) >= BATCH_SIZE:
            count = upsert_tickers(batch)
            total += count
            logger.info(f"LoadTickers: upserted batch ({total} total so far)...")
            batch = []

    # Flush remaining
    if batch:
        count = upsert_tickers(batch)
        total += count

    logger.info(f"LoadTickers: done. {total} tickers loaded into data/market.db")
    return total


if __name__ == '__main__':
    load_tickers()
