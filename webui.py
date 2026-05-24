"""Local-only Flask web UI for the Robinhood AI Trading Bot.

This UI binds to 127.0.0.1:5001 ONLY. It has no authentication and would
expose your Robinhood credentials to anyone on the network if exposed. NEVER
change the bind host to 0.0.0.0 or any external interface.

Preferred start: python app.py   (unified entry point)
Legacy start:    python webui.py (standalone, deprecated)
URL:             http://127.0.0.1:5001
"""

import asyncio
import json
import os
import warnings
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request, redirect, url_for, flash
from flask_wtf.csrf import CSRFProtect

from config import *  # noqa: F401,F403 - same convention as main.py
# Defensive fallback for users whose config.py predates WEBUI_PORT.
# Default 5001 (not 5000) because macOS AirPlay Receiver listens on *:5000 IPv6
# and returns HTTP 403 to browsers via Server: AirTunes — collides invisibly.
try:
    WEBUI_PORT
except NameError:
    WEBUI_PORT = 5001
# Defensive fallbacks for the staleness window — same try/except NameError
# pattern as elsewhere. These come from config.py in normal use; the fallbacks
# only matter when running against a config.py that predates these keys.
try:
    RUN_INTERVAL_SECONDS
except NameError:
    RUN_INTERVAL_SECONDS = 600
try:
    AFTER_HOURS_INTERVAL_SECONDS
except NameError:
    AFTER_HOURS_INTERVAL_SECONDS = 3600

# Path to the JSON file written by main.py each cycle. Resolved relative to
# webui's CWD (same convention main.py uses when writing).
LAST_DECISIONS_PATH = os.path.join('data', 'last-decisions.json')

from src.api import robinhood
from src.api import claude
from src.api import massive_client
from src import db
from src.state import trading_state
from src.trading import loop as trading_loop
from src.utils import logger


def _load_decisions_map():
    """Return a dict mapping symbol -> decision from the most recent cycle.

    Reads from in-process shared state first (populated by the unified app's
    trading loop). Falls back to data/last-decisions.json if shared state is
    empty (backward compatibility when running webui.py standalone alongside
    a separate main.py process).

    Returns an empty dict if no data is available so callers can safely do
    ``decisions_map.get(symbol)`` without error handling.
    """
    # Try in-process shared state first
    state_decisions = trading_state.decisions
    if state_decisions:
        mapping = {}
        for d in state_decisions:
            if not isinstance(d, dict):
                continue
            symbol = d.get('symbol')
            decision = d.get('decision')
            if symbol and decision:
                mapping[symbol] = decision
        return mapping

    # Fall back to JSON file
    if not os.path.exists(LAST_DECISIONS_PATH):
        return {}
    try:
        with open(LAST_DECISIONS_PATH, 'r') as f:
            payload = json.load(f)
    except Exception as e:
        logger.error(f"WebUI: error reading {LAST_DECISIONS_PATH} for decisions map: {e}")
        return {}

    decisions = payload.get('decisions') or []
    mapping = {}
    for d in decisions:
        if not isinstance(d, dict):
            continue
        symbol = d.get('symbol')
        decision = d.get('decision')
        if symbol and decision:
            mapping[symbol] = decision
    return mapping


def _load_decisions_detail_map():
    """Return a dict mapping symbol -> {decision, rationale, quantity}.

    Like _load_decisions_map but includes all decision fields for the
    stock detail popup.
    """
    state_decisions = trading_state.decisions
    source = state_decisions if state_decisions else None

    if not source:
        if not os.path.exists(LAST_DECISIONS_PATH):
            return {}
        try:
            with open(LAST_DECISIONS_PATH, 'r') as f:
                payload = json.load(f)
            source = payload.get('decisions') or []
        except Exception as e:
            logger.error(f"WebUI: error reading {LAST_DECISIONS_PATH} for decisions detail map: {e}")
            return {}

    mapping = {}
    for d in source:
        if not isinstance(d, dict):
            continue
        symbol = d.get('symbol')
        if symbol:
            mapping[symbol] = {
                'decision': d.get('decision'),
                'quantity': d.get('quantity', 0),
                'rationale': d.get('rationale', ''),
            }
    return mapping


app = Flask(__name__)
# Local single-user app — session data does not need to survive restart.
app.secret_key = os.urandom(24)
# CSRF protection on all POST forms — defense in depth even though the bind is
# 127.0.0.1-only. Without this, a malicious page opened in the same browser
# could cross-site POST to our forms.
csrf = CSRFProtect(app)


