#!/usr/bin/env python3
"""Test dynamic historical performance filters for buy eligibility.

The hypothesis: even when a stock triggers a buy signal, its historical
performance context matters. A stock in a long-term downtrend or with
consistently poor signal outcomes should be excluded from buys.

Filters tested:
1. 12-MONTH MOMENTUM: Stock must have positive 12-month price return
2. 6-MONTH MOMENTUM: Stock must have positive 6-month price return
3. RELATIVE STRENGTH: Stock must be above median 6-month return in the universe
4. ABOVE SMA200: Stock must be trading above its 200-day SMA
5. 52-WEEK DRAWDOWN: Stock must be within 25% of its 52-week high
6. SIGNAL WIN RATE: Stock's prior signals must have >35% win rate (last 1yr)
7. COMBINED: momentum + SMA200 + drawdown (best of the above)

Each filter is tested against the baseline (no filter) using the full
portfolio backtest with regime-aware buying.

Usage:
    python scripts/test_performance_filters.py
"""

import os
import sys
from datetime import datetime

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.db import init_db, _connect, get_stock_history_bars
from src.signals import compute_signals_for_bars
from src.utils import logger


class Position:
    def __init__(self, symbol, buy_price, shares, buy_date):
        self.symbol = symbol
        self.buy_price = buy_price
        self.shares = shares
        self.buy_date = buy_date
        self.current_price = buy_price

    @property
    def value(self):
        return self.shares * self.current_price

    @property
    def pnl_pct(self):
        if self.buy_price == 0:
            return 0
        return (self.current_price - self.buy_price) / self.buy_price * 100

    @property
    def cost_basis(self):
        return self.shares * self.buy_price


def _detect_market_regime(spy_bar):
    if not spy_bar:
        return 'unknown'
    close = spy_bar.get('close') or 0
    sma50 = spy_bar.get('sma_50')
    sma200 = spy_bar.get('sma_200')
    if not close or not sma50 or not sma200:
        return 'unknown'
    if close > sma50 and sma50 > sma200:
        return 'bull'
    elif close < sma50 and sma50 < sma200:
        return 'bear'
    elif close < sma50:
        return 'correction'
    else:
        return 'recovery'


