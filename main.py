import time
import warnings
from datetime import datetime, timezone
import json
import os
import asyncio

from config import *
# Defensive fallback for users whose config.py predates AFTER_HOURS_INTERVAL_SECONDS
try:
    AFTER_HOURS_INTERVAL_SECONDS
except NameError:
    AFTER_HOURS_INTERVAL_SECONDS = 3600

from src.api import robinhood
from src.api import claude
from src.state import trading_state
from src.utils import logger


# Get AI amount guidelines
def get_ai_amount_guidelines():
    sell_guidelines = []
    if MIN_SELLING_AMOUNT_USD is not False:
        sell_guidelines.append(f"Minimum amount {MIN_SELLING_AMOUNT_USD} USD")
    if MAX_SELLING_AMOUNT_USD is not False:
        sell_guidelines.append(f"Maximum amount {MAX_SELLING_AMOUNT_USD} USD")
    sell_guidelines = ", ".join(sell_guidelines) if sell_guidelines else None

    buy_guidelines = []
    if MIN_BUYING_AMOUNT_USD is not False:
        buy_guidelines.append(f"Minimum amount {MIN_BUYING_AMOUNT_USD} USD")
    if MAX_BUYING_AMOUNT_USD is not False:
        buy_guidelines.append(f"Maximum amount {MAX_BUYING_AMOUNT_USD} USD")
    buy_guidelines = ", ".join(buy_guidelines) if buy_guidelines else None

    return sell_guidelines, buy_guidelines


# Make AI-based decisions on stock portfolio and watchlist
def make_ai_decisions(account_info, portfolio_overview, watchlist_overview):
    constraints = [
        f"- Initial budget: {account_info['buying_power']} USD",
        f"- Max portfolio size: {PORTFOLIO_LIMIT} stocks",
    ]
    sell_guidelines, buy_guidelines = get_ai_amount_guidelines()
    if sell_guidelines:
        constraints.append(f"- Sell Amounts Guidelines: {sell_guidelines}")
    if buy_guidelines:
        constraints.append(f"- Buy Amounts Guidelines: {buy_guidelines}")
    if len(TRADE_EXCEPTIONS) > 0:
        constraints.append(f"- Excluded stocks: {', '.join(TRADE_EXCEPTIONS)}")

    ai_prompt = (
        "**Context:**\n"
        f"Today is {datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')}.{chr(10)}"
        f"You are a short-term investment advisor managing a stock portfolio.{chr(10)}"
        f"You analyze market conditions every {RUN_INTERVAL_SECONDS} seconds and make investment decisions.{chr(10)}{chr(10)}"
        "**Constraints:**\n"
        f"{chr(10).join(constraints)}"
        "\n\n"
        "**Stock Data:**\n"
        "```json\n"
        f"{json.dumps({**portfolio_overview, **watchlist_overview}, indent=1)}{chr(10)}"
        "```\n\n"
        "**Response Format:**\n"
        "Return your decisions in a JSON array with this structure:\n"
        "```json\n"
        "[\n"
        '  {"symbol": <symbol>, "decision": <decision>, "quantity": <quantity>, "rationale": <rationale>},\n'
        "  ...\n"
        "]\n"
        "```\n"
        "- <symbol>: Stock symbol.\n"
        "- <decision>: One of `buy`, `sell`, or `hold`.\n"
        "- <quantity>: Recommended transaction quantity.\n"
        "- <rationale>: A brief explanation of WHY this decision was made, referencing specific data points (e.g. RSI, VWAP, moving averages, analyst ratings) that influenced the decision.\n\n"
        "**Instructions:**\n"
        "- Provide only the JSON output with no additional text.\n"
        "- Return a decision for EVERY stock in the data — use `hold` with quantity 0 for stocks that should not be traded.\n"
        "- Do NOT omit stocks — every symbol must appear in the response."
    )
    logger.debug(f"AI making-decisions prompt:{chr(10)}{ai_prompt}")
    ai_response = claude.make_ai_request(ai_prompt)
    logger.debug(f"AI making-decisions response:{chr(10)}{ai_response.strip()}")
    decisions = claude.parse_ai_response(ai_response)
    return decisions


