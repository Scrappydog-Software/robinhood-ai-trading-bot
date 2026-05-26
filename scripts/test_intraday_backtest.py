#!/usr/bin/env python3
"""Compare daily vs 15-minute intraday entry/exit pricing impact.

Loads 15-minute bars for a set of high-trade stocks, then simulates
both approaches:
1. DAILY: buy at close on buy signal day, sell at open on sell signal day
2. INTRADAY: on a signal day, find the optimal 15-min entry/exit within that day

This measures how much price improvement we'd get from intraday execution
vs the current daily-bar approach.

Usage:
    python scripts/test_intraday_backtest.py
"""

import os
import sys
import time
from datetime import datetime, timedelta

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.api.massive_client import get_client, _throttle
from src.db import init_db, _connect, _write_lock, get_stock_history_bars
from src.signals import compute_signals_for_bars
from src.utils import logger

# Schema for intraday data
_INTRADAY_SCHEMA = """\
CREATE TABLE IF NOT EXISTS stock_history_intraday (
    symbol      TEXT NOT NULL,
    bar_time    TEXT NOT NULL,
    bar_date    TEXT NOT NULL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      INTEGER,
    vwap        REAL,
    PRIMARY KEY (symbol, bar_time)
);
CREATE INDEX IF NOT EXISTS idx_intraday_symbol_date ON stock_history_intraday(symbol, bar_date);
"""


def init_intraday_db():
    conn = _connect()
    try:
        with _write_lock:
            conn.executescript(_INTRADAY_SCHEMA)
            conn.commit()
    finally:
        conn.close()


def load_intraday_for_dates(symbol, dates):
    """Load 15-minute bars for specific dates from Massive API."""
    client = get_client()
    all_bars = []

    for date_str in dates:
        _throttle()
        try:
            aggs = client.get_aggs(symbol, 15, "minute", date_str, date_str, limit=50000)
            for agg in aggs:
                ts = datetime.fromtimestamp(agg.timestamp / 1000)
                all_bars.append({
                    'symbol': symbol,
                    'bar_time': ts.strftime('%Y-%m-%d %H:%M'),
                    'bar_date': date_str,
                    'open': getattr(agg, 'open', None),
                    'high': getattr(agg, 'high', None),
                    'low': getattr(agg, 'low', None),
                    'close': getattr(agg, 'close', None),
                    'volume': getattr(agg, 'volume', None),
                    'vwap': getattr(agg, 'vwap', None),
                })
        except Exception as e:
            logger.error(f"Error loading intraday for {symbol} on {date_str}: {e}")

    # Store in DB
    if all_bars:
        conn = _connect()
        try:
            with _write_lock:
                for bar in all_bars:
                    conn.execute(
                        "INSERT OR REPLACE INTO stock_history_intraday "
                        "(symbol, bar_time, bar_date, open, high, low, close, volume, vwap) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (bar['symbol'], bar['bar_time'], bar['bar_date'],
                         bar['open'], bar['high'], bar['low'], bar['close'],
                         bar['volume'], bar['vwap'])
                    )
                conn.commit()
        finally:
            conn.close()

    return all_bars


