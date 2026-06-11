#!/usr/bin/env python3
"""Compare daily signals vs 15-minute intraday signals.

DAILY approach: compute signals on daily bars, execute at close (buy) or open (sell).
INTRADAY approach: compute signals on 15-min bars throughout the day,
execute market order at the moment the signal fires.

This measures whether faster signal detection (15-min resolution) improves
returns vs waiting for end-of-day signals.

Usage:
    python scripts/test_intraday_signals.py
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
from src.signals import (
    compute_signals_for_bars, score_ma, score_rsi, score_macd,
    score_rsi_macd, score_bb, score_volume, _synthesis_label
)
from src.utils import logger


def load_15min_bars(symbol, from_date, to_date):
    """Load 15-minute bars from Massive API for a date range."""
    client = get_client()
    _throttle()
    try:
        aggs = client.get_aggs(symbol, 15, "minute", from_date, to_date, limit=50000)
        bars = []
        for agg in aggs:
            ts = datetime.fromtimestamp(agg.timestamp / 1000)
            bars.append({
                'bar_time': ts.strftime('%Y-%m-%d %H:%M'),
                'bar_date': ts.strftime('%Y-%m-%d'),
                'open': getattr(agg, 'open', None),
                'high': getattr(agg, 'high', None),
                'low': getattr(agg, 'low', None),
                'close': getattr(agg, 'close', None),
                'volume': getattr(agg, 'volume', None),
            })
        return bars
    except Exception as e:
        logger.error(f"Error loading 15min bars for {symbol}: {e}")
        return []


def compute_intraday_indicators(bars):
    """Compute indicators on 15-min bars suitable for signal scoring.

    Uses shorter periods appropriate for intraday:
    - SMA equivalent: 10-bar (2.5 hours), 50-bar (12.5 hours), 200-bar (3+ days)
    - RSI: 14 bars (3.5 hours)
    - MACD: standard 12/26/9
    - Volume: 20-bar average
    """
    n = len(bars)
    closes = [b['close'] or 0 for b in bars]
    volumes = [b['volume'] or 0 for b in bars]
    highs = [b['high'] or 0 for b in bars]
    lows = [b['low'] or 0 for b in bars]

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

    # RSI
    rsi = [None] * n
    if n > 14:
        gains, losses = [], []
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

    # Volume
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

    # Assign to bars
    for i in range(n):
        bars[i]['sma_10'] = sma10[i]
        bars[i]['sma_20'] = sma20[i]
        bars[i]['sma_50'] = sma50[i]
        bars[i]['sma_200'] = sma200[i]
        bars[i]['ema_12'] = ema12[i]
        bars[i]['ema_26'] = ema26[i]
        bars[i]['rsi_14'] = rsi[i]
        bars[i]['macd_line'] = macd_l[i]
        bars[i]['macd_signal'] = macd_sig[i]
        bars[i]['macd_histogram'] = macd_hist[i]
        bars[i]['bb_upper'] = bb_upper[i]
        bars[i]['bb_lower'] = bb_lower[i]
        bars[i]['bb_width'] = bb_width[i]
        bars[i]['vol_sma_20'] = vol_sma[i]
        bars[i]['vol_ratio'] = vol_ratio[i]
        bars[i]['obv'] = obv[i]

    return bars


def backtest_daily(daily_bars):
    """Standard daily backtest: buy at close on signal, sell at open on signal."""
    capital = 100.0
    shares = 0.0
    state = 'waiting'
    trades = 0

    for bar in daily_bars:
        close = bar.get('close') or 0
        open_p = bar.get('open') or close
        rec = (bar.get('signal_synthesis') or '').lower()

        if state == 'waiting' and rec in ('buy', 'strong_buy') and close > 0:
            shares = capital / close
            state = 'holding'
        elif state == 'holding' and rec in ('sell', 'strong_sell'):
            capital = shares * open_p
            shares = 0.0
            state = 'waiting'
            trades += 1

    final = capital if state == 'waiting' else shares * (daily_bars[-1].get('close') or 0)
    return round((final - 100) / 100 * 100, 1), trades


def backtest_intraday(intraday_bars):
    """Intraday backtest: compute signals on 15-min bars, execute at signal bar's close."""
    capital = 100.0
    shares = 0.0
    state = 'waiting'
    trades = 0
    buy_price = 0.0
    bars_held = 0

    for i, bar in enumerate(intraday_bars):
        if i < 200:  # need warmup for SMA200
            continue

        close = bar.get('close') or 0
        rec = (bar.get('signal_synthesis') or '').lower()

        if state == 'holding':
            bars_held += 1

        if state == 'waiting' and rec in ('buy', 'strong_buy') and close > 0:
            shares = capital / close
            buy_price = close
            bars_held = 0
            state = 'holding'
        elif state == 'holding' and rec in ('sell', 'strong_sell'):
            capital = shares * close  # execute at signal bar's close (market order)
            shares = 0.0
            state = 'waiting'
            trades += 1

    final = capital if state == 'waiting' else shares * (intraday_bars[-1].get('close') or 0)
    return round((final - 100) / 100 * 100, 1), trades