def _build_watchlists_view():
    """Fetch ALL of the user's watchlists from Robinhood.

    The dashboard is a UI for managing watchlists, so it should reflect
    Robinhood's actual state — including any list the user just created
    via this UI.

    Returns a list of dicts: {'name': str, 'symbols': [str, ...], 'error': str|None}
    """
    watchlists = []
    try:
        all_lists = robinhood.get_all_watchlists()
        names = [w.get('display_name') for w in all_lists if isinstance(w, dict) and w.get('display_name')]
    except Exception as e:
        logger.error(f"WebUI: error loading all watchlists: {e}")
        return [{'name': '?', 'symbols': [], 'error': f'Could not list watchlists: {e}'}]

    for name in names:
        try:
            stocks = robinhood.get_watchlist_stocks(name)
            symbols = [s.get('symbol') for s in stocks if isinstance(s, dict) and s.get('symbol')]
            watchlists.append({'name': name, 'symbols': symbols, 'error': None})
        except Exception as e:
            logger.error(f"Error loading watchlist '{name}': {e}")
            watchlists.append({'name': name, 'symbols': [], 'error': str(e)})
    return watchlists


def _build_portfolio_view():
    """Return a list of holding dicts suitable for templating."""
    try:
        holdings = robinhood.get_portfolio_stocks()
    except Exception as e:
        logger.error(f"Error loading portfolio stocks: {e}")
        return [], str(e), 0.0

    decisions_map = _load_decisions_map()
    rows = []
    total_value = 0.0
    for symbol, data in (holdings or {}).items():
        try:
            price = float(data.get('price', 0) or 0)
            qty = float(data.get('quantity', 0) or 0)
            avg = float(data.get('average_buy_price', 0) or 0)
            position_value = price * qty
            total_value += position_value
            rows.append({
                'symbol': symbol,
                'quantity': round(qty, 6),
                'current_price': round(price, 2),
                'avg_buy_price': round(avg, 2),
                'position_value': round(position_value, 2),
                'recommendation': decisions_map.get(symbol),
            })
        except Exception as e:
            logger.error(f"Error parsing holding {symbol}: {e}")
            continue
    rows.sort(key=lambda r: r['symbol'])
    return rows, None, round(total_value, 2)


def _build_account_view():
    try:
        info = robinhood.get_account_info()
        buying_power = info.get('buying_power')
        return {'buying_power': buying_power, 'error': None}
    except Exception as e:
        logger.error(f"Error loading account info: {e}")
        return {'buying_power': None, 'error': str(e)}


def _build_recommendations_from_decisions(decisions, timestamp, market_open_at_cycle, portfolio_symbols=None):
    """Common logic to shape a decisions list into the template view dict.

    Used by both the in-process state path and the JSON file fallback.
    """
    empty = {
        'available': False,
        'timestamp': None,
        'age_seconds': None,
        'stale': False,
        'market_open_at_cycle': None,
        'rows': [],
        'hold_count': 0,
        'error': None,
    }

    if decisions is None:
        return empty

    # Compute age from timestamp
    age_seconds = None
    if isinstance(timestamp, str):
        try:
            ts_norm = timestamp.replace('Z', '+00:00')
            file_dt = datetime.fromisoformat(ts_norm)
            age_seconds = int((datetime.now(timezone.utc) - file_dt).total_seconds())
        except Exception as e:
            logger.error(f"WebUI: error parsing timestamp '{timestamp}': {e}")

    # Stale if older than the cadence the bot would have used.
    stale = False
    if age_seconds is not None:
        threshold = RUN_INTERVAL_SECONDS if market_open_at_cycle else AFTER_HOURS_INTERVAL_SECONDS
        stale = age_seconds > threshold

    held = set(portfolio_symbols or [])
    rows = []
    for d in decisions:
        if not isinstance(d, dict):
            continue
        decision = d.get('decision')
        symbol = d.get('symbol')
        if decision == 'hold':
            continue
        if decision in ('sell', 'strong_sell') and symbol not in held:
            continue
        if decision not in ('strong_buy', 'buy', 'sell', 'strong_sell'):
            continue
        try:
            quantity = float(d.get('quantity', 0) or 0)
        except (TypeError, ValueError):
            quantity = 0.0
        rows.append({
            'symbol': symbol,
            'decision': decision,
            'quantity': quantity,
        })

    order = {'strong_buy': 0, 'buy': 1, 'sell': 2, 'strong_sell': 3}
    rows.sort(key=lambda r: (order.get(r['decision'], 4), r['symbol'] or ''))

    return {
        'available': True,
        'timestamp': timestamp,
        'age_seconds': age_seconds,
        'stale': stale,
        'market_open_at_cycle': bool(market_open_at_cycle) if market_open_at_cycle is not None else None,
        'rows': rows,
        'hold_count': 0,
        'error': None,
    }