def _check_filter(filter_name, sym, date, bars_by_date, all_dates, signal_history):
    """Check if a stock passes the given performance filter.

    Returns True if the stock is ELIGIBLE to buy, False if filtered out.
    """
    date_idx = all_dates.index(date) if date in all_dates else -1
    if date_idx < 0:
        return True

    bar = bars_by_date.get((sym, date))
    if not bar:
        return True

    close = bar.get('close') or 0
    if close <= 0:
        return True

    if filter_name == 'none':
        return True

    elif filter_name == '12mo_momentum':
        # Stock must have positive 12-month return
        lookback = 252
        if date_idx < lookback:
            return True
        past_date = all_dates[date_idx - lookback]
        past_bar = bars_by_date.get((sym, past_date))
        if not past_bar or not past_bar.get('close'):
            return True
        return close > past_bar['close']

    elif filter_name == '6mo_momentum':
        # Stock must have positive 6-month return
        lookback = 126
        if date_idx < lookback:
            return True
        past_date = all_dates[date_idx - lookback]
        past_bar = bars_by_date.get((sym, past_date))
        if not past_bar or not past_bar.get('close'):
            return True
        return close > past_bar['close']

    elif filter_name == '3mo_momentum':
        # Stock must have positive 3-month return
        lookback = 63
        if date_idx < lookback:
            return True
        past_date = all_dates[date_idx - lookback]
        past_bar = bars_by_date.get((sym, past_date))
        if not past_bar or not past_bar.get('close'):
            return True
        return close > past_bar['close']

    elif filter_name == 'above_sma200':
        # Stock must be above its 200-day SMA
        sma200 = bar.get('sma_200')
        if not sma200:
            return True
        return close > sma200

    elif filter_name == '52wk_drawdown_25':
        # Stock must be within 25% of 52-week high
        lookback = min(252, date_idx)
        if lookback < 50:
            return True
        high_52wk = 0
        for j in range(date_idx - lookback, date_idx + 1):
            if j < 0:
                continue
            d = all_dates[j]
            b = bars_by_date.get((sym, d))
            if b and b.get('high'):
                high_52wk = max(high_52wk, b['high'])
        if high_52wk <= 0:
            return True
        drawdown_pct = (close - high_52wk) / high_52wk * 100
        return drawdown_pct > -25

    elif filter_name == '52wk_drawdown_15':
        # Stock must be within 15% of 52-week high
        lookback = min(252, date_idx)
        if lookback < 50:
            return True
        high_52wk = 0
        for j in range(date_idx - lookback, date_idx + 1):
            if j < 0:
                continue
            d = all_dates[j]
            b = bars_by_date.get((sym, d))
            if b and b.get('high'):
                high_52wk = max(high_52wk, b['high'])
        if high_52wk <= 0:
            return True
        drawdown_pct = (close - high_52wk) / high_52wk * 100
        return drawdown_pct > -15

    elif filter_name == 'signal_winrate':
        # Stock's prior signals must have >35% win rate in the last year
        history = signal_history.get(sym, [])
        # Filter to last 252 trading days
        recent = [h for h in history if h['exit_date'] and h['exit_date'] >= all_dates[max(0, date_idx - 252)]]
        if len(recent) < 3:
            return True  # Not enough history to judge
        wins = sum(1 for h in recent if h['pnl_pct'] > 0)
        return (wins / len(recent)) > 0.35

    elif filter_name == 'combined_momentum_sma':
        # 6-month momentum + above SMA200
        # Momentum check
        lookback = 126
        if date_idx >= lookback:
            past_date = all_dates[date_idx - lookback]
            past_bar = bars_by_date.get((sym, past_date))
            if past_bar and past_bar.get('close'):
                if close <= past_bar['close']:
                    return False
        # SMA200 check
        sma200 = bar.get('sma_200')
        if sma200 and close <= sma200:
            return False
        return True

    elif filter_name == 'combined_all':
        # 6-month momentum + above SMA200 + within 25% of 52wk high
        # Momentum
        lookback = 126
        if date_idx >= lookback:
            past_date = all_dates[date_idx - lookback]
            past_bar = bars_by_date.get((sym, past_date))
            if past_bar and past_bar.get('close'):
                if close <= past_bar['close']:
                    return False
        # SMA200
        sma200 = bar.get('sma_200')
        if sma200 and close <= sma200:
            return False
        # 52wk drawdown
        lookback_52 = min(252, date_idx)
        if lookback_52 >= 50:
            high_52wk = 0
            for j in range(date_idx - lookback_52, date_idx + 1):
                if j < 0:
                    continue
                d = all_dates[j]
                b = bars_by_date.get((sym, d))
                if b and b.get('high'):
                    high_52wk = max(high_52wk, b['high'])
            if high_52wk > 0:
                drawdown_pct = (close - high_52wk) / high_52wk * 100
                if drawdown_pct < -25:
                    return False
        return True

    elif filter_name == 'winrate_and_12mo':
        # Signal win rate >35% AND 12-month positive momentum
        # Win rate check
        history = signal_history.get(sym, [])
        recent = [h for h in history if h['exit_date'] and h['exit_date'] >= all_dates[max(0, date_idx - 252)]]
        if len(recent) >= 3:
            wins = sum(1 for h in recent if h['pnl_pct'] > 0)
            if (wins / len(recent)) <= 0.35:
                return False
        # 12-month momentum
        lookback = 252
        if date_idx >= lookback:
            past_date = all_dates[date_idx - lookback]
            past_bar = bars_by_date.get((sym, past_date))
            if past_bar and past_bar.get('close'):
                if close <= past_bar['close']:
                    return False
        return True

    elif filter_name == 'winrate_and_6mo':
        # Signal win rate >35% AND 6-month positive momentum
        history = signal_history.get(sym, [])
        recent = [h for h in history if h['exit_date'] and h['exit_date'] >= all_dates[max(0, date_idx - 252)]]
        if len(recent) >= 3:
            wins = sum(1 for h in recent if h['pnl_pct'] > 0)
            if (wins / len(recent)) <= 0.35:
                return False
        lookback = 126
        if date_idx >= lookback:
            past_date = all_dates[date_idx - lookback]
            past_bar = bars_by_date.get((sym, past_date))
            if past_bar and past_bar.get('close'):
                if close <= past_bar['close']:
                    return False
        return True

    elif filter_name == 'winrate_and_12mo_and_dd25':
        # Signal win rate >35% AND 12-month momentum AND within 25% of 52wk high
        history = signal_history.get(sym, [])
        recent = [h for h in history if h['exit_date'] and h['exit_date'] >= all_dates[max(0, date_idx - 252)]]
        if len(recent) >= 3:
            wins = sum(1 for h in recent if h['pnl_pct'] > 0)
            if (wins / len(recent)) <= 0.35:
                return False
        # 12-month momentum
        lookback = 252
        if date_idx >= lookback:
            past_date = all_dates[date_idx - lookback]
            past_bar = bars_by_date.get((sym, past_date))
            if past_bar and past_bar.get('close'):
                if close <= past_bar['close']:
                    return False
        # 52wk drawdown
        lookback_52 = min(252, date_idx)
        if lookback_52 >= 50:
            high_52wk = 0
            for j in range(date_idx - lookback_52, date_idx + 1):
                if j < 0:
                    continue
                d = all_dates[j]
                b = bars_by_date.get((sym, d))
                if b and b.get('high'):
                    high_52wk = max(high_52wk, b['high'])
            if high_52wk > 0:
                drawdown_pct = (close - high_52wk) / high_52wk * 100
                if drawdown_pct < -25:
                    return False
        return True

    return True