def main():
    init_db()

    # Test stocks with high trade activity
    test_symbols = ['ABBV', 'AEM', 'ABT', 'KR', 'DY', 'CI', 'ARMK', 'BILI']

    # Use a 3-month window for intraday (API returns a lot of data)
    from_date = '2026-02-01'
    to_date = '2026-05-21'

    print(f"Comparing DAILY vs INTRADAY (15-min) signals")
    print(f"Period: {from_date} to {to_date}")
    print(f"Stocks: {', '.join(test_symbols)}")
    print(f"\nLoading data...")

    results = []

    for sym in test_symbols:
        # Daily backtest (use existing data + signals)
        daily_bars = get_stock_history_bars(sym)
        if not daily_bars or len(daily_bars) < 50:
            continue

        # Filter daily to same period
        daily_period = [b for b in daily_bars if from_date <= b['bar_date'] <= to_date]
        daily_period = compute_signals_for_bars(daily_period)
        daily_ret, daily_trades = backtest_daily(daily_period)

        # Intraday: load 15-min bars
        print(f"  {sym}: loading 15-min bars...")
        intraday_bars = load_15min_bars(sym, from_date, to_date)
        if not intraday_bars or len(intraday_bars) < 250:
            print(f"  {sym}: insufficient intraday data ({len(intraday_bars)} bars)")
            continue

        # Compute indicators and signals on 15-min bars
        intraday_bars = compute_intraday_indicators(intraday_bars)
        intraday_bars = compute_signals_for_bars(intraday_bars)
        intra_ret, intra_trades = backtest_intraday(intraday_bars)

        results.append({
            'symbol': sym,
            'daily_ret': daily_ret,
            'daily_trades': daily_trades,
            'intra_ret': intra_ret,
            'intra_trades': intra_trades,
            'intra_bars': len(intraday_bars),
        })
        print(f"  {sym}: daily={daily_ret:+.1f}% ({daily_trades}t)  intraday={intra_ret:+.1f}% ({intra_trades}t)  [{len(intraday_bars)} 15-min bars]")

    print(f"\n{'='*70}")
    print(f"DAILY vs INTRADAY SIGNAL COMPARISON ({from_date} to {to_date})")
    print(f"Daily: signals at end of day, execute at close/open")
    print(f"Intraday: signals every 15 min, execute market order at signal time")
    print(f"{'='*70}")
    print(f"{'Symbol':<8} {'Daily Ret':<11} {'Daily Trd':<10} {'Intra Ret':<11} {'Intra Trd':<10} {'Diff'}")

    total_daily = 0
    total_intra = 0
    for r in results:
        diff = r['intra_ret'] - r['daily_ret']
        print(f"{r['symbol']:<8} {r['daily_ret']:>+8.1f}%  {r['daily_trades']:>7}    {r['intra_ret']:>+8.1f}%  {r['intra_trades']:>7}    {diff:>+.1f}%")
        total_daily += r['daily_ret']
        total_intra += r['intra_ret']

    if results:
        print(f"\n{'─'*70}")
        avg_daily = total_daily / len(results)
        avg_intra = total_intra / len(results)
        print(f"{'AVG':<8} {avg_daily:>+8.1f}%             {avg_intra:>+8.1f}%             {avg_intra-avg_daily:>+.1f}%")
        print(f"\nConclusion: Intraday signals {'outperform' if avg_intra > avg_daily else 'underperform'} daily by {abs(avg_intra-avg_daily):.1f}% over {(datetime.strptime(to_date,'%Y-%m-%d') - datetime.strptime(from_date,'%Y-%m-%d')).days} days")
    print(f"{'='*70}")


if __name__ == '__main__':
    main()
