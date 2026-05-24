"""Unified entry point for the Robinhood AI Trading Bot.

Replaces running main.py and webui.py as separate processes. A single
``python app.py`` call:

  1. Authenticates to Robinhood once.
  2. Starts the trading loop in a background daemon thread.
  3. Starts the Flask web UI on 127.0.0.1:WEBUI_PORT.

The trading loop can be started/stopped from the browser via the
status card and /api/loop/* endpoints. The loop also auto-starts
on launch.

Security: binds ONLY to localhost. This app has no authentication —
exposing it on 0.0.0.0 would give anyone on the network unauthenticated
access to the Robinhood account.
"""

import asyncio
import threading
import time

from config import *  # noqa: F401,F403
# Defensive fallback for config keys that may not exist in older config.py files.
try:
    WEBUI_PORT
except NameError:
    WEBUI_PORT = 5001

from src.api import robinhood
from src.api import massive_client
from src import db
from src.state import trading_state
from src.trading import loop as trading_loop
from src.utils import logger

# Import the Flask app and its routes from webui.py
from webui import app


def _ensure_signals_for_all_stocks():
    """Load history and compute signals for portfolio/watchlist stocks missing from stock_stats.

    Runs in a background thread so it doesn't block app startup.
    """
    try:
        # Get all portfolio symbols
        holdings = robinhood.get_portfolio_stocks() or {}
        portfolio_symbols = list(holdings.keys())

        # Get all watchlist symbols
        watchlist_symbols = []
        try:
            all_lists = robinhood.get_all_watchlists()
            names = [w.get('display_name') for w in all_lists if isinstance(w, dict) and w.get('display_name')]
            for name in names:
                try:
                    stocks = robinhood.get_watchlist_stocks(name)
                    for s in stocks:
                        sym = s.get('symbol') if isinstance(s, dict) else None
                        if sym and sym not in watchlist_symbols:
                            watchlist_symbols.append(sym)
                except Exception:
                    continue
        except Exception as e:
            logger.error(f"App: error fetching watchlists for signal init: {e}")

        all_symbols = list(set(portfolio_symbols + watchlist_symbols))
        existing_stats = db.get_all_stock_stats()
        missing = [s for s in all_symbols if s not in existing_stats]

        if not missing:
            logger.info(f"App: all {len(all_symbols)} stocks have signals computed")
            return

        logger.info(f"App: {len(missing)} stocks need history/signals: {', '.join(missing[:10])}{'...' if len(missing) > 10 else ''}")

        for i, symbol in enumerate(missing, 1):
            try:
                logger.info(f"App: loading history for {symbol} ({i}/{len(missing)})...")
                status = db.get_stock_history_status(symbol)
                if not status['has_data']:
                    bars = massive_client.fetch_daily_bars(symbol, days=730)
                    if bars:
                        db.upsert_stock_history(symbol, bars)
                        db.compute_indicators(symbol)
                    else:
                        logger.error(f"App: no data returned for {symbol}")
                else:
                    db.compute_indicators(symbol)
            except Exception as e:
                logger.error(f"App: error processing {symbol}: {e}")
                continue

        logger.info(f"App: signal initialization complete for {len(missing)} stocks")
    except Exception as e:
        logger.error(f"App: error in signal initialization: {e}")


def main():
    """Authenticate, start trading loop, and run the web server."""

    # --- Initialise SQLite database ---
    db.init_db()

    # --- Single Robinhood login at startup ---
    logger.info("App: logging in to Robinhood...")
    login_resp = asyncio.run(robinhood.login_to_robinhood())
    if not login_resp or 'expires_in' not in login_resp:
        logger.error("App: Robinhood login failed; exiting.")
        raise SystemExit(1)

    trading_state.update(
        logged_in=True,
        token_expiry=time.time() + login_resp['expires_in'],
    )
    logger.info(f"App: Robinhood login successful. Token expires in {login_resp['expires_in']}s")

    # --- Load history + compute signals for all stocks (background) ---
    signal_thread = threading.Thread(
        target=_ensure_signals_for_all_stocks,
        name="signal-init",
        daemon=True,
    )
    signal_thread.start()

    # --- Start trading loop in background ---
    trading_loop.start()

    # --- Start Flask web UI (blocking) ---
    logger.info(f"App: starting Flask on http://127.0.0.1:{WEBUI_PORT} (local-only)...")
    app.run(host='127.0.0.1', port=WEBUI_PORT, debug=False)


if __name__ == '__main__':
    main()
