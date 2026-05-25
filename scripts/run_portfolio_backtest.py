#!/usr/bin/env python3
"""Run a realistic portfolio-level backtest.

Usage:
    python scripts/run_portfolio_backtest.py [--capital 100000] [--buy-pct 0.02] [--max-pos 50]

Simulates a multi-stock portfolio using the signal engine's recommendations.
Results stored in portfolio_backtest_* tables.
"""

import os
import sys
import argparse

_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from src.db import init_db, _connect
from src.portfolio_backtest import run_portfolio_backtest
from src.utils import logger


def main():
    parser = argparse.ArgumentParser(description='Run portfolio-level backtest')
    parser.add_argument('--capital', type=float, default=100000, help='Initial capital (default: $100,000)')
    parser.add_argument('--buy-pct', type=float, default=0.02, help='Position size as %% of portfolio (default: 0.02 = 2%%)')
    parser.add_argument('--max-pos', type=int, default=50, help='Max simultaneous positions (default: 50)')
    parser.add_argument('--name', type=str, default=None, help='Label for this backtest run')
    parser.add_argument('--exchange', type=str, default='XNYS,XNAS', help='Exchanges to include (default: XNYS,XNAS)')
    parser.add_argument('--min-mcap', type=float, default=25000000, help='Minimum market cap (default: 25M)')
    parser.add_argument('--min-volume', type=float, default=250000, help='Minimum avg daily volume (default: 250K)')
    parser.add_argument('--min-bars', type=int, default=400, help='Minimum history bars (default: 400)')
    args = parser.parse_args()

    init_db()

    # Get universe of stocks
    exchanges = [e.strip() for e in args.exchange.split(',')]
    placeholders = ','.join(['?'] * len(exchanges))

    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT s.symbol FROM stock_stats s "
            f"JOIN tickers t ON s.symbol = t.ticker "
            f"LEFT JOIN (SELECT symbol, AVG(volume) as avg_vol FROM stock_history "
            f"  WHERE volume > 0 GROUP BY symbol) v ON s.symbol = v.symbol "
            f"WHERE t.primary_exchange IN ({placeholders}) "
            f"AND (t.market_cap IS NULL OR t.market_cap > ?) "
            f"AND s.history_bars >= ? "
            f"AND (v.avg_vol IS NULL OR v.avg_vol >= ?) "
            f"ORDER BY s.symbol",
            (*exchanges, args.min_mcap, args.min_bars, args.min_volume)
        ).fetchall()
    finally:
        conn.close()

    symbols = [r['symbol'] for r in rows]

    if not symbols:
        logger.error("No stocks found matching criteria.")
        return

    name = args.name or f"portfolio_{len(symbols)}stocks_{args.buy_pct*100:.0f}pct"

    logger.info(f"Universe: {len(symbols)} stocks ({','.join(exchanges)}, mcap>{args.min_mcap/1e6:.0f}M, "
                f"vol>{args.min_volume/1000:.0f}K, bars>={args.min_bars})")
    logger.info(f"Config: ${args.capital:,.0f} capital, {args.buy_pct*100}% position size, max {args.max_pos} positions")

    summary = run_portfolio_backtest(
        symbols=symbols,
        initial_capital=args.capital,
        buy_pct=args.buy_pct,
        max_positions=args.max_pos,
        name=name,
    )

    print(f"\n{'='*60}")
    print(f"PORTFOLIO BACKTEST RESULTS")
    print(f"{'='*60}")
    print(f"  Initial Capital:    ${args.capital:>12,.2f}")
    print(f"  Final Value:        ${summary['final_value']:>12,.2f}")
    print(f"  Total Return:       {summary['total_return_pct']:>+11.1f}%")
    print(f"  Max Drawdown:       {summary['max_drawdown_pct']:>11.1f}%")
    print(f"  {'─'*40}")
    print(f"  Total Trades:       {summary['total_trades']:>11}")
    print(f"  Winning Trades:     {summary['winning_trades']:>11}")
    print(f"  Losing Trades:      {summary['losing_trades']:>11}")
    print(f"  Win Rate:           {summary['win_rate_pct']:>10.1f}%")
    print(f"  Avg Win:            {summary['avg_win_pct']:>+10.1f}%")
    print(f"  Avg Loss:           {summary['avg_loss_pct']:>+10.1f}%")
    print(f"  {'─'*40}")
    print(f"  Max Positions Held: {summary['max_positions_held']:>11}")
    print(f"  Avg Holding Period: {summary['avg_bars_held']:>9} bars")
    print(f"  Positions Open:     {summary['positions_still_open']:>11}")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
