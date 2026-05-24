"""Massive API client for fetching market ticker and price data.

Named massive_client.py (not massive.py) to avoid shadowing the ``massive``
pip package on sys.path.

Uses the Massive RESTClient under the hood.  The API key is read from
config.py via ``from config import *`` — the same pattern every other module
in this repo uses.

This module is the exclusive source for market data (quotes, historical OHLCV,
indicators). Robinhood is used only for account positions and watchlists.
"""

import os
import time
from datetime import datetime, timedelta

import certifi
import urllib3
from urllib3.util.retry import Retry
from massive import RESTClient

# macOS Python often lacks system root certs at the default OpenSSL path.
# Point urllib3/requests at certifi's bundle.
os.environ.setdefault('SSL_CERT_FILE', certifi.where())

from ..utils import logger
from config import *  # noqa: F401,F403

# Defensive fallback — config.py may not have MASSIVE_API_KEY yet.
try:
    MASSIVE_API_KEY
except NameError:
    MASSIVE_API_KEY = None

# ---------------------------------------------------------------------------
# Rate limiting / retry configuration
# ---------------------------------------------------------------------------
# Despite "unlimited" plan marketing, the API enforces an undocumented burst
# limit that triggers 429s on rapid successive calls. The SDK's default
# (3 retries, 0.1 backoff_factor) is insufficient.
#
# Strategy: exponential backoff with 5 retries and 2.0 backoff_factor
# yields delays of [0, 4, 8, 16, 32] seconds (max ~60s total wait per call).
# Combined with a per-call minimum delay to avoid bursting.

_RETRIES = 5
_BACKOFF_FACTOR = 2.0
_MIN_CALL_DELAY = 1.0  # seconds between API calls (proactive throttle)
_last_call_time = 0.0

# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------
_client = None


def get_client():
    """Return a lazy-initialised RESTClient singleton with robust retry config.

    Raises RuntimeError if MASSIVE_API_KEY is not configured.
    """
    global _client
    if _client is None:
        if not MASSIVE_API_KEY:
            raise RuntimeError(
                "MASSIVE_API_KEY is not set in config.py. "
                "Add it to config.py (see config.py.example)."
            )
        _client = RESTClient(api_key=MASSIVE_API_KEY, retries=_RETRIES)
        # Override the internal urllib3 PoolManager with stronger backoff
        retry_strategy = Retry(
            total=_RETRIES,
            backoff_factor=_BACKOFF_FACTOR,
            status_forcelist=[413, 429, 499, 500, 502, 503, 504],
            respect_retry_after_header=True,
            backoff_max=30,  # cap individual retry delay at 30s
        )
        _client.client = urllib3.PoolManager(
            num_pools=10,
            headers=_client.client.headers,
            retries=retry_strategy,
            timeout=urllib3.Timeout(connect=10.0, read=30.0),
            cert_reqs='CERT_REQUIRED',
            ca_certs=certifi.where(),
        )
    return _client


def _throttle():
    """Enforce minimum delay between API calls to avoid burst 429s."""
    global _last_call_time
    elapsed = time.time() - _last_call_time
    if elapsed < _MIN_CALL_DELAY:
        wait = _MIN_CALL_DELAY - elapsed
        time.sleep(wait)
    _last_call_time = time.time()


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def fetch_all_tickers(limit=1000):
    """Fetch all tickers from the Massive API.

    Returns an iterator of Ticker objects (the Massive client paginates
    automatically).  ``limit`` controls the page size — set to the
    maximum (1000) to minimise API calls.
    """
    client = get_client()
    _throttle()
    logger.info(f"MassiveClient: fetching all tickers (page size={limit})...")
    return client.list_tickers(limit=limit)


def fetch_ticker_details(ticker):
    """Fetch detailed information for a single ticker symbol.

    Returns a TickerDetails object.
    """
    client = get_client()
    _throttle()
    logger.debug(f"MassiveClient: fetching details for {ticker}")
    return client.get_ticker_details(ticker)


def fetch_previous_close(symbol):
    """Fetch the previous day's close data for a symbol.

    Returns a dict with: open, high, low, close, volume, vwap, or None on error.
    """
    client = get_client()
    _throttle()
    try:
        aggs = client.get_previous_close_agg(symbol)
        if aggs and isinstance(aggs, list) and len(aggs) > 0:
            bar = aggs[0]
            return {
                'open': getattr(bar, 'open', None),
                'high': getattr(bar, 'high', None),
                'low': getattr(bar, 'low', None),
                'close': getattr(bar, 'close', None),
                'volume': getattr(bar, 'volume', None),
                'vwap': getattr(bar, 'vwap', None),
            }
        # Some SDK versions return an iterator or single object
        if aggs and hasattr(aggs, 'close'):
            return {
                'open': getattr(aggs, 'open', None),
                'high': getattr(aggs, 'high', None),
                'low': getattr(aggs, 'low', None),
                'close': getattr(aggs, 'close', None),
                'volume': getattr(aggs, 'volume', None),
                'vwap': getattr(aggs, 'vwap', None),
            }
    except Exception as e:
        logger.error(f"MassiveClient: error fetching previous close for {symbol}: {e}")
    return None


