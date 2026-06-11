#!/usr/bin/env python3
"""Compare execution timing: next-day open vs same-day close (30-min early signals).

Hypothesis: If we compute signals 30 minutes before market close (3:30 PM),
the indicators would be nearly identical to end-of-day values (prices barely
move in the last 30 min). We could then execute a market order before close,
avoiding the overnight gap.

This test compares two approaches using the portfolio backtest framework:
1. CURRENT MODEL: Signal after close → execute at NEXT DAY's open
2. EARLY SIGNAL: Signal ~30 min before close → execute at same-day close

With daily bars, approach #2 is approximated by: compute signal on today's
completed bar and execute at today's close. The approximation is valid because
the 3:30 PM price is highly correlated with the 4:00 PM close (median
difference <0.3% for liquid stocks).

Usage:
    python scripts/test_early_signal_execution.py
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


def run_backtest(all_bars, bars_by_date, all_dates, spy_by_date,
                 execution_mode='next_day_open',
                 initial_capital=100000, buy_pct=0.02, max_positions=50):
    """Run portfolio backtest with configurable execution timing.

    execution_mode:
        'next_day_open' — current model: yesterday's signal, today's open
        'same_day_close' — early signal: today's signal, today's close
    """
    cash = initial_capital
    positions = {}
    trades = []
    peak_value = initial_capital
    daily_values = []

    for i, date in enumerate(all_dates):
        # Update current prices
        for sym, pos in list(positions.items()):
            bar = bars_by_date.get((sym, date))
            if bar and bar.get('close'):
                pos.current_price = bar['close']

        # Portfolio value
        invested_value = sum(p.value for p in positions.values())
        portfolio_value = cash + invested_value
        if portfolio_value > peak_value:
            peak_value = portfolio_value

        daily_values.append({
            'date': date,
            'portfolio_value': portfolio_value,
        })

        # --- Determine signal date and execution price ---
        if execution_mode == 'next_day_open':
            # Current model: check PREVIOUS day's signal, execute at today's open
            prev_date = all_dates[i - 1] if i > 0 else None
            signal_date = prev_date
            get_buy_price = lambda sym, d=date: (bars_by_date.get((sym, d)) or {}).get('open')
            get_sell_price = lambda sym, d=date: (bars_by_date.get((sym, d)) or {}).get('open')
        else:
            # Early signal model: check TODAY's signal, execute at today's close
            signal_date = date
            get_buy_price = lambda sym, d=date: (bars_by_date.get((sym, d)) or {}).get('close')
            get_sell_price = lambda sym, d=date: (bars_by_date.get((sym, d)) or {}).get('close')

        if not signal_date:
            continue

        # --- SELL LOGIC ---
        for sym in list(positions.keys()):
            sig_bar = bars_by_date.get((sym, signal_date))
            if not sig_bar:
                continue
            rec = (sig_bar.get('signal_synthesis') or '').lower()
            if rec in ('sell', 'strong_sell'):
                pos = positions.pop(sym)
                sell_price = get_sell_price(sym) or pos.current_price
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

        # --- BUY LOGIC (regime-aware) ---
        regime = 'unknown'
        min_score = 2
        min_cash_reserve = 0.0

        spy_bar = spy_by_date.get(signal_date)
        regime = _detect_market_regime(spy_bar)

        if regime == 'bull' or regime == 'recovery':
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
                sig_bar = bars_by_date.get((sym, signal_date))
                if not sig_bar:
                    continue
                rec = (sig_bar.get('signal_synthesis') or '').lower()
                score = sig_bar.get('signal_score', 0)
                if rec in ('buy', 'strong_buy') and score >= min_score:
                    buy_price = get_buy_price(sym)
                    if buy_price and buy_price > 0:
                        buy_candidates.append((score, sym, buy_price))

            buy_candidates.sort(reverse=True, key=lambda x: x[0])

            for score, sym, buy_price in buy_candidates:
                available_cash = cash - (portfolio_value * min_cash_reserve)
                if available_cash < position_size or len(positions) >= max_positions:
                    break
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

    # Max drawdown
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

    # CAGR
    if daily_values and len(daily_values) > 252:
        years = len(daily_values) / 252
        cagr = ((final_value / initial_capital) ** (1 / years) - 1) * 100
    else:
        years = len(daily_values) / 252 if daily_values else 1
        cagr = total_return_pct / years if years > 0 else 0

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
    }


def main():
    init_db()

    # Get stocks with sufficient history (same filters as portfolio backtest)
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

    # Load and compute signals for all symbols
    print("Loading history and computing signals...")
    all_bars = {}
    for i, sym in enumerate(symbols):
        bars = get_stock_history_bars(sym)
        if bars and len(bars) >= 400:
            bars = compute_signals_for_bars(bars)
            all_bars[sym] = bars
        if (i + 1) % 100 == 0:
            print(f"  Loaded {i+1}/{len(symbols)} stocks...")

    print(f"Loaded {len(all_bars)} stocks with signals")

    # Load SPY for regime detection
    spy_bars = get_stock_history_bars('SPY')
    if spy_bars:
        spy_bars = compute_signals_for_bars(spy_bars)
        spy_by_date = {b['bar_date']: b for b in spy_bars}
    else:
        spy_by_date = {}
        print("WARNING: SPY not available, regime detection disabled")

    # Build unified date timeline
    all_dates = sorted(set(
        bar['bar_date'] for bars in all_bars.values() for bar in bars
    ))

    # Index bars by date
    bars_by_date = {}
    for sym, bars in all_bars.items():
        for bar in bars:
            bars_by_date[(sym, bar['bar_date'])] = bar

    # Start when SMA200 is available (need ~200 bars warmup)
    start_date = all_dates[200] if len(all_dates) > 200 else all_dates[0]
    all_dates = [d for d in all_dates if d >= start_date]
    print(f"Backtesting from {all_dates[0]} to {all_dates[-1]} ({len(all_dates)} trading days)")

    # --- Run both models ---
    print(f"\n{'='*70}")
    print("Running CURRENT MODEL: signal after close → execute next-day open...")
    result_nextday = run_backtest(
        all_bars, bars_by_date, all_dates, spy_by_date,
        execution_mode='next_day_open'
    )

    print("Running EARLY SIGNAL MODEL: signal 30-min before close → execute at close...")
    result_sameday = run_backtest(
        all_bars, bars_by_date, all_dates, spy_by_date,
        execution_mode='same_day_close'
    )

    # --- Compare results ---
    print(f"\n{'='*70}")
    print(f"EXECUTION TIMING COMPARISON")
    print(f"Period: {all_dates[0]} to {all_dates[-1]} ({result_nextday['years']} years)")
    print(f"Universe: {len(all_bars)} stocks | $100K initial | 2% position size | 50 max positions")
    print(f"{'='*70}")
    print(f"")
    print(f"{'Metric':<25} {'Next-Day Open':<18} {'Same-Day Close':<18} {'Difference'}")
    print(f"{'─'*75}")

    metrics = [
        ('CAGR', 'cagr', '%'),
        ('Total Return', 'total_return_pct', '%'),
        ('Final Value', 'final_value', '$'),
        ('Max Drawdown', 'max_drawdown_pct', '%'),
        ('Total Trades', 'total_trades', ''),
        ('Win Rate', 'win_rate_pct', '%'),
        ('Avg Win', 'avg_win_pct', '%'),
        ('Avg Loss', 'avg_loss_pct', '%'),
        ('Avg Bars Held', 'avg_bars_held', ''),
        ('Positions Open', 'positions_open', ''),
    ]

    for label, key, unit in metrics:
        v1 = result_nextday[key]
        v2 = result_sameday[key]
        diff = v2 - v1

        if unit == '$':
            col1 = f"${v1:,.0f}"
            col2 = f"${v2:,.0f}"
            col3 = f"${diff:+,.0f}"
        elif unit == '%':
            col1 = f"{v1:+.1f}%" if v1 < 0 or key != 'win_rate_pct' else f"{v1:.1f}%"
            col2 = f"{v2:+.1f}%" if v2 < 0 or key != 'win_rate_pct' else f"{v2:.1f}%"
            col3 = f"{diff:+.1f}%"
        else:
            col1 = f"{v1}"
            col2 = f"{v2}"
            col3 = f"{diff:+.0f}"

        print(f"{label:<25} {col1:<18} {col2:<18} {col3}")

    print(f"{'='*70}")
    print()

    cagr_diff = result_sameday['cagr'] - result_nextday['cagr']
    if cagr_diff > 0.5:
        print(f"CONCLUSION: Same-day close execution OUTPERFORMS next-day open by {cagr_diff:+.1f}% CAGR")
        print(f"  → Computing signals 30 min early and executing before close captures")
        print(f"    favorable overnight gaps that otherwise benefit the buyer at open.")
    elif cagr_diff < -0.5:
        print(f"CONCLUSION: Next-day open execution OUTPERFORMS same-day close by {abs(cagr_diff):.1f}% CAGR")
        print(f"  → The overnight gap tends to be favorable for our buy signals,")
        print(f"    meaning stocks that trigger buy signals tend to gap DOWN overnight")
        print(f"    (or the open price provides a better entry than close).")
    else:
        print(f"CONCLUSION: Negligible difference ({cagr_diff:+.1f}% CAGR)")
        print(f"  → Execution timing (close vs next-day open) doesn't materially")
        print(f"    impact returns. The signal quality matters more than the entry price.")

    print()
    print("NOTE: This approximation uses today's close as proxy for the 3:30 PM price.")
    print("In practice, the 3:30 PM price differs from close by <0.3% for liquid stocks.")
    print("The real benefit of early signals would be guaranteed same-day execution")
    print("(avoiding scenarios where next-day open gaps significantly against you).")


if __name__ == '__main__':
    main()