# Filter AI hallucinations
def filter_ai_hallucinations(account_info, portfolio_overview, watchlist_overview, decisions_data):
    filtered_decisions = []

    for decision in decisions_data:
        symbol = decision.get('symbol')
        decision_type = decision.get('decision')
        quantity = decision.get('quantity', 0)

        # Filter decisions for stocks in TRADE_EXCEPTIONS
        if symbol in TRADE_EXCEPTIONS:
            logger.debug(f"Filtering out {decision_type} decision for {symbol} - in TRADE_EXCEPTIONS")
            continue

        # Filter sell decisions with 0 quantity
        if decision_type == "sell" and quantity == 0:
            logger.debug(f"Filtering out sell decision for {symbol} with 0 quantity")
            continue

        # Filter buy decisions with 0 quantity
        if decision_type == "buy" and quantity == 0:
            logger.debug(f"Filtering out buy decision for {symbol} with 0 quantity")
            continue

        # Get stock data from either portfolio or watchlist
        stock_data = portfolio_overview.get(symbol) or watchlist_overview.get(symbol)
        if not stock_data:
            logger.debug(f"Filtering out decision for {symbol} - not found in portfolio or watchlist")
            continue

        # Filter buy decisions with is_buy_pdt_restricted == True
        if decision_type == "buy" and stock_data.get("is_buy_pdt_restricted", False):
            logger.debug(f"Filtering out buy decision for {symbol} due to PDT restriction")
            continue

        # Filter sell decisions with is_sell_pdt_restricted == True
        if decision_type == "sell" and stock_data.get("is_sell_pdt_restricted", False):
            logger.debug(f"Filtering out sell decision for {symbol} due to PDT restriction")
            continue

        filtered_decisions.append(decision)

    logger.debug(f"Filtered out {len(decisions_data) - len(filtered_decisions)} decision(s)")
    return filtered_decisions


# Persist the latest filtered AI decisions to disk for the web UI to consume.
#
# Contract (see also webui._build_recommendations_view):
#   - File:        data/last-decisions.json
#   - Atomicity:   write to .tmp first, then os.replace -> the webui never sees
#                  a half-written file (os.replace is atomic on POSIX + Windows
#                  when source/dest are on the same filesystem, which they are
#                  here because both live under ./data/).
#   - Shape:       {timestamp: ISO8601-Z, market_open: bool, decisions: [...]}
#   - Decisions:   the FULL filtered list including 'hold' entries. Holds are
#                  filtered out at READ time by the webui — keeping holds on disk
#                  lets other consumers (debugging, future analytics) see them.
#   - Errors:      any IO failure is logged and swallowed. A failed write must
#                  not kill the trading loop.
def write_last_decisions(decisions_data, market_open):
    # Update in-process shared state so the web UI gets live data
    # without a disk round-trip.
    normalized = [
        {
            'symbol': d.get('symbol'),
            'decision': d.get('decision'),
            'quantity': d.get('quantity', 0),
            'rationale': d.get('rationale', ''),
        }
        for d in (decisions_data or [])
    ]
    trading_state.update(
        decisions=normalized,
        market_open=bool(market_open),
    )

    # Also write to disk as a side-effect for debugging / external tools.
    try:
        os.makedirs('data', exist_ok=True)
        payload = {
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'market_open': bool(market_open),
            'decisions': normalized,
        }
        final_path = os.path.join('data', 'last-decisions.json')
        tmp_path = final_path + '.tmp'
        with open(tmp_path, 'w') as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, final_path)
    except Exception as e:
        logger.error(f"Error writing last-decisions.json: {e}")