def run_filtered_backtest(all_bars, bars_by_date, all_dates, spy_by_date,
                          filter_name='none',
                          initial_capital=100000, buy_pct=0.02, max_positions=50):
    """Portfolio backtest with a performance filter applied before buying."""

    cash = initial_capital
    positions = {}
    trades = []
    peak_value = initial_capital
    daily_values = []
    filtered_count = 0
    total_candidates = 0

    # Track signal history for win-rate filter
    signal_history = {}  # symbol -> list of {entry_date, exit_date, pnl_pct}

    for i, date in enumerate(all_dates):
        # Update prices
        for sym, pos in list(positions.items()):
            bar = bars_by_date.get((sym, date))
            if bar and bar.get('close'):
                pos.current_price = bar['close']

        invested_value = sum(p.value for p in positions.values())
        portfolio_value = cash + invested_value
        if portfolio_value > peak_value:
            peak_value = portfolio_value

        daily_values.append({'date': date, 'portfolio_value': portfolio_value})

        # Signal date = previous day (signals computed after close)
        prev_date = all_dates[i - 1] if i > 0 else None
        if not prev_date:
            continue

        # --- SELL LOGIC ---
        for sym in list(positions.keys()):
            prev_bar = bars_by_date.get((sym, prev_date))
            today_bar = bars_by_date.get((sym, date))
            if not prev_bar:
                continue
            rec = (prev_bar.get('signal_synthesis') or '').lower()
            if rec in ('sell', 'strong_sell'):
                pos = positions.pop(sym)
                sell_price = (today_bar.get('open') if today_bar else None) or pos.current_price
                proceeds = pos.shares * sell_price
                pnl = proceeds - pos.cost_basis
                pnl_pct = (sell_price - pos.buy_price) / pos.buy_price * 100
                cash += proceeds
                bars_held = len([d for d in all_dates if pos.buy_date <= d <= date])
                trades.append({
                    'symbol': sym, 'action': 'sell', 'date': date,
                    'price': sell_price, 'pnl': pnl, 'pnl_pct': pnl_pct,
                    'bars_held': bars_held,
                })
                # Record for signal history
                if sym not in signal_history:
                    signal_history[sym] = []
                signal_history[sym].append({
                    'entry_date': pos.buy_date,
                    'exit_date': date,
                    'pnl_pct': pnl_pct,
                })

        # --- BUY LOGIC (regime-aware + performance filter) ---
        regime = 'unknown'
        min_score = 2
        min_cash_reserve = 0.0

        spy_bar = spy_by_date.get(prev_date)
        regime = _detect_market_regime(spy_bar)

        if regime in ('bull', 'recovery'):
            min_score = 2
            min_cash_reserve = 0.05
        elif regime == 'correction':
            min_score = 5
            min_cash_reserve = 0.25
        elif regime == 'bear':
            min_score = 7
            min_cash_reserve = 0.50

        position_size = portfolio_value * buy_pct
        available_cash = cash - (portfolio_value * min_cash_reserve)

        if available_cash >= position_size and len(positions) < max_positions:
            buy_candidates = []
            for sym in all_bars:
                if sym in positions:
                    continue
                prev_bar = bars_by_date.get((sym, prev_date))
                if not prev_bar:
                    continue
                rec = (prev_bar.get('signal_synthesis') or '').lower()
                score = prev_bar.get('signal_score', 0)
                if rec in ('buy', 'strong_buy') and score >= min_score:
                    total_candidates += 1
                    # Apply performance filter on the SIGNAL DATE
                    if _check_filter(filter_name, sym, prev_date, bars_by_date,
                                     all_dates, signal_history):
                        today_bar = bars_by_date.get((sym, date))
                        if today_bar and today_bar.get('open'):
                            buy_candidates.append((score, sym, today_bar))
                    else:
                        filtered_count += 1

            buy_candidates.sort(reverse=True, key=lambda x: x[0])

            for score, sym, today_bar in buy_candidates:
                available_cash = cash - (portfolio_value * min_cash_reserve)
                if available_cash < position_size or len(positions) >= max_positions:
                    break
                buy_price = today_bar.get('open') or 0
                if buy_price <= 0:
                    continue
                shares = position_size / buy_price
                positions[sym] = Position(sym, buy_price, shares, date)
                cash -= position_size
                trades.append({
                    'symbol': sym, 'action': 'buy', 'date': date,
                    'price': buy_price, 'pnl': None, 'pnl_pct': None,
                    'bars_held': None,
                })

    # Summary
    final_value = cash + sum(p.value for p in positions.values())
    total_return_pct = (final_value - initial_capital) / initial_capital * 100

    running_peak = initial_capital
    max_drawdown = 0
    for dv in daily_values:
        if dv['portfolio_value'] > running_peak:
            running_peak = dv['portfolio_value']
        dd = (dv['portfolio_value'] - running_peak) / running_peak * 100
        if dd < max_drawdown:
            max_drawdown = dd

    sell_trades = [t for t in trades if t['action'] == 'sell']
    winning = [t for t in sell_trades if t['pnl'] and t['pnl'] > 0]
    losing = [t for t in sell_trades if t['pnl'] and t['pnl'] <= 0]
    win_rate = len(winning) / len(sell_trades) * 100 if sell_trades else 0
    avg_win = sum(t['pnl_pct'] for t in winning) / len(winning) if winning else 0
    avg_loss = sum(t['pnl_pct'] for t in losing) / len(losing) if losing else 0
    avg_bars = sum(t['bars_held'] for t in sell_trades if t['bars_held']) / len(sell_trades) if sell_trades else 0

    years = len(daily_values) / 252 if daily_values else 1
    if years > 1:
        cagr = ((final_value / initial_capital) ** (1 / years) - 1) * 100
    else:
        cagr = total_return_pct / years if years > 0 else 0

    filter_rate = filtered_count / total_candidates * 100 if total_candidates > 0 else 0

    return {
        'final_value': final_value,
        'total_return_pct': round(total_return_pct, 2),
        'cagr': round(cagr, 2),
        'max_drawdown_pct': round(max_drawdown, 2),
        'total_trades': len(trades),
        'sell_trades': len(sell_trades),
        'winning_trades': len(winning),
        'losing_trades': len(losing),
        'win_rate_pct': round(win_rate, 1),
        'avg_win_pct': round(avg_win, 1),
        'avg_loss_pct': round(avg_loss, 1),
        'avg_bars_held': round(avg_bars),
        'positions_open': len(positions),
        'years': round(years, 1),
        'filtered_count': filtered_count,
        'total_candidates': total_candidates,
        'filter_rate_pct': round(filter_rate, 1),
    }


