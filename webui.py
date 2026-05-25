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


from src.api import robinhood
from src.api import claude
from src.api import massive_client
from src import db
from src.state import trading_state
from src.trading import loop as trading_loop
from src.utils import logger


def _build_decisions_map_from_stats(all_stats):
    """Build a symbol -> decision mapping from stock_stats.

    Single source of truth for recommendations across the entire UI.
    """
    return {
        symbol: stats.get('latest_signal')
        for symbol, stats in all_stats.items()
        if stats.get('latest_signal')
    }


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

    all_stats = db.get_all_stock_stats()
    rows = []
    total_value = 0.0
    for symbol, data in (holdings or {}).items():
        try:
            price = float(data.get('price', 0) or 0)
            qty = float(data.get('quantity', 0) or 0)
            avg = float(data.get('average_buy_price', 0) or 0)
            position_value = price * qty
            total_value += position_value
            stats = all_stats.get(symbol)
            rows.append({
                'symbol': symbol,
                'quantity': round(qty, 6),
                'current_price': round(price, 2),
                'avg_buy_price': round(avg, 2),
                'position_value': round(position_value, 2),
                'recommendation': stats.get('latest_signal') if stats else None,
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


def _build_recommendations_view(all_stats, portfolio_symbols=None):
    """Build actionable recommendations from stock_stats.

    Shows buy/strong_buy recommendations and sell/strong_sell for stocks
    currently in the portfolio. Holds are excluded.
    """
    held = set(portfolio_symbols or [])
    rows = []
    for symbol, stats in all_stats.items():
        decision = stats.get('latest_signal')
        if not decision or decision == 'hold':
            continue
        if decision in ('sell', 'strong_sell') and symbol not in held:
            continue
        if decision not in ('strong_buy', 'buy', 'sell', 'strong_sell'):
            continue
        rows.append({
            'symbol': symbol,
            'decision': decision,
            'quantity': 0,
        })

    order = {'strong_buy': 0, 'buy': 1, 'sell': 2, 'strong_sell': 3}
    rows.sort(key=lambda r: (order.get(r['decision'], 4), r['symbol'] or ''))

    return {
        'available': len(all_stats) > 0,
        'rows': rows,
        'error': None,
    }


@app.route('/')
def index():
    account = _build_account_view()
    portfolio_rows, portfolio_error, portfolio_total = _build_portfolio_view()
    portfolio_symbols = [r['symbol'] for r in portfolio_rows]
    watchlists = _build_watchlists_view()
    stock_stats = db.get_all_stock_stats()
    recommendations = _build_recommendations_view(stock_stats, portfolio_symbols=portfolio_symbols)
    decisions_map = _build_decisions_map_from_stats(stock_stats)
    loop_status = trading_state.snapshot()
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

    # --- Current recommendation (from rule-based signals) ---
    # Show the latest signal from stock_stats. Rationale is only populated
    # when user explicitly clicks "Request Analysis" (LLM call).
    if stats and stats.get('latest_signal'):
        result['ai_decision'] = {
            'decision': stats['latest_signal'],
            'quantity': 0,
            'rationale': None,
        }
    else:
        result['ai_decision'] = None

    return jsonify(result)


@app.route('/api/stock/<symbol>/analyze', methods=['POST'])
@csrf.exempt
def api_stock_analyze(symbol):
    """On-demand single-stock AI analysis using pre-computed DB data.

    Reads all indicators from stock_history (already computed), sends to
    Claude Sonnet for a decision with rationale. No live API calls needed.
    """
    symbol = symbol.upper()
    logger.info(f"WebUI: on-demand analysis requested for {symbol}")
    try:
        # Get latest bar with all indicators from SQLite (instant)
        bars = db.get_stock_history_bars(symbol)
        stats = db.get_stock_stats(symbol)
        ticker = db.get_ticker_by_symbol(symbol)

        if not bars:
            return jsonify({'ok': False, 'error': f'No history data for {symbol}. Load history first.'}), 404

        last_bar = bars[-1]
        stock_data = {
            'symbol': symbol,
            'name': ticker.get('name') if ticker else symbol,
            'current_price': last_bar.get('close'),
            'bar_date': last_bar.get('bar_date'),
            'open': last_bar.get('open'),
            'high': last_bar.get('high'),
            'low': last_bar.get('low'),
            'close': last_bar.get('close'),
            'volume': last_bar.get('volume'),
            'sma_10': last_bar.get('sma_10'),
            'sma_20': last_bar.get('sma_20'),
            'sma_50': last_bar.get('sma_50'),
            'sma_200': last_bar.get('sma_200'),
            'ema_12': last_bar.get('ema_12'),
            'ema_26': last_bar.get('ema_26'),
            'rsi_14': last_bar.get('rsi_14'),
            'macd_line': last_bar.get('macd_line'),
            'macd_signal': last_bar.get('macd_signal'),
            'macd_histogram': last_bar.get('macd_histogram'),
            'bb_upper': last_bar.get('bb_upper'),
            'bb_lower': last_bar.get('bb_lower'),
            'bb_width': last_bar.get('bb_width'),
            'vol_ratio': last_bar.get('vol_ratio'),
            'obv': last_bar.get('obv'),
            'market_cap': ticker.get('market_cap') if ticker else None,
            'signal_score': last_bar.get('signal_score'),
            'signal_synthesis': last_bar.get('signal_synthesis'),
        }

        # Add extension % for context
        if stock_data.get('sma_200') and stock_data['sma_200'] > 0 and stock_data.get('close'):
            stock_data['extension_above_sma200_pct'] = round(
                (stock_data['close'] - stock_data['sma_200']) / stock_data['sma_200'] * 100, 1
            )

        # Add backtest context
        if stats:
            stock_data['backtest_1yr_return'] = stats.get('bt_1yr_return_pct')
            stock_data['backtest_2yr_return'] = stats.get('backtest_return_pct')

        # Add recent price context (last 5 bars)
        if len(bars) >= 5:
            stock_data['recent_5_day_prices'] = [
                {'date': b['bar_date'], 'close': b['close']} for b in bars[-5:]
            ]

        # Remove None values for cleaner prompt
        stock_data = {k: v for k, v in stock_data.items() if v is not None}

        prompt = (
            f"You are a systematic technical analyst. Analyze this stock using the data below.\n\n"
            f"**Technical Analysis Framework:**\n"
            f"1. MA Crossover: SMA alignment, Golden/Death Cross, overextension (>30% above SMA200 = caution)\n"
            f"2. RSI: <30 oversold, >70 overbought, divergence signals\n"
            f"3. MACD: histogram direction, crossovers, zero-line position\n"
            f"4. RSI+MACD Combined: both must agree for strong conviction\n"
            f"5. Bollinger Bands: position within bands, band width\n"
            f"6. Volume: ratio vs 20-day avg, OBV trend\n"
            f"7. Synthesis: count aligned signals for conviction\n\n"
            f"**Stock Data (all indicators pre-computed):**\n```json\n{json.dumps(stock_data, indent=1)}\n```\n\n"
            f"**Response Format:**\n"
            f'Return exactly one JSON object: {{"symbol": "{symbol}", "decision": "<strong_buy|buy|hold|sell|strong_sell>", '
            f'"quantity": 0, "rationale": "<detailed explanation referencing the specific indicator values above>"}}\n\n'
            f"Be specific — reference actual numbers from the data. Provide only JSON."
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
        try:
            db.insert_stock_analysis({
                'symbol': symbol,
                'analyzed_at': analyzed_at,
                'decision': result.get('decision', ''),
                'quantity': result.get('quantity'),
                'rationale': result.get('rationale'),
                'price': stock_data.get('close'),
                'rsi': stock_data.get('rsi_14'),
                'vwap': None,
                'ma_50': stock_data.get('sma_50'),
                'ma_200': stock_data.get('sma_200'),
                'analyst_summary': None,
                'held_quantity': 0,
                'held_avg_price': 0,
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
    """Batch-analyze ALL historical bars using Claude Haiku.

    Sends bars in batches of 20 to Claude Haiku for buy/sell/hold
    decisions. Overwrites all existing recommendations so results
    are fully from Haiku.
    """
    symbol = symbol.upper()
    BATCH_SIZE = 20

    logger.info(f"WebUI: Haiku recompute requested for {symbol}")

    try:
        unanalyzed = db.get_all_bars_for_analysis(symbol)
        if not unanalyzed:
            return jsonify({'ok': True, 'analyzed': 0, 'message': 'No bars to analyze'})

        logger.info(f"WebUI: {len(unanalyzed)} unanalyzed bars for {symbol}")

        from anthropic import Anthropic
        haiku_client = Anthropic(api_key=claude.client.api_key)

        total_analyzed = 0
        for i in range(0, len(unanalyzed), BATCH_SIZE):
            batch = unanalyzed[i:i + BATCH_SIZE]

            bars_data = []
            for bar in batch:
                entry = {
                    'date': bar['bar_date'],
                    'open': bar['open'],
                    'high': bar['high'],
                    'low': bar['low'],
                    'close': bar['close'],
                    'volume': bar['volume'],
                }
                if bar.get('sma_50') is not None:
                    entry['sma_50'] = round(bar['sma_50'], 2)
                if bar.get('sma_200') is not None:
                    entry['sma_200'] = round(bar['sma_200'], 2)
                if bar.get('rsi_14') is not None:
                    entry['rsi_14'] = round(bar['rsi_14'], 1)
                if bar.get('macd_histogram') is not None:
                    entry['macd_histogram'] = round(bar['macd_histogram'], 3)
                if bar.get('bb_upper') is not None:
                    entry['bb_upper'] = round(bar['bb_upper'], 2)
                if bar.get('bb_lower') is not None:
                    entry['bb_lower'] = round(bar['bb_lower'], 2)
                if bar.get('vol_ratio') is not None:
                    entry['vol_ratio'] = round(bar['vol_ratio'], 2)
                bars_data.append(entry)

            prompt = (
                f"You are a systematic technical analyst evaluating {symbol}.\n\n"
                f"**Analysis Framework (apply these rules):**\n"
                f"1. MA Crossover: SMA(50) vs SMA(200) alignment. Triple alignment (price>50>200) = bullish trend. Price below SMA(200) = no longs.\n"
                f"2. RSI: Below 30 crossing up = buy. Above 70 crossing down = sell.\n"
                f"3. MACD: Histogram positive and growing = bullish momentum. Negative and falling = bearish. Crossovers confirm direction.\n"
                f"4. RSI+MACD Combined: Both must agree for strong signals. Conflicting = hold.\n"
                f"5. Bollinger Bands: Close above upper band with low volume = caution. Close below lower band = potential bounce.\n"
                f"6. Volume: Vol_ratio > 1.5 confirms breakouts. Below 0.8 = suspect move.\n"
                f"7. Overextension: If close is >30% above SMA(200), do NOT issue strong_buy — cap at buy. Sell signals still apply normally.\n"
                f"8. Conviction: strong_buy requires 5+ bullish signals aligned. buy requires 3-4. hold = mixed. sell requires 3-4 bearish. strong_sell requires 5+ bearish.\n\n"
                f"**Data (with pre-computed indicators):**\n```json\n{json.dumps(bars_data, indent=1)}\n```\n\n"
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


@app.route('/stock/<symbol>')
def stock_detail_page(symbol):
    """Full-page stock detail — reads ONLY from SQLite for instant load."""
    symbol = symbol.upper()
    ticker = db.get_ticker_by_symbol(symbol)
    stats = db.get_stock_stats(symbol)
    history_status = db.get_stock_history_status(symbol)
    return render_template(
        'stock_detail.html',
        symbol=symbol,
        ticker=ticker,
        stats=stats,
        history_status=history_status,
    )


@app.route('/history/<symbol>')
def stock_history_page(symbol):
    """Full-page stock price history with chart and data table."""
    symbol = symbol.upper()
    bars = db.get_stock_history_bars(symbol)
    status = db.get_stock_history_status(symbol)
    ticker = db.get_ticker_by_symbol(symbol)
    analyzed_count = sum(1 for b in bars if b.get('recommendation'))
    unanalyzed_count = len(bars) - analyzed_count
    has_indicators = any(b.get('sma_10') is not None for b in bars)
    has_signals = any(b.get('signal_score') is not None for b in bars)

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


# ---- Documentation pages ----

_DOCS = {
    'overview': {'file': 'docs/project-overview.md', 'title': 'Project Overview'},
    'schema': {'file': 'docs/database-schema.md', 'title': 'Database Schema'},
    'algorithms': {'file': 'docs/algorithms.md', 'title': 'Algorithms'},
    'framework': {'file': 'docs/technical-analysis-framework.md', 'title': 'Technical Analysis Framework'},
}


@app.route('/docs')
def docs_index():
    return render_template('docs.html', docs=_DOCS, slug=None, title=None, content=None)


@app.route('/docs/<slug>')
def docs_page(slug):
    doc = _DOCS.get(slug)
    if not doc:
        return redirect(url_for('docs_index'))
    try:
        with open(doc['file'], 'r') as f:
            content = f.read()
    except FileNotFoundError:
        content = f"*File `{doc['file']}` not found.*"
    return render_template('docs.html', docs=_DOCS, slug=slug, title=doc['title'], content=content)


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


@app.route('/screener')
def screener():
    """Render the Stock Screener page — all analyzed stocks with search/sort."""
    stocks = db.get_screener_stocks()
    return render_template('screener.html', stocks=stocks)


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
