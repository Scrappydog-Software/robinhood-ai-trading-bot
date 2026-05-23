"""Massive API client for fetching market ticker data.

Named massive_client.py (not massive.py) to avoid shadowing the ``massive``
pip package on sys.path.

Uses the Massive RESTClient under the hood.  The API key is read from
config.py via ``from config import *`` — the same pattern every other module
in this repo uses.
"""

from massive import RESTClient

from ..utils import logger
from config import *  # noqa: F401,F403

# Defensive fallback — config.py may not have MASSIVE_API_KEY yet.
try:
    MASSIVE_API_KEY
except NameError:
    MASSIVE_API_KEY = None

# ---------------------------------------------------------------------------
# Lazy singleton
# ---------------------------------------------------------------------------
_client = None


def get_client():
    """Return a lazy-initialised RESTClient singleton.

    Raises RuntimeError if MASSIVE_API_KEY is not configured.
    """
    global _client
    if _client is None:
        if not MASSIVE_API_KEY:
            raise RuntimeError(
                "MASSIVE_API_KEY is not set in config.py. "
                "Add it to config.py (see config.py.example)."
            )
        _client = RESTClient(api_key=MASSIVE_API_KEY)
    return _client


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def fetch_all_tickers():
    """Fetch all tickers from the Massive API.

    Returns an iterator of Ticker objects (the Massive client paginates
    automatically).
    """
    client = get_client()
    logger.info("MassiveClient: fetching all tickers...")
    return client.list_tickers()


def fetch_ticker_details(ticker):
    """Fetch detailed information for a single ticker symbol.

    Returns a TickerDetails object.
    """
    client = get_client()
    logger.debug(f"MassiveClient: fetching details for {ticker}")
    return client.get_ticker_details(ticker)
