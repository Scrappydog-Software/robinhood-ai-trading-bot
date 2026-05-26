"""Realistic portfolio-level backtesting engine.

Simulates a multi-stock portfolio starting with $100,000. Tracks cash,
positions, trade history, and daily portfolio value. Uses the signal
engine's recommendations to drive buy/sell decisions.

Key rules:
- Standard buy size: 2% of current portfolio value
- Cannot buy if insufficient cash
- When fully invested: exit weakest position to free cash for stronger signal
- Tracks per-position P&L, holding period, and exit reason

Stores results in a separate DB table (portfolio_backtest) so existing
single-stock backtests are unaffected.
"""

import json
from datetime import datetime

from .db import _connect, _write_lock, init_db
from .signals import compute_signals_for_bars
from .utils import logger


# ---------------------------------------------------------------------------
# Database schema for portfolio backtesting
# ---------------------------------------------------------------------------

_PORTFOLIO_BT_SCHEMA = """\
CREATE TABLE IF NOT EXISTS portfolio_backtest_config (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    initial_capital REAL NOT NULL DEFAULT 100000,
    buy_pct         REAL NOT NULL DEFAULT 0.02,
    max_positions   INTEGER DEFAULT 50,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS portfolio_backtest_trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id     INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    action          TEXT NOT NULL,
    date            TEXT NOT NULL,
    price           REAL NOT NULL,
    shares          REAL NOT NULL,
    value           REAL NOT NULL,
    reason          TEXT,
    portfolio_value REAL,
    cash_after      REAL,
    pnl             REAL,
    pnl_pct         REAL,
    bars_held       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pbt_backtest ON portfolio_backtest_trades(backtest_id);
CREATE INDEX IF NOT EXISTS idx_pbt_symbol ON portfolio_backtest_trades(symbol);

CREATE TABLE IF NOT EXISTS portfolio_backtest_daily (
    backtest_id     INTEGER NOT NULL,
    date            TEXT NOT NULL,
    portfolio_value REAL NOT NULL,
    cash            REAL NOT NULL,
    positions_count INTEGER NOT NULL,
    invested_value  REAL NOT NULL,
    PRIMARY KEY (backtest_id, date)
);

CREATE TABLE IF NOT EXISTS portfolio_backtest_summary (
    backtest_id         INTEGER PRIMARY KEY,
    total_return_pct    REAL,
    final_value         REAL,
    max_drawdown_pct    REAL,
    total_trades        INTEGER,
    winning_trades      INTEGER,
    losing_trades       INTEGER,
    win_rate_pct        REAL,
    avg_win_pct         REAL,
    avg_loss_pct        REAL,
    max_positions_held  INTEGER,
    avg_bars_held       INTEGER,
    sharpe_estimate     REAL,
    completed_at        TEXT
);
"""


def init_portfolio_backtest_db():
    """Create portfolio backtest tables if they don't exist."""
    conn = _connect()
    try:
        with _write_lock:
            conn.executescript(_PORTFOLIO_BT_SCHEMA)
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Portfolio Backtest Engine
# ---------------------------------------------------------------------------

class Position:
    def __init__(self, symbol, buy_price, shares, buy_date, buy_bar_idx):
        self.symbol = symbol
        self.buy_price = buy_price
        self.shares = shares
        self.buy_date = buy_date
        self.buy_bar_idx = buy_bar_idx
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
    """Determine market regime from SPY bar data.

    Returns: 'bull', 'recovery', 'correction', 'bear', or 'unknown'
    """
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


