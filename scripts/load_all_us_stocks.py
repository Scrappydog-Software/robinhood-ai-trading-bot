#!/usr/bin/env python3
"""One-time script: Load 2-year history for all active US stocks, compute
indicators/signals, and run a current-day Haiku recommendation.

Usage:
    python scripts/load_all_us_stocks.py

Estimated cost: ~$2-3 (Haiku API for current-day recommendations)
Estimated time: ~4-5 hours (API throttling)

Steps:
  1. Fetch all active US stock tickers from SQLite
  2. For each: load 2-year daily OHLCV from Massive API
  3. Compute indicators + rule-based signals (Python, free)
  4. Batch current-day Haiku recommendation (20 stocks per call)
"""

import os
import sys
import time
import json

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.api import massive_client
from src.api import claude
from src.db import (
    init_db, _connect, upsert_stock_history, compute_indicators,
    get_stock_history_status, update_bar_recommendations,
)
from src.utils import logger

HAIKU_BATCH_SIZE = 20


def get_active_us_tickers():
    """Return list of active US stock ticker symbols."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT ticker FROM tickers WHERE market = 'stocks' AND active = 1 "
            "ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    return [r['ticker'] for r in rows]


def load_history_for_all(symbols):
    """Load 2-year history for all symbols, skipping those already loaded."""
    total = len(symbols)
    loaded = 0
    skipped = 0
    errors = 0

    for i, symbol in enumerate(symbols, 1):
        try:
            status = get_stock_history_status(symbol)
            if status['bar_count'] >= 400:
                skipped += 1
                if i % 500 == 0:
                    logger.info(f"History: {i}/{total} processed ({loaded} loaded, {skipped} skipped, {errors} errors)")
                continue

            bars = massive_client.fetch_daily_bars(symbol, days=730)
            if bars:
                upsert_stock_history(symbol, bars)
                compute_indicators(symbol)
                loaded += 1
            else:
                errors += 1

            if i % 100 == 0:
                logger.info(f"History: {i}/{total} processed ({loaded} loaded, {skipped} skipped, {errors} errors)")

        except Exception as e:
            errors += 1
            logger.error(f"History: error for {symbol}: {e}")
            continue

    logger.info(f"History: DONE. {loaded} loaded, {skipped} already had data, {errors} errors out of {total}")
    return loaded


def get_latest_bar(symbol):
    """Get the most recent bar for a symbol."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT bar_date, open, high, low, close, volume, vwap "
            "FROM stock_history WHERE symbol = ? ORDER BY bar_date DESC LIMIT 1",
            (symbol,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def run_haiku_current_day(symbols):
    """Run Haiku recommendation on the latest bar for each stock, in batches."""
    from anthropic import Anthropic
    haiku_client = Anthropic(api_key=claude.client.api_key)

    total = len(symbols)
    analyzed = 0
    errors = 0

    # Build list of symbols with their latest bar
    work = []
    for symbol in symbols:
        bar = get_latest_bar(symbol)
        if bar:
            work.append({'symbol': symbol, 'bar': bar})

    logger.info(f"Haiku: {len(work)} stocks with data, running in batches of {HAIKU_BATCH_SIZE}...")

    for i in range(0, len(work), HAIKU_BATCH_SIZE):
        batch = work[i:i + HAIKU_BATCH_SIZE]

        bars_data = []
        for item in batch:
            bars_data.append({
                'symbol': item['symbol'],
                'date': item['bar']['bar_date'],
                'open': item['bar']['open'],
                'high': item['bar']['high'],
                'low': item['bar']['low'],
                'close': item['bar']['close'],
                'volume': item['bar']['volume'],
                'vwap': item['bar']['vwap'],
            })

        prompt = (
            f"You are analyzing the most recent trading day for {len(batch)} stocks.\n\n"
            f"For each stock below, provide a buy/sell/hold recommendation "
            f"based on the OHLCV data.\n\n"
            f"**Data:**\n```json\n{json.dumps(bars_data, indent=1)}\n```\n\n"
            f"**Response Format:**\nReturn a JSON array with one entry per stock:\n"
            f'[{{"symbol": "TICKER", "date": "YYYY-MM-DD", "recommendation": "strong_buy|buy|hold|sell|strong_sell"}}]\n\n'
            f"Provide only the JSON output with no additional text."
        )

        try:
            resp = haiku_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            decisions = claude.parse_ai_response(ai_text)

            for d in decisions:
                if isinstance(d, dict) and d.get('symbol') and d.get('recommendation'):
                    update_bar_recommendations(d['symbol'], [{
                        'bar_date': d.get('date') or bars_data[0]['date'],
                        'recommendation': d['recommendation'],
                    }])
                    analyzed += 1

        except Exception as e:
            errors += 1
            logger.error(f"Haiku: batch error at {i}: {e}")

        batch_num = i // HAIKU_BATCH_SIZE + 1
        total_batches = (len(work) + HAIKU_BATCH_SIZE - 1) // HAIKU_BATCH_SIZE
        if batch_num % 10 == 0 or batch_num == total_batches:
            logger.info(f"Haiku: batch {batch_num}/{total_batches} done ({analyzed} analyzed, {errors} errors)")

    logger.info(f"Haiku: DONE. {analyzed} stocks analyzed, {errors} batch errors")
    return analyzed


def main():
    init_db()

    logger.info("=" * 60)
    logger.info("LOAD ALL US STOCKS — 2-year history + current-day Haiku")
    logger.info("=" * 60)

    # Step 1: Get all tickers
    symbols = get_active_us_tickers()
    logger.info(f"Found {len(symbols)} active US stock tickers")

    if not symbols:
        logger.error("No tickers found. Run 'Load Tickers' from the Research page first.")
        return

    # Step 2: Load 2-year history + compute indicators/signals
    logger.info("\n--- Step 1/2: Loading 2-year history + computing signals ---")
    start = time.time()
    loaded = load_history_for_all(symbols)
    elapsed = time.time() - start
    logger.info(f"History step took {elapsed/60:.1f} minutes")

    # Step 3: Current-day Haiku recommendation
    logger.info("\n--- Step 2/2: Running Haiku current-day recommendations ---")
    start = time.time()
    analyzed = run_haiku_current_day(symbols)
    elapsed = time.time() - start
    logger.info(f"Haiku step took {elapsed/60:.1f} minutes")

    logger.info("=" * 60)
    logger.info("COMPLETE")
    logger.info(f"  Stocks processed: {len(symbols)}")
    logger.info(f"  History loaded: {loaded}")
    logger.info(f"  Haiku analyzed: {analyzed}")
    logger.info("=" * 60)


if __name__ == '__main__':
    main()