def _build_recommendations_view(portfolio_symbols=None):
    """Load the most recent cycle's AI decisions, filtered for actionable items.

    Shows only buy recommendations and sell recommendations for stocks
    currently in the portfolio. Holds are excluded — they remain visible
    in the Recommendation column of the Portfolio and Watchlist tables.
    """
    empty = {
        'available': False,
        'timestamp': None,
        'age_seconds': None,
        'stale': False,
        'market_open_at_cycle': None,
        'rows': [],
        'hold_count': 0,
        'error': None,
    }

    # Try in-process shared state first
    state = trading_state.snapshot()
    if state['decisions']:
        return _build_recommendations_from_decisions(
            state['decisions'],
            state['last_cycle_time'],
            state['market_open'],
            portfolio_symbols=portfolio_symbols,
        )

    # Fall back to JSON file
    if not os.path.exists(LAST_DECISIONS_PATH):
        return empty

    try:
        with open(LAST_DECISIONS_PATH, 'r') as f:
            payload = json.load(f)
    except Exception as e:
        logger.error(f"WebUI: error reading {LAST_DECISIONS_PATH}: {e}")
        result = dict(empty)
        result['error'] = str(e)
        return result

    return _build_recommendations_from_decisions(
        payload.get('decisions') or [],
        payload.get('timestamp'),
        payload.get('market_open'),
        portfolio_symbols=portfolio_symbols,
    )


@app.route('/')
def index():
    account = _build_account_view()
    portfolio_rows, portfolio_error, portfolio_total = _build_portfolio_view()
    portfolio_symbols = [r['symbol'] for r in portfolio_rows]
    watchlists = _build_watchlists_view()
    recommendations = _build_recommendations_view(portfolio_symbols=portfolio_symbols)
    decisions_map = _load_decisions_map()
    loop_status = trading_state.snapshot()
    stock_stats = db.get_all_stock_stats()
    return render_template(
        'index.html',
        account=account,
        portfolio_rows=portfolio_rows,
        portfolio_error=portfolio_error,
        portfolio_total=portfolio_total,
        watchlists=watchlists,
        recommendations=recommendations,
        decisions_map=decisions_map,
        loop_status=loop_status,
        stock_stats=stock_stats,
    )


@app.route('/watchlists/create', methods=['POST'])
def create_watchlist():
    name = (request.form.get('name') or '').strip()
    logger.info(f"WebUI: create_watchlist submitted name='{name}'")
    if not name:
        flash('Watchlist name is required.', 'error')
        return redirect(url_for('index'))
    try:
        result = robinhood.create_watchlist(name)
        if result.get('ok'):
            flash(f"Created watchlist '{name}'.", 'success')
        else:
            flash(f"Could not create watchlist '{name}': {result.get('error')}", 'error')
    except Exception as e:
        logger.error(f"WebUI: create_watchlist exception: {e}")
        flash(f"Error creating watchlist: {e}", 'error')
    return redirect(url_for('index'))


@app.route('/watchlists/<name>/add', methods=['POST'])
def add_to_watchlist(name):
    symbol = (request.form.get('symbol') or '').strip().upper()
    logger.info(f"WebUI: add_to_watchlist submitted name='{name}' symbol='{symbol}'")
    if not symbol:
        flash('Symbol is required.', 'error')
        return redirect(url_for('index'))
    try:
        result = robinhood.add_stock_to_watchlist(name, symbol)
        if result.get('ok'):
            flash(f"Added {symbol} to '{name}'.", 'success')
        else:
            flash(f"Could not add {symbol} to '{name}': {result.get('error')}", 'error')
    except Exception as e:
        logger.error(f"WebUI: add_to_watchlist exception: {e}")
        flash(f"Error adding {symbol} to '{name}': {e}", 'error')
    return redirect(url_for('index'))