def run_portfolio_backtest(symbols, initial_capital=100000, buy_pct=0.02,
                           max_positions=50, name="default",
                           regime_aware=True, start_date=None):
    """Run a realistic portfolio-level backtest across multiple stocks.

    Args:
        symbols: list of ticker symbols to include in the universe
        initial_capital: starting cash ($100,000 default)
        buy_pct: position size as % of portfolio value (0.02 = 2%)
        max_positions: maximum simultaneous positions
        name: label for this backtest run
        regime_aware: if True, use SPY to adjust buying based on market regime

    Market regime rules (when regime_aware=True):
        - Bull/Recovery: buy freely on score >= 2
        - Correction: maintain 25% cash reserve, only buy score >= 5
        - Bear: maintain 50% cash reserve, only buy score >= 7

    Returns:
        dict with summary stats
    """
    from .db import get_stock_history_bars

    init_portfolio_backtest_db()

    # Create backtest config record
    conn = _connect()
    try:
        with _write_lock:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO portfolio_backtest_config (name, initial_capital, buy_pct, max_positions) "
                "VALUES (?, ?, ?, ?)",
                (name, initial_capital, buy_pct, max_positions)
            )
            conn.commit()
            backtest_id = cursor.lastrowid
    finally:
        conn.close()

    logger.info(f"PortfolioBT: starting '{name}' (id={backtest_id}) with ${initial_capital:,.0f}, "
                f"{buy_pct*100}% position size, max {max_positions} positions, "
                f"regime_aware={regime_aware}")

    # Load SPY for market regime detection
    spy_by_date = {}
    if regime_aware:
        spy_bars = get_stock_history_bars('SPY')
        if spy_bars:
            spy_bars = compute_signals_for_bars(spy_bars)
            spy_by_date = {b['bar_date']: b for b in spy_bars}
            logger.info(f"PortfolioBT: SPY loaded ({len(spy_bars)} bars) for regime detection")
        else:
            logger.warning("PortfolioBT: SPY not in DB — regime detection disabled")
            regime_aware = False

    # Load and compute signals for all symbols
    all_bars = {}
    for sym in symbols:
        bars = get_stock_history_bars(sym)
        if bars and len(bars) >= 50:
            bars = compute_signals_for_bars(bars)
            all_bars[sym] = bars

    logger.info(f"PortfolioBT: loaded {len(all_bars)} stocks with sufficient history")

    if not all_bars:
        return {'error': 'No stocks with sufficient history'}

    # Build a unified date timeline
    all_dates = sorted(set(
        bar['bar_date'] for bars in all_bars.values() for bar in bars
    ))

    # Index bars by date for fast lookup
    bars_by_date = {}
    for sym, bars in all_bars.items():
        for bar in bars:
            key = (sym, bar['bar_date'])
            bars_by_date[key] = bar

    # Filter dates if start_date specified
    if start_date:
        all_dates = [d for d in all_dates if d >= start_date]
        logger.info(f"PortfolioBT: starting from {start_date} ({len(all_dates)} trading days)")

    # Simulation state
    cash = initial_capital
    positions = {}  # symbol -> Position
    trades = []
    daily_values = []
    peak_value = initial_capital

    for date in all_dates:
        # Update current prices for all held positions
        for sym, pos in list(positions.items()):
            bar = bars_by_date.get((sym, date))
            if bar and bar.get('close'):
                pos.current_price = bar['close']

        # Calculate portfolio value
        invested_value = sum(p.value for p in positions.values())
        portfolio_value = cash + invested_value

        # Track peak for drawdown
        if portfolio_value > peak_value:
            peak_value = portfolio_value

        # Record daily snapshot
        daily_values.append({
            'date': date,
            'portfolio_value': round(portfolio_value, 2),
            'cash': round(cash, 2),
            'positions_count': len(positions),
            'invested_value': round(invested_value, 2),
        })

        # --- SELL LOGIC ---
        # Signals are computed after market close. Execute at NEXT day's open.
        # We check YESTERDAY's signal and execute today at open.
        prev_date_idx = all_dates.index(date) - 1 if all_dates.index(date) > 0 else -1
        prev_date = all_dates[prev_date_idx] if prev_date_idx >= 0 else None

        if prev_date:
            for sym in list(positions.keys()):
                prev_bar = bars_by_date.get((sym, prev_date))
                today_bar = bars_by_date.get((sym, date))
                if not prev_bar:
                    continue
                rec = (prev_bar.get('signal_synthesis') or '').lower()
                if rec in ('sell', 'strong_sell'):
                    pos = positions.pop(sym)
                    # Execute at today's open (next available price after signal)
                    sell_price = (today_bar.get('open') if today_bar else None) or pos.current_price
                    proceeds = pos.shares * sell_price
                    pnl = proceeds - pos.cost_basis
                    pnl_pct = (sell_price - pos.buy_price) / pos.buy_price * 100
                    cash += proceeds
                    bars_held = len([d for d in all_dates if pos.buy_date <= d <= date])
                    trades.append({
                        'symbol': sym, 'action': 'sell', 'date': date,
                        'price': sell_price, 'shares': pos.shares,
                        'value': proceeds, 'reason': f'signal_{rec}',
                        'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 1),
                        'bars_held': bars_held,
                        'portfolio_value': round(cash + sum(p.value for p in positions.values()), 2),
                        'cash_after': round(cash, 2),
                    })

        # --- REBALANCE: Exit weakest if fully invested and strong buy available ---
        if len(positions) >= max_positions:
            # Find strongest buy signal in the universe today
            best_buy = None
            best_score = 0
            for sym in all_bars:
                if sym in positions:
                    continue
                bar = bars_by_date.get((sym, date))
                if bar and (bar.get('signal_synthesis') or '').lower() in ('buy', 'strong_buy'):
                    score = bar.get('signal_score', 0)
                    if score > best_score:
                        best_score = score
                        best_buy = sym

            if best_buy and best_score >= 5:
                # Find weakest position (worst P&L with negative MACD)
                weakest = None
                weakest_score = float('inf')
                for sym, pos in positions.items():
                    bar = bars_by_date.get((sym, date))
                    if bar:
                        pos_score = bar.get('signal_score', 0)
                        # Prioritize selling positions that are losing AND have weak signals
                        combined = pos.pnl_pct + pos_score * 2
                        if combined < weakest_score:
                            weakest_score = combined
                            weakest = sym

                if weakest and weakest_score < -5:
                    pos = positions.pop(weakest)
                    sell_price = bars_by_date.get((weakest, date), {}).get('close') or pos.current_price
                    proceeds = pos.shares * sell_price
                    pnl = proceeds - pos.cost_basis
                    pnl_pct = (sell_price - pos.buy_price) / pos.buy_price * 100
                    cash += proceeds
                    bars_held = len([d for d in all_dates if pos.buy_date <= d <= date])
                    trades.append({
                        'symbol': weakest, 'action': 'sell', 'date': date,
                        'price': sell_price, 'shares': pos.shares,
                        'value': proceeds, 'reason': 'rebalance_weakest',
                        'pnl': round(pnl, 2), 'pnl_pct': round(pnl_pct, 1),
                        'bars_held': bars_held,
                        'portfolio_value': round(cash + sum(p.value for p in positions.values()), 2),
                        'cash_after': round(cash, 2),
                    })

        # --- BUY LOGIC (regime-aware) ---
        # Signals computed after yesterday's close. Execute buys at today's open.
        # Check YESTERDAY's signals and buy at today's open.
        if prev_date:
            regime = 'unknown'
            min_score = 2
            min_cash_reserve = 0.0

            if regime_aware:
                spy_bar = spy_by_date.get(prev_date)
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
                # Collect buy signals from YESTERDAY
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
                        # Get today's open for execution price
                        today_bar = bars_by_date.get((sym, date))
                        if today_bar and today_bar.get('open'):
                            buy_candidates.append((score, sym, today_bar))

                buy_candidates.sort(reverse=True, key=lambda x: x[0])

                for score, sym, today_bar in buy_candidates:
                    available_cash = cash - (portfolio_value * min_cash_reserve)
                    if available_cash < position_size or len(positions) >= max_positions:
                        break
                    buy_price = today_bar.get('open') or 0
                    if buy_price <= 0:
                        continue
                    shares = position_size / buy_price
                    positions[sym] = Position(sym, buy_price, shares, date, 0)
                    cash -= position_size
                    trades.append({
                        'symbol': sym, 'action': 'buy', 'date': date,
                        'price': buy_price, 'shares': round(shares, 6),
                        'value': round(position_size, 2), 'reason': f'signal_score_{score}',
                        'pnl': None, 'pnl_pct': None, 'bars_held': None,
                        'portfolio_value': round(cash + sum(p.value for p in positions.values()), 2),
                        'cash_after': round(cash, 2),
                    })

    # --- Final summary ---
    final_value = cash + sum(p.value for p in positions.values())
    total_return_pct = (final_value - initial_capital) / initial_capital * 100
    max_drawdown = min(
        (dv['portfolio_value'] - peak_value) / peak_value * 100
        for dv in daily_values
    ) if daily_values else 0

    sell_trades = [t for t in trades if t['action'] == 'sell']
    winning = [t for t in sell_trades if t['pnl'] and t['pnl'] > 0]
    losing = [t for t in sell_trades if t['pnl'] and t['pnl'] <= 0]
    win_rate = len(winning) / len(sell_trades) * 100 if sell_trades else 0
    avg_win = sum(t['pnl_pct'] for t in winning) / len(winning) if winning else 0
    avg_loss = sum(t['pnl_pct'] for t in losing) / len(losing) if losing else 0
    max_pos = max(dv['positions_count'] for dv in daily_values) if daily_values else 0
    avg_bars = sum(t['bars_held'] for t in sell_trades if t['bars_held']) / len(sell_trades) if sell_trades else 0

    summary = {
        'backtest_id': backtest_id,
        'total_return_pct': round(total_return_pct, 2),
        'final_value': round(final_value, 2),
        'max_drawdown_pct': round(max_drawdown, 2),
        'total_trades': len(trades),
        'winning_trades': len(winning),
        'losing_trades': len(losing),
        'win_rate_pct': round(win_rate, 1),
        'avg_win_pct': round(avg_win, 1),
        'avg_loss_pct': round(avg_loss, 1),
        'max_positions_held': max_pos,
        'avg_bars_held': round(avg_bars),
        'positions_still_open': len(positions),
    }

    # Store results in DB
    conn = _connect()
    try:
        with _write_lock:
            # Store trades
            for t in trades:
                conn.execute(
                    "INSERT INTO portfolio_backtest_trades "
                    "(backtest_id, symbol, action, date, price, shares, value, reason, "
                    "portfolio_value, cash_after, pnl, pnl_pct, bars_held) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (backtest_id, t['symbol'], t['action'], t['date'], t['price'],
                     t['shares'], t['value'], t['reason'], t['portfolio_value'],
                     t['cash_after'], t['pnl'], t['pnl_pct'], t['bars_held'])
                )

            # Store daily snapshots (sample every 5th day to save space)
            for i, dv in enumerate(daily_values):
                if i % 5 == 0 or i == len(daily_values) - 1:
                    conn.execute(
                        "INSERT OR REPLACE INTO portfolio_backtest_daily "
                        "(backtest_id, date, portfolio_value, cash, positions_count, invested_value) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (backtest_id, dv['date'], dv['portfolio_value'], dv['cash'],
                         dv['positions_count'], dv['invested_value'])
                    )

            # Store summary
            conn.execute(
                "INSERT OR REPLACE INTO portfolio_backtest_summary "
                "(backtest_id, total_return_pct, final_value, max_drawdown_pct, "
                "total_trades, winning_trades, losing_trades, win_rate_pct, "
                "avg_win_pct, avg_loss_pct, max_positions_held, avg_bars_held, "
                "completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (backtest_id, summary['total_return_pct'], summary['final_value'],
                 summary['max_drawdown_pct'], summary['total_trades'],
                 summary['winning_trades'], summary['losing_trades'],
                 summary['win_rate_pct'], summary['avg_win_pct'], summary['avg_loss_pct'],
                 summary['max_positions_held'], summary['avg_bars_held'],
                 datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ'))
            )
            conn.commit()
    finally:
        conn.close()

    logger.info(f"PortfolioBT: '{name}' complete. ${initial_capital:,.0f} → ${final_value:,.0f} "
                f"({total_return_pct:+.1f}%), {len(trades)} trades, "
                f"max drawdown {max_drawdown:.1f}%, win rate {win_rate:.1f}%")

    return summary