# Main trading bot function
def trading_bot(market_open=None):
    # market_open is passed in from main() to avoid a second is_market_open()
    # round-trip per cycle. None signals "caller didn't tell us" — we fall back
    # to a fresh check so trading_bot() stays callable on its own (tests, ad-hoc).
    if market_open is None:
        market_open = robinhood.is_market_open()

    logger.info("Getting account info...")
    account_info = robinhood.get_account_info()

    logger.info("Getting portfolio stocks...")
    portfolio_stocks = robinhood.get_portfolio_stocks()

    logger.debug(f"Portfolio stocks total: {len(portfolio_stocks)}")

    portfolio_stocks_value = 0
    for stock in portfolio_stocks.values():
        portfolio_stocks_value += float(stock['price']) * float(stock['quantity'])
    portfolio = [f"{symbol} ({round(float(stock['price']) * float(stock['quantity']) / portfolio_stocks_value * 100, 2)}%)" for symbol, stock in portfolio_stocks.items()]
    logger.info(f"Portfolio stocks to proceed: {'None' if len(portfolio) == 0 else ', '.join(portfolio)}")

    logger.info("Prepare portfolio stocks for AI analysis...")
    portfolio_overview = {}
    for symbol, stock_data in portfolio_stocks.items():
        historical_data_day = robinhood.get_historical_data(symbol, interval="5minute", span="day")
        historical_data_year = robinhood.get_historical_data(symbol, interval="day", span="year")
        ratings_data = robinhood.get_ratings(symbol)
        portfolio_overview[symbol] = robinhood.extract_my_stocks_data(stock_data)
        portfolio_overview[symbol] = robinhood.enrich_with_rsi(portfolio_overview[symbol], historical_data_day, symbol)
        portfolio_overview[symbol] = robinhood.enrich_with_vwap(portfolio_overview[symbol], historical_data_day, symbol)
        portfolio_overview[symbol] = robinhood.enrich_with_moving_averages(portfolio_overview[symbol], historical_data_year, symbol)
        portfolio_overview[symbol] = robinhood.enrich_with_analyst_ratings(portfolio_overview[symbol], ratings_data)
        portfolio_overview[symbol] = robinhood.enrich_with_pdt_restrictions(portfolio_overview[symbol], symbol)

    logger.info("Getting all watchlist stocks from Robinhood...")
    watchlist_stocks = []
    try:
        all_lists = robinhood.get_all_watchlists()
        watchlist_names = [w.get('display_name') for w in all_lists if isinstance(w, dict) and w.get('display_name')]
        logger.info(f"Found {len(watchlist_names)} watchlists: {', '.join(watchlist_names)}")
        for watchlist_name in watchlist_names:
            try:
                stocks = robinhood.get_watchlist_stocks(watchlist_name)
                watchlist_stocks.extend(stocks)
                watchlist_stocks = [dict(t) for t in {tuple(d.items()) for d in watchlist_stocks}]
            except Exception as e:
                logger.error(f"Error getting watchlist stocks for {watchlist_name}: {e}")
    except Exception as e:
        logger.error(f"Error fetching watchlists from Robinhood: {e}")

    logger.debug(f"Watchlist stocks total: {len(watchlist_stocks)}")

    watchlist_overview = {}
    if len(watchlist_stocks) > 0:
        logger.debug(f"Removing portfolio stocks from watchlist...")
        watchlist_stocks = [stock for stock in watchlist_stocks if stock['symbol'] not in portfolio_stocks.keys()]

        logger.info(f"Watchlist stocks to proceed: {', '.join([stock['symbol'] for stock in watchlist_stocks])}")

        logger.info("Prepare watchlist overview for AI analysis...")
        for stock_data in watchlist_stocks:
            symbol = stock_data['symbol']
            historical_data_day = robinhood.get_historical_data(symbol, interval="5minute", span="day")
            historical_data_year = robinhood.get_historical_data(symbol, interval="day", span="year")
            ratings_data = robinhood.get_ratings(symbol)
            watchlist_overview[symbol] = robinhood.extract_watchlist_data(stock_data)
            watchlist_overview[symbol] = robinhood.enrich_with_rsi(watchlist_overview[symbol], historical_data_day, symbol)
            watchlist_overview[symbol] = robinhood.enrich_with_vwap(watchlist_overview[symbol], historical_data_day, symbol)
            watchlist_overview[symbol] = robinhood.enrich_with_moving_averages(watchlist_overview[symbol], historical_data_year, symbol)
            watchlist_overview[symbol] = robinhood.enrich_with_analyst_ratings(watchlist_overview[symbol], ratings_data)
            watchlist_overview[symbol] = robinhood.enrich_with_pdt_restrictions(watchlist_overview[symbol], symbol)

    if len(portfolio_overview) == 0 and len(watchlist_overview) == 0:
        logger.warning("No stocks to analyze, skipping AI-based decision-making...")
        # Still persist an empty decisions snapshot so the dashboard reflects
        # the most recent cycle rather than showing data from an earlier run.
        write_last_decisions([], market_open)
        return {}

    decisions_data = []
    trading_results = {}

    try:
        logger.info("Making AI-based decision...")
        decisions_data = make_ai_decisions(account_info, portfolio_overview, watchlist_overview)
    except Exception as e:
        logger.error(f"Error making AI-based decision: {e}")

    logger.info("Filtering AI hallucinations...")
    decisions_data = filter_ai_hallucinations(account_info, portfolio_overview, watchlist_overview, decisions_data)

    # Persist the FULL filtered list (incl. holds) for the web UI dashboard.
    # We do this even when there are no actionable trades, so the dashboard
    # reflects "the bot ran a cycle and found nothing to do" rather than
    # showing stale recommendations from a previous cycle.
    write_last_decisions(decisions_data, market_open)

    if len(decisions_data) == 0:
        logger.info("No decisions to execute")
        return trading_results

    logger.info("Executing decisions...")

    for decision_data in decisions_data:
        symbol = decision_data['symbol']
        decision = decision_data['decision']
        quantity = decision_data['quantity']
        logger.info(f"{symbol} > Decision: {decision} of {quantity}")

        if decision == "sell":
            try:
                sell_resp = robinhood.sell_stock(symbol, quantity)
                if sell_resp and 'id' in sell_resp:
                    if sell_resp['id'] == "demo":
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "sell", "result": "success", "details": "Demo mode"}
                        logger.info(f"{symbol} > Demo > Sold {quantity} stocks")
                    elif sell_resp['id'] == "market_closed":
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "sell", "result": "market_closed", "details": "Market closed; analysis-only mode"}
                        logger.info(f"{symbol} > Market closed > Would have sold {quantity} stocks")
                    elif sell_resp['id'] == "cancelled":
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "sell", "result": "cancelled", "details": "Cancelled by user"}
                        logger.info(f"{symbol} > Sell cancelled by user")
                    else:
                        details = robinhood.extract_sell_response_data(sell_resp)
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "sell", "result": "success", "details": details}
                        logger.info(f"{symbol} > Sold {quantity} stocks")
                else:
                    details = sell_resp['detail'] if 'detail' in sell_resp else sell_resp
                    trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "sell", "result": "error", "details": details}
                    logger.error(f"{symbol} > Error selling: {details}")
            except Exception as e:
                trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "sell", "result": "error", "details": str(e)}
                logger.error(f"{symbol} > Error selling: {e}")

        if decision == "buy":
            try:
                buy_resp = robinhood.buy_stock(symbol, quantity)
                if buy_resp and 'id' in buy_resp:
                    if buy_resp['id'] == "demo":
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "buy", "result": "success", "details": "Demo mode"}
                        logger.info(f"{symbol} > Demo > Bought {quantity} stocks")
                    elif buy_resp['id'] == "market_closed":
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "buy", "result": "market_closed", "details": "Market closed; analysis-only mode"}
                        logger.info(f"{symbol} > Market closed > Would have bought {quantity} stocks")
                    elif buy_resp['id'] == "cancelled":
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "buy", "result": "cancelled", "details": "Cancelled by user"}
                        logger.info(f"{symbol} > Buy cancelled by user")
                    else:
                        details = robinhood.extract_buy_response_data(buy_resp)
                        trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "buy", "result": "success", "details": details}
                        logger.info(f"{symbol} > Bought {quantity} stocks")
                else:
                    details = buy_resp['detail'] if 'detail' in buy_resp else buy_resp
                    trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "buy", "result": "error", "details": details}
                    logger.error(f"{symbol} > Error buying: {details}")
            except Exception as e:
                trading_results[symbol] = {"symbol": symbol, "quantity": quantity, "decision": "buy", "result": "error", "details": str(e)}
                logger.error(f"{symbol} > Error buying: {e}")

    return trading_results