@app.route('/watchlists/<name>/remove', methods=['POST'])
def remove_from_watchlist(name):
    symbol = (request.form.get('symbol') or '').strip().upper()
    logger.info(f"WebUI: remove_from_watchlist submitted name='{name}' symbol='{symbol}'")
    if not symbol:
        flash('Symbol is required.', 'error')
        return redirect(url_for('index'))
    try:
        result = robinhood.remove_stock_from_watchlist(name, symbol)
        if result.get('ok'):
            flash(f"Removed {symbol} from '{name}'.", 'success')
        else:
            flash(f"Could not remove {symbol} from '{name}': {result.get('error')}", 'error')
    except Exception as e:
        logger.error(f"WebUI: remove_from_watchlist exception: {e}")
        flash(f"Error removing {symbol} from '{name}': {e}", 'error')
    return redirect(url_for('index'))


# ---- API routes for trading loop control ----

@app.route('/api/status')
def api_status():
    """Return the current trading loop status as JSON."""
    state = trading_state.snapshot()
    return jsonify({
        'loop_running': state['loop_running'],
        'last_cycle_time': state['last_cycle_time'],
        'last_cycle_error': state['last_cycle_error'],
        'market_open': state['market_open'],
        'logged_in': state['logged_in'],
    })


@app.route('/api/stock/<symbol>')
@csrf.exempt  # JSON API endpoint — CSRF token not applicable
def api_stock_detail(symbol):
    """Return aggregated detail for a single stock.

    Combines Robinhood holdings data, the latest AI decision + rationale,
    and cached ticker details from SQLite.  Used by the stock detail modal.
    """
    symbol = symbol.upper()
    result = {'symbol': symbol}

    # --- Position data from Robinhood + price from Massive API ---
    rh_data = None
    try:
        holdings = robinhood.get_portfolio_stocks()
        if holdings and symbol in holdings:
            stock = holdings[symbol]
            qty = float(stock.get('quantity', 0) or 0)
            avg = float(stock.get('average_buy_price', 0) or 0)
            # Get current price from Massive API
            price = massive_client.fetch_current_price(symbol) or float(stock.get('price', 0) or 0)
            rh_data = {
                'current_price': round(price, 2),
                'quantity': round(qty, 6),
                'avg_buy_price': round(avg, 2),
                'position_value': round(price * qty, 2),
            }
        else:
            # Not in portfolio — get current price from Massive API
            price = massive_client.fetch_current_price(symbol)
            if price:
                rh_data = {
                    'current_price': round(price, 2),
                    'quantity': 0,
                    'avg_buy_price': 0,
                    'position_value': 0,
                }
    except Exception as e:
        logger.error(f"WebUI: api_stock_detail error fetching data for {symbol}: {e}")
    result['robinhood'] = rh_data

    # --- AI decision + rationale ---
    detail_map = _load_decisions_detail_map()
    result['ai_decision'] = detail_map.get(symbol)

    # --- Ticker details from SQLite ---
    ticker_row = db.get_ticker_by_symbol(symbol)
    result['ticker'] = ticker_row

    # --- Historical data status ---
    result['history_status'] = db.get_stock_history_status(symbol)

    # --- Backtest stats (auto-compute if history exists but stats don't) ---
    stats = db.get_stock_stats(symbol)
    if not stats and result['history_status'] and result['history_status']['has_data']:
        logger.info(f"WebUI: auto-computing signals/stats for {symbol} (history exists, no stats)")
        db.compute_indicators(symbol)
        stats = db.get_stock_stats(symbol)
    result['stats'] = stats

    return jsonify(result)