def fetch_snapshot(symbol):
    """Fetch a real-time snapshot for a symbol (current price, day stats).

    Requires Starter plan or above. Returns None gracefully if plan
    doesn't support snapshots.
    """
    client = get_client()
    _throttle()
    try:
        snap = client.get_snapshot_ticker("stocks", symbol)
        if not snap:
            return None

        result = {}
        # Day bar
        if hasattr(snap, 'day') and snap.day:
            result['day_open'] = getattr(snap.day, 'o', None) or getattr(snap.day, 'open', None)
            result['day_high'] = getattr(snap.day, 'h', None) or getattr(snap.day, 'high', None)
            result['day_low'] = getattr(snap.day, 'l', None) or getattr(snap.day, 'low', None)
            result['day_close'] = getattr(snap.day, 'c', None) or getattr(snap.day, 'close', None)
            result['day_volume'] = getattr(snap.day, 'v', None) or getattr(snap.day, 'volume', None)
            result['day_vwap'] = getattr(snap.day, 'vw', None) or getattr(snap.day, 'vwap', None)

        # Previous day
        if hasattr(snap, 'prevDay') and snap.prevDay:
            result['prev_close'] = getattr(snap.prevDay, 'c', None) or getattr(snap.prevDay, 'close', None)

        # Today's change
        if hasattr(snap, 'todaysChange'):
            result['today_change'] = snap.todaysChange
        if hasattr(snap, 'todaysChangePerc'):
            result['today_change_pct'] = snap.todaysChangePerc

        # Price: prefer day close, then prev close
        result['price'] = result.get('day_close') or result.get('prev_close')

        return result if result.get('price') else None
    except Exception as e:
        err_str = str(e)
        if 'NOT_AUTHORIZED' in err_str or 'upgrade your plan' in err_str:
            logger.debug(f"MassiveClient: snapshots not available on current plan for {symbol}")
        else:
            logger.error(f"MassiveClient: error fetching snapshot for {symbol}: {e}")
    return None


def fetch_current_price(symbol):
    """Get the current/latest price for a symbol.

    Tries snapshot first (if plan supports it), then falls back to
    previous close aggregate.
    Returns float price or None.
    """
    snap = fetch_snapshot(symbol)
    if snap and snap.get('price'):
        return float(snap['price'])

    prev = fetch_previous_close(symbol)
    if prev and prev.get('close'):
        return float(prev['close'])

    return None


def fetch_daily_bars(symbol, days=730):
    """Fetch daily OHLCV bars for a symbol.

    Args:
        symbol: Stock ticker symbol
        days: Number of calendar days to look back (default 730 = ~2 years)

    Returns a list of dicts with: bar_date, open, high, low, close, volume, vwap
    """
    client = get_client()
    _throttle()
    today = datetime.now()
    to_date = today.strftime('%Y-%m-%d')
    from_date = (today - timedelta(days=days)).strftime('%Y-%m-%d')

    try:
        aggs = client.get_aggs(symbol, 1, "day", from_date, to_date, limit=50000)
        bars = []
        for agg in aggs:
            bar_date = datetime.fromtimestamp(agg.timestamp / 1000).strftime('%Y-%m-%d')
            bars.append({
                'bar_date': bar_date,
                'open': getattr(agg, 'open', None),
                'high': getattr(agg, 'high', None),
                'low': getattr(agg, 'low', None),
                'close': getattr(agg, 'close', None),
                'volume': getattr(agg, 'volume', None),
                'vwap': getattr(agg, 'vwap', None),
            })
        return bars
    except Exception as e:
        logger.error(f"MassiveClient: error fetching daily bars for {symbol}: {e}")
        return []