def main():
    init_db()

    # Get stocks with sufficient history
    conn = _connect()
    try:
        rows = conn.execute("""
            SELECT s.symbol
            FROM stock_stats s
            JOIN tickers t ON s.symbol = t.ticker
            WHERE s.history_bars >= 400
              AND t.primary_exchange IN ('XNYS', 'XNAS')
        """).fetchall()
    finally:
        conn.close()

    symbols = [r['symbol'] for r in rows]
    print(f"Universe: {len(symbols)} stocks (bars>=400, NYSE/NASDAQ)")

    # Load and compute signals
    print("Loading history and computing signals...")
    all_bars = {}
    for i, sym in enumerate(symbols):
        bars = get_stock_history_bars(sym)
        if bars and len(bars) >= 400:
            bars = compute_signals_for_bars(bars)
            all_bars[sym] = bars
        if (i + 1) % 500 == 0:
            print(f"  Loaded {i+1}/{len(symbols)} stocks...")

    print(f"Loaded {len(all_bars)} stocks with signals")

    # SPY for regime detection
    spy_bars = get_stock_history_bars('SPY')
    if spy_bars:
        spy_bars = compute_signals_for_bars(spy_bars)
        spy_by_date = {b['bar_date']: b for b in spy_bars}
    else:
        spy_by_date = {}

    # Build timeline
    all_dates = sorted(set(
        bar['bar_date'] for bars in all_bars.values() for bar in bars
    ))

    bars_by_date = {}
    for sym, bars in all_bars.items():
        for bar in bars:
            bars_by_date[(sym, bar['bar_date'])] = bar

    # Start after SMA200 warmup
    start_date = all_dates[200] if len(all_dates) > 200 else all_dates[0]
    all_dates = [d for d in all_dates if d >= start_date]
    print(f"Backtesting from {all_dates[0]} to {all_dates[-1]} ({len(all_dates)} trading days)")

    # --- Run all filter variants ---
    filters = [
        ('none', 'No filter (baseline)'),
        ('signal_winrate', 'Prior signal win rate >35%'),
        ('12mo_momentum', '12-month positive return'),
        ('6mo_momentum', '6-month positive return'),
        ('winrate_and_12mo', 'Win rate >35% + 12mo momentum'),
        ('winrate_and_6mo', 'Win rate >35% + 6mo momentum'),
        ('winrate_and_12mo_and_dd25', 'Win rate + 12mo + within 25% high'),
    ]

    results = {}
    for filter_name, description in filters:
        print(f"\n  Running: {description}...")
        result = run_filtered_backtest(
            all_bars, bars_by_date, all_dates, spy_by_date,
            filter_name=filter_name
        )
        results[filter_name] = result
        print(f"    CAGR: {result['cagr']:+.1f}% | Return: {result['total_return_pct']:+.1f}% | "
              f"Win: {result['win_rate_pct']:.0f}% | DD: {result['max_drawdown_pct']:.0f}% | "
              f"Filtered: {result['filter_rate_pct']:.0f}%")

    # --- Summary table ---
    baseline = results['none']
    print(f"\n{'='*100}")
    print(f"DYNAMIC PERFORMANCE FILTER COMPARISON")
    print(f"Period: {all_dates[0]} to {all_dates[-1]} ({baseline['years']} years)")
    print(f"Universe: {len(all_bars)} stocks | $100K | 2% positions | 50 max | Regime-aware")
    print(f"{'='*100}")
    print(f"{'Filter':<32} {'CAGR':<8} {'Return':<9} {'MaxDD':<8} {'WinR':<7} {'AvgW':<7} {'AvgL':<8} {'Trades':<7} {'Filt%':<7} {'vs Base'}")
    print(f"{'─'*100}")

    for filter_name, description in filters:
        r = results[filter_name]
        diff = r['cagr'] - baseline['cagr']
        diff_str = f"{diff:+.1f}%" if filter_name != 'none' else "  —"
        print(f"{description:<32} {r['cagr']:>+5.1f}%  {r['total_return_pct']:>+6.1f}%  "
              f"{r['max_drawdown_pct']:>5.1f}%  {r['win_rate_pct']:>4.1f}%  "
              f"{r['avg_win_pct']:>+4.1f}%  {r['avg_loss_pct']:>+5.1f}%  "
              f"{r['sell_trades']:>5}  {r['filter_rate_pct']:>4.1f}%  {diff_str}")

    print(f"{'='*100}")

    # Find best filter
    best_filter = max(results.items(), key=lambda x: x[1]['cagr'])
    best_name = best_filter[0]
    best_result = best_filter[1]

    print(f"\nBEST FILTER: {dict(filters)[best_name]}")
    print(f"  CAGR: {best_result['cagr']:+.1f}% (vs {baseline['cagr']:+.1f}% baseline = {best_result['cagr'] - baseline['cagr']:+.1f}% improvement)")
    print(f"  Final value: ${best_result['final_value']:,.0f} (vs ${baseline['final_value']:,.0f} baseline)")
    print(f"  Max drawdown: {best_result['max_drawdown_pct']:.1f}% (vs {baseline['max_drawdown_pct']:.1f}% baseline)")
    print(f"  Win rate: {best_result['win_rate_pct']:.1f}% (vs {baseline['win_rate_pct']:.1f}% baseline)")
    print(f"  Filter rejection rate: {best_result['filter_rate_pct']:.1f}% of buy candidates excluded")

    # Risk-adjusted comparison
    print(f"\n{'─'*100}")
    print(f"RISK-ADJUSTED (Return/MaxDD ratio — higher is better):")
    print(f"{'Filter':<32} {'Return/DD':<10}")
    print(f"{'─'*50}")
    for filter_name, description in filters:
        r = results[filter_name]
        ratio = abs(r['total_return_pct'] / r['max_drawdown_pct']) if r['max_drawdown_pct'] != 0 else 0
        marker = " ← BEST" if ratio == max(abs(results[f]['total_return_pct'] / results[f]['max_drawdown_pct']) for f, _ in filters if results[f]['max_drawdown_pct'] != 0) else ""
        print(f"{description:<32} {ratio:>6.2f}{marker}")


if __name__ == '__main__':
    main()
