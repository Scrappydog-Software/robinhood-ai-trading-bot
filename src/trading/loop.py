"""Background trading loop thread management.

Runs the trading_bot() cycle on a configurable interval in a daemon
thread. Uses threading.Event.wait(timeout=interval) instead of
time.sleep() for clean cancellation — calling stop() sets the event
and the thread exits within one iteration.

The loop updates the shared TradingState singleton after each cycle
so the web UI can read live data without JSON file round-trips.
"""

import asyncio
import threading
import time
from datetime import datetime, timezone

from config import *  # noqa: F401,F403
# Defensive fallbacks for config keys that may not exist in older config.py files.
try:
    RUN_INTERVAL_SECONDS
except NameError:
    RUN_INTERVAL_SECONDS = 600
try:
    AFTER_HOURS_INTERVAL_SECONDS
except NameError:
    AFTER_HOURS_INTERVAL_SECONDS = 3600

from src.api import robinhood
from src.state import trading_state
from src.utils import logger


# The stop event — when set, the loop thread exits cleanly.
_stop_event = threading.Event()

# The thread reference — so we can check if it's alive.
_loop_thread = None
_loop_lock = threading.Lock()


def _ensure_login():
    """Check if the Robinhood token needs refresh and re-login if so.

    Updates trading_state with login status and token expiry.
    Returns True if logged in, False on failure.
    """
    # Refresh 5 minutes before expiry
    if time.time() < trading_state.token_expiry - 300:
        return True

    logger.info("Trading loop: logging in to Robinhood...")
    login_resp = asyncio.run(robinhood.login_to_robinhood())
    if not login_resp or 'expires_in' not in login_resp:
        logger.error("Trading loop: Robinhood login failed")
        trading_state.update(logged_in=False)
        return False

    trading_state.update(
        logged_in=True,
        token_expiry=time.time() + login_resp['expires_in'],
    )
    logger.info(f"Trading loop: logged in. Token expires in {login_resp['expires_in']}s")
    return True


def _run_loop():
    """Main loop body — runs in a daemon thread."""
    # Import trading_bot here to avoid circular imports at module load time.
    from main import trading_bot

    while not _stop_event.is_set():
        run_interval_seconds = 60  # fallback on error

        try:
            if not _ensure_login():
                trading_state.update(
                    last_cycle_error="Robinhood login failed",
                    last_cycle_time=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                )
                _stop_event.wait(timeout=run_interval_seconds)
                continue

            market_open = robinhood.is_market_open()
            trading_state.update(market_open=market_open)

            if market_open:
                run_interval_seconds = RUN_INTERVAL_SECONDS
                logger.info(f"Trading loop: market open, running in {MODE} mode...")
            else:
                run_interval_seconds = AFTER_HOURS_INTERVAL_SECONDS
                logger.info(f"Trading loop: market closed, analysis only in {MODE} mode...")

            trading_results = trading_bot(market_open=market_open)

            # Log results summary
            sold = [f"{r['symbol']} ({r['quantity']})" for r in trading_results.values() if r['decision'] == "sell" and r['result'] == "success"]
            bought = [f"{r['symbol']} ({r['quantity']})" for r in trading_results.values() if r['decision'] == "buy" and r['result'] == "success"]
            would_sell = [f"{r['symbol']} ({r['quantity']})" for r in trading_results.values() if r['decision'] == "sell" and r['result'] == "market_closed"]
            would_buy = [f"{r['symbol']} ({r['quantity']})" for r in trading_results.values() if r['decision'] == "buy" and r['result'] == "market_closed"]
            errors = [f"{r['symbol']} ({r['details']})" for r in trading_results.values() if r['result'] == "error"]

            logger.info(f"Sold: {'None' if not sold else ', '.join(sold)}")
            logger.info(f"Bought: {'None' if not bought else ', '.join(bought)}")
            if would_sell:
                logger.info(f"Would have sold (market closed): {', '.join(would_sell)}")
            if would_buy:
                logger.info(f"Would have bought (market closed): {', '.join(would_buy)}")
            logger.info(f"Errors: {'None' if not errors else ', '.join(errors)}")

            trading_state.update(
                last_cycle_time=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                last_cycle_error=None,
            )

        except Exception as e:
            run_interval_seconds = 60
            logger.error(f"Trading loop error: {e}")
            trading_state.update(
                last_cycle_time=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                last_cycle_error=str(e),
            )

        logger.info(f"Trading loop: waiting {run_interval_seconds}s...")
        _stop_event.wait(timeout=run_interval_seconds)

    # Thread is exiting
    trading_state.update(loop_running=False)
    logger.info("Trading loop: stopped.")


def start():
    """Start the trading loop in a background daemon thread.

    No-op if the loop is already running. Returns True if the loop
    was started, False if it was already running.
    """
    global _loop_thread

    with _loop_lock:
        if _loop_thread is not None and _loop_thread.is_alive():
            logger.info("Trading loop: already running, ignoring start request")
            return False

        _stop_event.clear()
        trading_state.update(loop_running=True)

        _loop_thread = threading.Thread(target=_run_loop, name="trading-loop", daemon=True)
        _loop_thread.start()
        logger.info("Trading loop: started in background thread")
        return True


def stop():
    """Signal the trading loop to stop. Returns immediately; the thread
    will exit after the current cycle (or sleep) completes.

    Returns True if stop was signalled, False if loop was not running.
    """
    global _loop_thread

    with _loop_lock:
        if _loop_thread is None or not _loop_thread.is_alive():
            trading_state.update(loop_running=False)
            logger.info("Trading loop: not running, ignoring stop request")
            return False

        _stop_event.set()
        logger.info("Trading loop: stop signalled")
        return True


def is_running():
    """Check if the trading loop thread is alive."""
    with _loop_lock:
        return _loop_thread is not None and _loop_thread.is_alive()