def fetch_intraday_bars(symbol, interval="5", span_days=1):
    """Fetch intraday bars for a symbol.

    Args:
        symbol: Stock ticker symbol
        interval: Minutes per bar (default "5")
        span_days: Number of days to look back (default 1)

    Returns a list of dicts with: timestamp, open, high, low, close, volume, vwap
    """
    client = get_client()
    _throttle()
    today = datetime.now()
    to_date = today.strftime('%Y-%m-%d')
    from_date = (today - timedelta(days=span_days)).strftime('%Y-%m-%d')

    try:
        aggs = client.get_aggs(symbol, int(interval), "minute", from_date, to_date, limit=50000)
        bars = []
        for agg in aggs:
            bars.append({
                'timestamp': agg.timestamp,
                'open': getattr(agg, 'open', None),
                'high': getattr(agg, 'high', None),
                'low': getattr(agg, 'low', None),
                'close': getattr(agg, 'close', None),
                'volume': getattr(agg, 'volume', None),
                'vwap': getattr(agg, 'vwap', None),
            })
        return bars
    except Exception as e:
        logger.error(f"MassiveClient: error fetching intraday bars for {symbol}: {e}")
        return []


def compute_rsi(closes, period=14):
    """Compute RSI from a list of close prices.

    Returns the most recent RSI value, or None if insufficient data.
    """
    if len(closes) < period + 1:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def compute_vwap(bars):
    """Compute VWAP from a list of bar dicts (must have high, low, close, volume).

    Returns VWAP float or None.
    """
    total_volume = 0
    total_tp_vol = 0
    for bar in bars:
        h = bar.get('high') or 0
        l = bar.get('low') or 0
        c = bar.get('close') or 0
        v = bar.get('volume') or 0
        if v > 0:
            tp = (h + l + c) / 3
            total_tp_vol += tp * v
            total_volume += v
    if total_volume == 0:
        return None
    return round(total_tp_vol / total_volume, 2)


def compute_moving_averages(closes):
    """Compute 50-day and 200-day moving averages from close prices.

    Returns dict with '50_day_mavg_price' and '200_day_mavg_price', or None values
    if insufficient data.
    """
    result = {'50_day_mavg_price': None, '200_day_mavg_price': None}
    if len(closes) >= 50:
        result['50_day_mavg_price'] = round(sum(closes[-50:]) / 50, 2)
    if len(closes) >= 200:
        result['200_day_mavg_price'] = round(sum(closes[-200:]) / 200, 2)
    return result


def enrich_stock_data(symbol, stock_data):
    """Enrich stock data with RSI, VWAP, and moving averages from Massive API.

    Uses cached daily history from SQLite when available (skips re-fetching).
    Stores any newly fetched bars to stock_history for future use.
    Modifies stock_data in place and returns it.
    """
    from src import db

    today_str = datetime.now().strftime('%Y-%m-%d')

    # --- Daily bars for moving averages ---
    # Check if we already have recent history cached in SQLite
    latest_bar = db.get_latest_bar_date(symbol)
    daily_closes = None

    if latest_bar and latest_bar >= (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%d'):
        # Use cached data — no API call needed
        daily_closes = db.get_daily_closes(symbol, limit=200)
        logger.info(f"  {symbol}: using {len(daily_closes)} cached daily bars (latest: {latest_bar})")
    else:
        # Fetch from API and store
        try:
            logger.info(f"  {symbol}: fetching 2-year daily bars from API...")
            daily = fetch_daily_bars(symbol, days=730)
            logger.info(f"  {symbol}: got {len(daily)} daily bars, storing to DB...")
            if daily:
                db.upsert_stock_history(symbol, daily)
                db.compute_indicators(symbol)
                daily_closes = [b['close'] for b in daily if b.get('close')]
        except Exception as e:
            logger.error(f"  {symbol}: error on daily bars: {e}")

    if daily_closes:
        ma = compute_moving_averages(daily_closes)
        if ma.get('50_day_mavg_price'):
            stock_data['50_day_mavg_price'] = ma['50_day_mavg_price']
        if ma.get('200_day_mavg_price'):
            stock_data['200_day_mavg_price'] = ma['200_day_mavg_price']

    # --- Intraday bars for RSI and VWAP ---
    try:
        logger.info(f"  {symbol}: fetching intraday bars...")
        intraday = fetch_intraday_bars(symbol, interval="5", span_days=1)
        logger.info(f"  {symbol}: got {len(intraday)} intraday bars")
        if intraday:
            intraday_closes = [b['close'] for b in intraday if b.get('close')]
            if len(intraday_closes) >= 15:
                rsi = compute_rsi(intraday_closes)
                if rsi is not None:
                    stock_data['rsi'] = rsi
            vwap = compute_vwap(intraday)
            if vwap is not None:
                stock_data['vwap'] = vwap
    except Exception as e:
        logger.error(f"  {symbol}: error on intraday: {e}")

    return stock_data
