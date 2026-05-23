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
import time

from config import *  # noqa: F401,F403
# Defensive fallback for config keys that may not exist in older config.py files.
try:
    WEBUI_PORT
except NameError:
    WEBUI_PORT = 5001

from src.api import robinhood
from src import db
from src.state import trading_state
from src.trading import loop as trading_loop
from src.utils import logger

# Import the Flask app and its routes from webui.py
from webui import app


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

    # --- Start trading loop in background ---
    trading_loop.start()

    # --- Start Flask web UI (blocking) ---
    logger.info(f"App: starting Flask on http://127.0.0.1:{WEBUI_PORT} (local-only)...")
    app.run(host='127.0.0.1', port=WEBUI_PORT, debug=False)


if __name__ == '__main__':
    main()
