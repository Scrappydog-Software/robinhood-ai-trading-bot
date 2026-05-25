#!/usr/bin/env python3
"""Load 5 years of daily OHLCV history for all quality stocks.

Restartable: skips stocks that already have >= 1200 bars (5 years).
Uses 15 parallel workers for ~3-4 minute runtime on ~3000 stocks.

Usage:
    python scripts/load_5yr_history.py
    python scripts/load_5yr_history.py --workers 10
    python scripts/load_5yr_history.py --force  # reload even if already loaded
"""

import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.api.massive_client import get_client, fetch_daily_bars
from src.db import (
    init_db, _connect, _write_lock, upsert_stock_history,
    compute_indicators, get_stock_history_status,
)
from src.utils import logger

MAX_WORKERS = 15
DAYS = 1826  # 5 years


def _load_one(symbol):
    """Fetch 5-year history for a single stock and store in DB."""
    try:
        bars = fetch_daily_bars(symbol, days=DAYS)
        if bars:
            upsert_stock_history(symbol, bars)
            return (symbol, len(bars), None)
        return (symbol, 0, 'no data returned')
    except Exception as e:
        return (symbol, 0, str(e)[:100])


def main():
    parser = argparse.ArgumentParser(description='Load 5-year history for quality stocks')
    parser.add_argument('--workers', type=int, default=MAX_WORKERS, help=f'Parallel workers (default: {MAX_WORKERS})')
    parser.add_argument('--force', action='store_true', help='Reload even if already have 5yr data')
    parser.add_argument('--compute', action='store_true', help='Recompute indicators after loading')
    parser.add_argument('--min-volume', type=float, default=250000, help='Min avg volume filter')
    parser.add_argument('--min-mcap', type=float, default=25000000, help='Min market cap filter')
    args = parser.parse_args()

    init_db()

    # Get filtered universe
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT DISTINCT s.symbol, s.history_bars FROM stock_stats s "
            "JOIN tickers t ON s.symbol = t.ticker "
            "LEFT JOIN (SELECT symbol, AVG(volume) as avg_vol FROM stock_history "
            "  WHERE volume > 0 GROUP BY symbol) v ON s.symbol = v.symbol "
            "WHERE t.primary_exchange IN ('XNYS', 'XNAS') "
            "AND (t.market_cap IS NULL OR t.market_cap > ?) "
            "AND (v.avg_vol IS NULL OR v.avg_vol >= ?) "
            "ORDER BY s.symbol",
            (args.min_mcap, args.min_volume)
        ).fetchall()
    finally:
        conn.close()

    all_symbols = [(r['symbol'], r['history_bars'] or 0) for r in rows]

    # Filter: skip stocks already with 5yr data (>= 1200 bars) unless --force
    if args.force:
        symbols = [s for s, _ in all_symbols]
        logger.info(f"Load5yr: FORCE mode — loading all {len(symbols)} stocks")
    else:
        symbols = [s for s, bars in all_symbols if bars < 1200]
        skipped = len(all_symbols) - len(symbols)
        logger.info(f"Load5yr: {len(symbols)} stocks need 5yr data ({skipped} already loaded, skipping)")

    if not symbols:
        logger.info("Load5yr: all stocks already have 5yr history. Use --force to reload.")
        return

    logger.info(f"Load5yr: loading 5-year history for {len(symbols)} stocks with {args.workers} workers...")
    start_time = time.time()
    loaded = 0
    errors = 0
    total_bars = 0

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(_load_one, s): s for s in symbols}

        for i, future in enumerate(as_completed(futures), 1):
            symbol, bars_count, error = future.result()

            if error:
                errors += 1
            else:
                loaded += 1
                total_bars += bars_count

            if i % 100 == 0 or i == len(symbols):
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed > 0 else 0
                eta = (len(symbols) - i) / rate / 60 if rate > 0 else 0
                logger.info(
                    f"Load5yr: {i}/{len(symbols)} "
                    f"(loaded={loaded}, errors={errors}, "
                    f"{rate:.1f}/sec, ETA {eta:.1f}min)"
                )

    elapsed = time.time() - start_time
    logger.info(
        f"Load5yr: download complete in {elapsed/60:.1f}min. "
        f"{loaded}/{len(symbols)} loaded ({total_bars:,} total bars), {errors} errors."
    )

    # Optionally recompute indicators
    if args.compute:
        logger.info(f"Load5yr: recomputing indicators for {loaded} stocks...")
        recompute_start = time.time()
        conn = _connect()
        try:
            updated = conn.execute(
                "SELECT DISTINCT symbol FROM stock_history WHERE symbol IN "
                f"({','.join(['?']*len(symbols))})",
                symbols
            ).fetchall()
        finally:
            conn.close()

        for i, row in enumerate(updated, 1):
            compute_indicators(row['symbol'])
            if i % 500 == 0 or i == len(updated):
                logger.info(f"Load5yr: indicators {i}/{len(updated)} done")

        logger.info(f"Load5yr: indicators complete in {(time.time()-recompute_start)/60:.1f}min")

    print(f"\n{'='*60}")
    print(f"5-YEAR HISTORY LOAD COMPLETE")
    print(f"{'='*60}")
    print(f"  Stocks loaded:    {loaded}")
    print(f"  Total bars:       {total_bars:,}")
    print(f"  Errors:           {errors}")
    print(f"  Time:             {elapsed/60:.1f} minutes")
    print(f"  Rate:             {loaded/elapsed:.1f} stocks/sec")
    if args.compute:
        print(f"  Indicators:       recomputed")
    else:
        print(f"  Indicators:       run with --compute flag to recompute")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