@app.route('/api/stock/<symbol>/analyze', methods=['POST'])
@csrf.exempt
def api_stock_analyze(symbol):
    """On-demand single-stock AI analysis with historical storage.

    Fetches enrichment data from Robinhood, sends to Claude for a
    buy/sell/hold decision with rationale, stores the result in
    stock_analysis table, and returns the decision.
    """
    symbol = symbol.upper()
    logger.info(f"WebUI: on-demand analysis requested for {symbol}")
    try:
        account_info = robinhood.get_account_info()

        # Position data from Robinhood
        holdings = robinhood.get_portfolio_stocks()
        if holdings and symbol in holdings:
            stock_data = robinhood.extract_my_stocks_data(holdings[symbol])
        else:
            stock_data = {
                'current_price': 0,
                'my_quantity': 0,
                'my_average_buy_price': 0,
            }

        # Current price and market data from Massive API
        logger.info(f"WebUI: fetching market data from Massive API for {symbol}...")
        price = massive_client.fetch_current_price(symbol)
        if price:
            stock_data['current_price'] = round(price, 2)
        massive_client.enrich_stock_data(symbol, stock_data)

        # Analyst ratings from Robinhood (account-specific)
        ratings_data = robinhood.get_ratings(symbol)
        stock_data = robinhood.enrich_with_analyst_ratings(stock_data, ratings_data)
        logger.info(f"WebUI: market data enriched for {symbol}, sending to Claude...")

        prompt = (
            f"Analyze this single stock and provide a buy, sell, or hold recommendation.\n\n"
            f"**Stock Data:**\n```json\n{json.dumps({symbol: stock_data}, indent=1)}\n```\n\n"
            f"**Account Buying Power:** ${account_info.get('buying_power', 'unknown')}\n\n"
            f"**Response Format:**\n"
            f'Return exactly one JSON object: {{"symbol": "{symbol}", "decision": "<strong_buy|buy|hold|sell|strong_sell>", '
            f'"quantity": <number>, "rationale": "<detailed explanation referencing RSI, VWAP, '
            f'moving averages, analyst ratings, and any other relevant data points>"}}\n\n'
            f"Provide only the JSON output with no additional text."
        )

        ai_response = claude.make_ai_request(prompt)
        logger.info(f"WebUI: Claude response received for {symbol}, parsing...")
        result = claude.parse_ai_response(ai_response)

        if isinstance(result, list) and len(result) > 0:
            result = result[0]
        elif not isinstance(result, dict):
            result = {'symbol': symbol, 'decision': 'hold', 'quantity': 0, 'rationale': str(result)}

        # Store in stock_analysis with source='on_demand'
        analyzed_at = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        analyst_summary = stock_data.get('analyst_summary')
        try:
            db.insert_stock_analysis({
                'symbol': symbol,
                'analyzed_at': analyzed_at,
                'decision': result.get('decision', ''),
                'quantity': result.get('quantity'),
                'rationale': result.get('rationale'),
                'price': stock_data.get('current_price'),
                'rsi': stock_data.get('rsi'),
                'vwap': stock_data.get('vwap'),
                'ma_50': stock_data.get('50_day_mavg_price'),
                'ma_200': stock_data.get('200_day_mavg_price'),
                'analyst_summary': json.dumps(analyst_summary) if analyst_summary else None,
                'held_quantity': stock_data.get('my_quantity'),
                'held_avg_price': stock_data.get('my_average_buy_price'),
                'source': 'on_demand',
            })
        except Exception as e:
            logger.error(f"WebUI: error storing on-demand analysis for {symbol}: {e}")

        logger.info(f"WebUI: analysis complete for {symbol}: {result.get('decision', '?')}")
        return jsonify({'ok': True, 'analysis': result})
    except Exception as e:
        logger.error(f"WebUI: on-demand analysis error for {symbol}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/stock/<symbol>/load-history', methods=['POST'])
@csrf.exempt
def api_stock_load_history(symbol):
    """Fetch 2 years of daily OHLCV aggregates from Massive API and store them."""
    symbol = symbol.upper()
    logger.info(f"WebUI: loading 2-year history for {symbol}")

    try:
        bars = massive_client.fetch_daily_bars(symbol, days=730)
        if not bars:
            return jsonify({'ok': False, 'error': f'No data returned for {symbol}'}), 404

        bars_loaded = db.upsert_stock_history(symbol, bars)
        logger.info(f"WebUI: loaded {bars_loaded} bars for {symbol}")

        indicators_computed = db.compute_indicators(symbol)
        logger.info(f"WebUI: computed indicators for {symbol} ({indicators_computed} bars)")

        from_date = bars[0]['bar_date'] if bars else '?'
        to_date = bars[-1]['bar_date'] if bars else '?'
        return jsonify({
            'ok': True,
            'bars_loaded': bars_loaded,
            'indicators_computed': indicators_computed,
            'from_date': from_date,
            'to_date': to_date,
        })
    except Exception as e:
        logger.error(f"WebUI: error loading history for {symbol}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/stock/<symbol>/compute-indicators', methods=['POST'])
@csrf.exempt
def api_compute_indicators(symbol):
    """Compute technical indicators for a stock's existing history."""
    symbol = symbol.upper()
    logger.info(f"WebUI: computing indicators for {symbol}")
    try:
        count = db.compute_indicators(symbol)
        return jsonify({'ok': True, 'bars_computed': count})
    except Exception as e:
        logger.error(f"WebUI: error computing indicators for {symbol}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/stock/<symbol>/compute-signals', methods=['POST'])
@csrf.exempt
def api_compute_signals(symbol):
    """Run rule-based signal scoring on a stock's history (no LLM needed)."""
    symbol = symbol.upper()
    logger.info(f"WebUI: computing signals for {symbol}")
    try:
        count = db.compute_signals(symbol)
        return jsonify({'ok': True, 'bars_scored': count})
    except Exception as e:
        logger.error(f"WebUI: error computing signals for {symbol}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/loop/start', methods=['POST'])
@csrf.exempt  # JSON API endpoint — CSRF token not applicable
def api_loop_start():
    """Start the trading loop. Returns JSON status."""
    started = trading_loop.start()
    return jsonify({
        'ok': True,
        'started': started,
        'loop_running': trading_loop.is_running(),
    })


@app.route('/api/loop/stop', methods=['POST'])
@csrf.exempt  # JSON API endpoint — CSRF token not applicable
def api_loop_stop():
    """Stop the trading loop. Returns JSON status."""
    stopped = trading_loop.stop()
    return jsonify({
        'ok': True,
        'stopped': stopped,
        'loop_running': trading_loop.is_running(),
    })


# ---- Stock Research page ----

@app.route('/api/stock/<symbol>/backtest-analyze', methods=['POST'])
@csrf.exempt
def api_backtest_analyze(symbol):
    """Batch-analyze historical bars using Claude Haiku.

    Sends unanalyzed bars in batches of 20 to Claude Haiku for
    buy/sell/hold decisions. Stores recommendations in the
    stock_history table. Skips previously analyzed rows.
    """
    symbol = symbol.upper()
    BATCH_SIZE = 20

    logger.info(f"WebUI: backtest analysis requested for {symbol}")

    try:
        unanalyzed = db.get_unanalyzed_bars(symbol)
        if not unanalyzed:
            return jsonify({'ok': True, 'analyzed': 0, 'message': 'All bars already analyzed'})

        logger.info(f"WebUI: {len(unanalyzed)} unanalyzed bars for {symbol}")

        from anthropic import Anthropic
        haiku_client = Anthropic(api_key=claude.client.api_key)

        total_analyzed = 0
        for i in range(0, len(unanalyzed), BATCH_SIZE):
            batch = unanalyzed[i:i + BATCH_SIZE]

            bars_data = []
            for bar in batch:
                bars_data.append({
                    'date': bar['bar_date'],
                    'open': bar['open'],
                    'high': bar['high'],
                    'low': bar['low'],
                    'close': bar['close'],
                    'volume': bar['volume'],
                    'vwap': bar['vwap'],
                })

            prompt = (
                f"You are analyzing historical daily price data for {symbol}.\n\n"
                f"For each trading day below, provide a buy/sell/hold recommendation "
                f"based on the OHLCV data and price action patterns.\n\n"
                f"**Data:**\n```json\n{json.dumps(bars_data, indent=1)}\n```\n\n"
                f"**Response Format:**\nReturn a JSON array with one entry per day:\n"
                f'[{{"date": "YYYY-MM-DD", "recommendation": "strong_buy|buy|hold|sell|strong_sell"}}]\n\n'
                f"Provide only the JSON output with no additional text."
            )

            resp = haiku_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=2048,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
            decisions = claude.parse_ai_response(ai_text)

            updates = []
            for d in decisions:
                if isinstance(d, dict) and d.get('date') and d.get('recommendation'):
                    updates.append({
                        'bar_date': d['date'],
                        'recommendation': d['recommendation'],
                    })

            if updates:
                db.update_bar_recommendations(symbol, updates)
                total_analyzed += len(updates)

            logger.info(f"WebUI: backtest batch {i//BATCH_SIZE + 1} done, {total_analyzed} analyzed so far")

        logger.info(f"WebUI: backtest complete for {symbol}: {total_analyzed} bars analyzed")
        return jsonify({'ok': True, 'analyzed': total_analyzed})
    except Exception as e:
        logger.error(f"WebUI: backtest analysis error for {symbol}: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/history/<symbol>')
def stock_history_page(symbol):
    """Full-page stock price history with chart and data table."""
    symbol = symbol.upper()
    bars = db.get_stock_history_bars(symbol)
    status = db.get_stock_history_status(symbol)
    ticker = db.get_ticker_by_symbol(symbol)
    analyzed_count = sum(1 for b in bars if b.get('recommendation') or b.get('signal_synthesis'))
    unanalyzed_count = sum(1 for b in bars if not b.get('recommendation') and not b.get('signal_synthesis'))
    has_indicators = any(b.get('sma_10') is not None for b in bars)
    has_signals = any(b.get('signal_synthesis') is not None for b in bars)

    # Backtest simulation: walk oldest→newest
    # Use signal_synthesis exclusively when signals have been computed
    INITIAL_CAPITAL = 100.0
    capital = INITIAL_CAPITAL
    shares_held = 0.0
    state = 'waiting_to_buy'  # waiting_to_buy | holding
    buy_markers = []   # [{x: index, y: price}]
    sell_markers = []  # [{x: index, y: price}]

    for i, bar in enumerate(bars):
        bar['bt_shares_bought'] = None
        bar['bt_shares_sold'] = None
        bar['bt_sale_amount'] = None
        bar['bt_running_total'] = None
        # Use signal_synthesis when available; only fall back to recommendation
        # if NO signals have been computed at all for this stock
        if has_signals:
            rec = (bar.get('signal_synthesis') or '').lower()
        else:
            rec = (bar.get('recommendation') or '').lower()

        if state == 'waiting_to_buy' and rec in ('buy', 'strong_buy'):
            close_price = bar.get('close') or 0
            if close_price > 0 and capital > 0:
                shares_held = capital / close_price
                bar['bt_shares_bought'] = round(shares_held, 6)
                bar['bt_running_total'] = round(capital, 2)
                buy_markers.append({'x': i, 'y': close_price, 'date': bar['bar_date']})
                state = 'holding'
            else:
                bar['bt_running_total'] = round(capital, 2)
        elif state == 'holding' and rec in ('sell', 'strong_sell'):
            open_price = bar.get('open') or 0
            sale_amount = shares_held * open_price
            bar['bt_shares_sold'] = round(shares_held, 6)
            bar['bt_sale_amount'] = round(sale_amount, 2)
            capital = sale_amount
            bar['bt_running_total'] = round(capital, 2)
            sell_markers.append({'x': i, 'y': open_price, 'date': bar['bar_date']})
            shares_held = 0.0
            state = 'waiting_to_buy'
        else:
            if state == 'holding' and shares_held > 0:
                bar['bt_running_total'] = round(shares_held * (bar.get('close') or 0), 2)
            else:
                bar['bt_running_total'] = round(capital, 2)

    bt_final = capital if state == 'waiting_to_buy' else round(shares_held * (bars[-1].get('close') or 0), 2) if bars else INITIAL_CAPITAL
    bt_return_pct = round((bt_final - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1) if INITIAL_CAPITAL > 0 else 0
    bt_trades = len(sell_markers)

    return render_template(
        'history.html',
        symbol=symbol,
        ticker_name=ticker.get('name') if ticker else symbol,
        bars=bars,
        bars_json=json.dumps(bars),
        status=status,
        analyzed_count=analyzed_count,
        unanalyzed_count=unanalyzed_count,
        has_signals=has_signals,
        buy_markers_json=json.dumps(buy_markers),
        sell_markers_json=json.dumps(sell_markers),
        bt_final=bt_final,
        bt_return_pct=bt_return_pct,
        bt_trades=bt_trades,
        has_indicators=has_indicators,
        initial_capital=INITIAL_CAPITAL,
    )


RESEARCH_PAGE_SIZE = 50


@app.route('/research')
def research():
    """Render the Stock Research page with search, filter, and pagination."""
    q = request.args.get('q', '').strip() or None
    market_filter = request.args.get('market', '').strip() or None
    type_filter = request.args.get('type', '').strip() or None
    active_raw = request.args.get('active', '').strip()

    active_filter = None
    if active_raw in ('0', '1'):
        active_filter = int(active_raw)

    page = request.args.get('page', 1, type=int)
    if page < 1:
        page = 1
    offset = (page - 1) * RESEARCH_PAGE_SIZE

    tickers = db.search_tickers(
        query=q, market=market_filter, type_=type_filter,
        active=active_filter, limit=RESEARCH_PAGE_SIZE, offset=offset,
    )
    ticker_count = db.get_ticker_count(
        query=q, market=market_filter, type_=type_filter, active=active_filter,
    )
    total_pages = max(1, (ticker_count + RESEARCH_PAGE_SIZE - 1) // RESEARCH_PAGE_SIZE)
    loaded_at = db.get_loaded_at()

    # Distinct values for filter dropdowns
    markets = db.get_distinct_values('market')
    types = db.get_distinct_values('type')

    return render_template(
        'research.html',
        tickers=tickers,
        ticker_count=ticker_count,
        total_pages=total_pages,
        page=page,
        q=q,
        market_filter=market_filter,
        type_filter=type_filter,
        active_filter=active_filter,
        active_raw=active_raw,
        loaded_at=loaded_at,
        markets=markets,
        types=types,
    )


@app.route('/api/tickers/load', methods=['POST'])
@csrf.exempt  # JSON API endpoint — CSRF token not applicable
def api_tickers_load():
    """Fetch all tickers from the Massive API and store in SQLite.

    Streams from the API iterator and batches inserts (1000 rows per batch).
    Returns JSON with the count of tickers loaded.
    """
    BATCH_SIZE = 1000

    def _ticker_to_row(t):
        return {
            'ticker':               getattr(t, 'ticker', None),
            'name':                 getattr(t, 'name', None),
            'market':               getattr(t, 'market', None),
            'locale':               getattr(t, 'locale', None),
            'type':                 getattr(t, 'type', None),
            'active':               int(getattr(t, 'active', False)) if getattr(t, 'active', None) is not None else None,
            'currency_name':        getattr(t, 'currency_name', None),
            'currency_symbol':      getattr(t, 'currency_symbol', None),
            'base_currency_symbol': getattr(t, 'base_currency_symbol', None),
            'base_currency_name':   getattr(t, 'base_currency_name', None),
            'cik':                  getattr(t, 'cik', None),
            'composite_figi':       getattr(t, 'composite_figi', None),
            'share_class_figi':     getattr(t, 'share_class_figi', None),
            'primary_exchange':     getattr(t, 'primary_exchange', None),
            'last_updated_utc':     getattr(t, 'last_updated_utc', None),
            'delisted_utc':         getattr(t, 'delisted_utc', None),
            'source_feed':          getattr(t, 'source_feed', None),
        }

    try:
        tickers_iter = massive_client.fetch_all_tickers(limit=BATCH_SIZE)
        batch = []
        total = 0
        for t in tickers_iter:
            batch.append(_ticker_to_row(t))
            if len(batch) >= BATCH_SIZE:
                count = db.upsert_tickers(batch)
                total += count
                logger.info(f"WebUI: loaded {total} tickers so far...")
                batch = []
        if batch:
            count = db.upsert_tickers(batch)
            total += count
        logger.info(f"WebUI: loaded {total} tickers from Massive API")
        return jsonify({'ok': True, 'count': total})
    except Exception as e:
        logger.error(f"WebUI: error loading tickers: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    warnings.warn(
        "Running webui.py directly is deprecated. Use 'python app.py' for the "
        "unified application (Flask web UI + trading loop). webui.py standalone "
        "mode will continue to work but may be removed in a future release.",
        DeprecationWarning,
        stacklevel=1,
    )
    # Authenticate to Robinhood once at startup. login_to_robinhood is async.
    logger.info("WebUI: logging in to Robinhood...")
    login_resp = asyncio.run(robinhood.login_to_robinhood())
    if not login_resp:
        logger.error("WebUI: Robinhood login failed; exiting.")
        raise SystemExit(1)
    logger.info("WebUI: Robinhood login successful.")

    # IMPORTANT: bind ONLY to localhost. This app has no authentication —
    # exposing it on 0.0.0.0 (or any external interface) would give anyone
    # on the network unauthenticated access to the Robinhood account.
    logger.info(f"WebUI: starting Flask on http://127.0.0.1:{WEBUI_PORT} (local-only)...")
    app.run(host='127.0.0.1', port=WEBUI_PORT, debug=False)