def get_intraday_bars(symbol, date_str):
    """Get 15-min bars for a symbol on a specific date from DB."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM stock_history_intraday WHERE symbol = ? AND bar_date = ? ORDER BY bar_time",
            (symbol, date_str)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def simulate_intraday_entry(intraday_bars, direction='buy'):
    """Find best intraday execution price.

    For BUY: find the lowest low in the first 2 hours (dip buying)
    For SELL: find the highest high in the first 2 hours (selling into strength)
    """
    if not intraday_bars:
        return None

    # Filter to first 2 hours of trading (9:30 - 11:30 ET)
    early_bars = [b for b in intraday_bars if b['bar_time'][11:16] < '11:30']
    if not early_bars:
        early_bars = intraday_bars[:8]  # fallback: first 8 bars (2 hours)

    if direction == 'buy':
        # Best buy = lowest price in the morning
        best = min(early_bars, key=lambda b: b['low'] or float('inf'))
        return best['low']
    else:
        # Best sell = highest price in the morning
        best = max(early_bars, key=lambda b: b['high'] or 0)
        return best['high']


def main():
    init_db()
    init_intraday_db()

    # Test stocks: high-trade count from our backtest
    test_symbols = ['ABBV', 'AVO', 'AEM', 'ABT', 'KR', 'DY', 'CI', 'ARMK', 'AMR', 'BILI']

    print(f"Testing daily vs intraday execution on {len(test_symbols)} stocks...")
    print(f"Loading signal data and identifying trade dates...")

    results = []

    for sym in test_symbols:
        bars = get_stock_history_bars(sym)
        if not bars or len(bars) < 200:
            continue
        bars = compute_signals_for_bars(bars)

        # Find all buy/sell signal dates
        trade_dates = []
        state = 'waiting'
        for bar in bars:
            rec = (bar.get('signal_synthesis') or '').lower()
            if state == 'waiting' and rec in ('buy', 'strong_buy'):
                trade_dates.append(('buy', bar['bar_date'], bar.get('close') or 0))
                state = 'holding'
            elif state == 'holding' and rec in ('sell', 'strong_sell'):
                trade_dates.append(('sell', bar['bar_date'], bar.get('open') or 0))
                state = 'waiting'

        if not trade_dates:
            continue

        # Load intraday data for trade dates (limit to last 2 years for API efficiency)
        recent_trades = [(action, date, price) for action, date, price in trade_dates if date >= '2024-05-01']

        if not recent_trades:
            continue

        print(f"\n  {sym}: {len(recent_trades)} trades in last 2 years, loading intraday...")
        dates_to_load = [date for _, date, _ in recent_trades]
        load_intraday_for_dates(sym, dates_to_load)

        # Compare daily vs intraday execution
        daily_pnl = 0
        intraday_pnl = 0
        trade_improvements = []

        i = 0
        while i < len(recent_trades) - 1:
            if recent_trades[i][0] != 'buy':
                i += 1
                continue
            # Find matching sell
            buy_action, buy_date, daily_buy_price = recent_trades[i]
            sell_found = False
            for j in range(i + 1, len(recent_trades)):
                if recent_trades[j][0] == 'sell':
                    sell_action, sell_date, daily_sell_price = recent_trades[j]
                    sell_found = True

                    # Daily execution
                    daily_ret = (daily_sell_price - daily_buy_price) / daily_buy_price * 100

                    # Intraday execution
                    buy_intraday = get_intraday_bars(sym, buy_date)
                    sell_intraday = get_intraday_bars(sym, sell_date)

                    intra_buy = simulate_intraday_entry(buy_intraday, 'buy') or daily_buy_price
                    intra_sell = simulate_intraday_entry(sell_intraday, 'sell') or daily_sell_price

                    intra_ret = (intra_sell - intra_buy) / intra_buy * 100
                    improvement = intra_ret - daily_ret

                    trade_improvements.append(improvement)
                    daily_pnl += daily_ret
                    intraday_pnl += intra_ret

                    i = j + 1
                    break
            if not sell_found:
                break

        if trade_improvements:
            avg_improvement = sum(trade_improvements) / len(trade_improvements)
            results.append({
                'symbol': sym,
                'trades': len(trade_improvements),
                'daily_total': round(daily_pnl, 1),
                'intraday_total': round(intraday_pnl, 1),
                'avg_improvement': round(avg_improvement, 2),
            })
            print(f"    {len(trade_improvements)} round-trips: daily={daily_pnl:+.1f}% intraday={intraday_pnl:+.1f}% (improvement: {avg_improvement:+.2f}%/trade)")

    print(f"\n{'='*70}")
    print(f"DAILY vs INTRADAY (15-min) EXECUTION COMPARISON")
    print(f"{'='*70}")
    print(f"{'Symbol':<8} {'Trades':<7} {'Daily Total':<12} {'Intraday Total':<15} {'Avg Improvement'}")

    total_daily = 0
    total_intraday = 0
    total_trades = 0
    for r in results:
        print(f"{r['symbol']:<8} {r['trades']:<7} {r['daily_total']:>+9.1f}%   {r['intraday_total']:>+9.1f}%       {r['avg_improvement']:>+.2f}%/trade")
        total_daily += r['daily_total']
        total_intraday += r['intraday_total']
        total_trades += r['trades']

    if results:
        avg_imp = (total_intraday - total_daily) / total_trades if total_trades else 0
        print(f"\n{'─'*70}")
        print(f"{'TOTAL':<8} {total_trades:<7} {total_daily:>+9.1f}%   {total_intraday:>+9.1f}%       {avg_imp:>+.2f}%/trade")
        print(f"\nConclusion: Intraday execution {'improves' if avg_imp > 0 else 'worsens'} returns by {abs(avg_imp):.2f}% per trade on average")
        if total_trades > 0:
            annual_impact = avg_imp * (total_trades / len(results)) * 4  # rough annualization
            print(f"Estimated annual impact: {annual_impact:+.1f}% (based on {total_trades/len(results):.0f} trades/stock × 4 quarters)")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