# Run trading bot in a loop
async def main():
    robinhood_token_expiry = 0

    while True:
        try:
            # Check if Robinhood token needs refresh (refresh 5 minutes before expiry)
            if time.time() >= robinhood_token_expiry - 300:
                logger.info("Login to Robinhood...")
                login_resp = await robinhood.login_to_robinhood()
                if not login_resp or 'expires_in' not in login_resp:
                    raise Exception("Failed to login to Robinhood")
                robinhood_token_expiry = time.time() + login_resp['expires_in']
                logger.info(f"Successfully logged in. Token expires in {login_resp['expires_in']} seconds")

            market_open = robinhood.is_market_open()
            if market_open:
                run_interval_seconds = RUN_INTERVAL_SECONDS
                logger.info(f"Market is open, running trading bot in {MODE} mode...")
            else:
                run_interval_seconds = AFTER_HOURS_INTERVAL_SECONDS
                logger.info(f"Market is closed, running analysis only (no order placement) in {MODE} mode...")

            trading_results = trading_bot(market_open=market_open)

            sold_stocks = [f"{result['symbol']} ({result['quantity']})" for result in trading_results.values() if result['decision'] == "sell" and result['result'] == "success"]
            bought_stocks = [f"{result['symbol']} ({result['quantity']})" for result in trading_results.values() if result['decision'] == "buy" and result['result'] == "success"]
            would_have_sold = [f"{result['symbol']} ({result['quantity']})" for result in trading_results.values() if result['decision'] == "sell" and result['result'] == "market_closed"]
            would_have_bought = [f"{result['symbol']} ({result['quantity']})" for result in trading_results.values() if result['decision'] == "buy" and result['result'] == "market_closed"]
            errors = [f"{result['symbol']} ({result['details']})" for result in trading_results.values() if result['result'] == "error"]
            logger.info(f"Sold: {'None' if len(sold_stocks) == 0 else ', '.join(sold_stocks)}")
            logger.info(f"Bought: {'None' if len(bought_stocks) == 0 else ', '.join(bought_stocks)}")
            if would_have_sold:
                logger.info(f"Would have sold (market closed): {', '.join(would_have_sold)}")
            if would_have_bought:
                logger.info(f"Would have bought (market closed): {', '.join(would_have_bought)}")
            logger.info(f"Errors: {'None' if len(errors) == 0 else ', '.join(errors)}")
        except Exception as e:
            run_interval_seconds = 60
            logger.error(f"Trading bot error: {e}")

        logger.info(f"Waiting for {run_interval_seconds} seconds...")
        time.sleep(run_interval_seconds)


# Run the main function (deprecated — use ``python app.py`` instead)
if __name__ == '__main__':
    warnings.warn(
        "Running main.py directly is deprecated. Use 'python app.py' for the "
        "unified application (Flask web UI + trading loop). main.py will "
        "continue to work but may be removed in a future release.",
        DeprecationWarning,
        stacklevel=1,
    )
    confirm = input(f"Are you sure you want to run the bot in {MODE} mode? (yes/no): ")
    if confirm.lower() != "yes":
        logger.warning("Exiting the bot...")
        exit()
    asyncio.run(main())

