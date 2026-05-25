# Algorithms

## Overview

The trading bot uses two independent analysis systems:

1. **Rule-based signal engine** (`src/signals.py` + `src/db.py`) -- Pure Python technical analysis with deterministic scoring. No API calls, no LLM. Operates on pre-computed indicators stored in `stock_history`.
2. **Claude LLM analysis** (`src/api/claude.py` + `main.py`) -- Sends enriched stock data to Claude for natural-language buy/sell/hold decisions with rationale.

The rule-based engine is the primary signal source for the stock screener, history charts, and backtest simulations. Claude LLM analysis powers the automated trading loop and on-demand single-stock analysis.

---

## Technical Indicators (16 Columns)

All indicators are computed in `src/db.py::compute_indicators()` using pure Python math -- no pandas, no numpy, no external TA libraries. The function reads OHLCV data from `stock_history`, computes all 16 indicators, and writes them back to the same rows.

### Simple Moving Averages (SMA)

**Implementation:** For each bar at index `i`, the SMA of period `p` is the arithmetic mean of `data[i-p+1 : i+1]`. Values are `NULL` for the first `p-1` bars.

| Indicator | Column | Period | Formula |
|---|---|---|---|
| SMA(10) | `sma_10` | 10 | `sum(close[i-9:i+1]) / 10` |
| SMA(20) | `sma_20` | 20 | `sum(close[i-19:i+1]) / 20` |
| SMA(50) | `sma_50` | 50 | `sum(close[i-49:i+1]) / 50` |
| SMA(200) | `sma_200` | 200 | `sum(close[i-199:i+1]) / 200` |

### Exponential Moving Averages (EMA)

**Implementation:** The EMA uses a smoothing factor `k = 2 / (period + 1)`. The first EMA value is seeded with the SMA of the first `period` bars. Subsequent values: `EMA[i] = close[i] * k + EMA[i-1] * (1 - k)`.

| Indicator | Column | Period | Smoothing Factor |
|---|---|---|---|
| EMA(12) | `ema_12` | 12 | `k = 2/13 = 0.1538` |
| EMA(26) | `ema_26` | 26 | `k = 2/27 = 0.0741` |

### RSI (Relative Strength Index)

**Column:** `rsi_14`
**Period:** 14

**Implementation:**
1. Compute daily price changes: `diff[i] = close[i] - close[i-1]`
2. Separate into gains (positive diffs) and losses (absolute value of negative diffs)
3. Initial average gain/loss = simple average of first 14 gains/losses
4. Subsequent values use Wilder's smoothing: `avg_gain[i] = (avg_gain[i-1] * 13 + gain[i]) / 14`
5. `RS = avg_gain / avg_loss`
6. `RSI = 100 - (100 / (1 + RS))`
7. If `avg_loss == 0`, RSI = 100

First RSI value appears at bar index 14 (requires 15 bars of data). Range: 0 to 100.

### MACD (Moving Average Convergence Divergence)

| Component | Column | Formula |
|---|---|---|
| MACD Line | `macd_line` | `EMA(12) - EMA(26)` |
| Signal Line | `macd_signal` | 9-period EMA of MACD Line (k = 2/10 = 0.2) |
| Histogram | `macd_histogram` | `MACD Line - Signal Line` |

**Implementation:** MACD Line is computed for every bar where both EMA(12) and EMA(26) are available (bar index >= 25). The Signal Line requires 9 valid MACD values before it can start. The Histogram is only computed where both MACD Line and Signal Line exist.

### Bollinger Bands

| Component | Column | Formula |
|---|---|---|
| Upper Band | `bb_upper` | `SMA(20) + 2 * StdDev(20)` |
| Lower Band | `bb_lower` | `SMA(20) - 2 * StdDev(20)` |
| Band Width | `bb_width` | `(bb_upper - bb_lower) / SMA(20) * 100` |

**Implementation:** Standard deviation is computed as population StdDev (not sample) over the 20-bar window: `sqrt(sum((x - mean)^2) / N)`. Band Width is expressed as a percentage of the middle band (SMA(20)).

### Volume Indicators

| Indicator | Column | Formula |
|---|---|---|
| Volume SMA(20) | `vol_sma_20` | 20-day simple moving average of volume |
| Volume Ratio | `vol_ratio` | `volume[i] / vol_sma_20[i]` (> 1.0 = above average) |
| OBV | `obv` | Cumulative: add volume on up days, subtract on down days |

**OBV Implementation:**
- `obv[0] = 0`
- If `close[i] > close[i-1]`: `obv[i] = obv[i-1] + volume[i]`
- If `close[i] < close[i-1]`: `obv[i] = obv[i-1] - volume[i]`
- If `close[i] == close[i-1]`: `obv[i] = obv[i-1]`

---

## Signal Scoring Engine (7 Sections)

Implemented in `src/signals.py::compute_signals_for_bars()`. Each bar is evaluated through 6 scoring functions (Sections 1-6), each returning a numeric score from -2 to +2:

| Score | Label |
|---|---|
| +2 | `strong_buy` |
| +1 | `buy` |
| 0 | `hold` |
| -1 | `sell` |
| -2 | `strong_sell` |

Section 7 synthesizes the total. The first 14 bars in any series are automatically set to `hold` / score 0 (insufficient history context).

### Section 1: Moving Average Crossover (`score_ma`)

**Column:** `signal_ma`

Evaluates trend direction using SMA crossovers.

**Rules (evaluated in priority order):**

1. **Triple MA alignment** (highest priority):
   - `SMA(10) > SMA(50) > SMA(200)` --> score = +2 (strong buy)
   - `SMA(10) < SMA(50) < SMA(200)` --> score = -2 (strong sell)

2. **Golden/Death Cross** (SMA(50) vs SMA(200), lookback 5 bars):
   - SMA(50) crosses above SMA(200) --> score = +2 (Golden Cross)
   - SMA(50) crosses below SMA(200) --> score = -2 (Death Cross)

3. **Short-term crossover** (SMA(10) vs SMA(50), lookback 3 bars):
   - SMA(10) crosses above SMA(50) --> score = +1
   - SMA(10) crosses below SMA(50) --> score = -1

4. **Long-term filter** (applied last):
   - If close < SMA(200) and score > 0, downgrade to 0 (do not buy below 200-day MA)

### Section 2: RSI (`score_rsi`)

**Column:** `signal_rsi`

Evaluates momentum and overbought/oversold conditions.

**Rules (evaluated in priority order):**

1. **Oversold bounce:** Previous RSI < 30, current RSI >= 30 --> score = +1
2. **Overbought reversal:** Previous RSI > 70, current RSI <= 70 --> score = -1
3. **Extreme oversold:** RSI < 20 --> score = +1
4. **Extreme overbought:** RSI > 80 --> score = -1
5. **Bullish divergence** (10-bar lookback): Price makes a new low in the last 3 bars, but RSI low is higher than earlier RSI low by > 3 points --> score = +2
6. **Bearish divergence** (10-bar lookback): Price makes a new high in the last 3 bars, but RSI high is lower than earlier RSI high by > 3 points --> score = -2

### Section 3: MACD (`score_macd`)

**Column:** `signal_macd`

Evaluates trend and momentum via MACD crossovers.

**Rules (evaluated in priority order):**

1. **Bullish crossover:** MACD Line crosses above Signal Line --> score = +1 (or +2 if MACD > 0)
2. **Bearish crossover:** MACD Line crosses below Signal Line --> score = -1 (or -2 if MACD < 0)
3. **Zero-line cross** (if no crossover detected):
   - MACD crosses above 0 --> score = +1
   - MACD crosses below 0 --> score = -1
4. **Divergence** (10-bar lookback, if score still 0):
   - Price new low but MACD higher low --> score = +2 (bullish divergence)
   - Price new high but MACD lower high --> score = -2 (bearish divergence)

### Section 4: RSI + MACD Combined (`score_rsi_macd`)

**Column:** `signal_rsi_macd`

Dual-confirmation -- only fires strong signals when both RSI and MACD agree.

**Rules:**

| RSI Score | MACD Score | Combined Score | Meaning |
|---|---|---|---|
| > 0 | > 0 | +2 | Both bullish -- strong buy |
| < 0 | < 0 | -2 | Both bearish -- strong sell |
| > 0 | < 0 | 0 | Conflicting -- no trade |
| < 0 | > 0 | 0 | Conflicting -- no trade |
| > 0 | 0 | +1 | One directional, one neutral -- weak buy |
| 0 | > 0 | +1 | One directional, one neutral -- weak buy |
| < 0 | 0 | -1 | One directional, one neutral -- weak sell |
| 0 | < 0 | -1 | One directional, one neutral -- weak sell |
| 0 | 0 | 0 | Both neutral |

### Section 5: Bollinger Bands (`score_bb`)

**Column:** `signal_bb`

Evaluates volatility, mean reversion, and breakout patterns.

**Rules:**

1. **Squeeze detection:** If current `bb_width` is within 10% of the minimum `bb_width` over the last 120 bars (6 months), the market is in a squeeze.

2. **Breakout mode** (squeeze active or bb_width > 8):
   - Close > Upper Band AND vol_ratio > 1.5 --> score = +2 (bullish breakout)
   - Close < Lower Band AND vol_ratio > 1.5 --> score = -2 (bearish breakout)
   - Close > Upper Band AND vol_ratio < 1.0 --> score = 0 (false breakout, no chase)

3. **Mean reversion mode:**
   - Previous close <= Lower Band, current close > Lower Band --> score = +1 (bounce off lower)
   - Previous close >= Upper Band, current close < Upper Band --> score = -1 (rejection at upper)

4. **W-Bottom pattern** (20-bar lookback):
   - Two or more touches of the Lower Band where the second low is higher than the first, and the most recent touch is within the last 3 bars --> score = +2

### Section 6: Volume Confirmation (`score_volume`)

**Column:** `signal_volume`

Evaluates whether price moves are supported by volume.

**Rules:**

1. **Climactic reversal:** vol_ratio > 3.0 at a 20-bar price low with recovery --> score = +2 (capitulation)
2. **Confirmed up move:** Close > previous close AND vol_ratio > 1.5 --> score = +1
3. **Confirmed down move:** Close < previous close AND vol_ratio > 1.5 --> score = -1
4. **Suspect move:** Price change > 2% but vol_ratio < 0.8 --> score = 0
5. **OBV divergence** (5-bar lookback):
   - Price rising but OBV falling --> score = -1 (distribution / institutional exit)
   - Price falling but OBV rising --> score = +1 (accumulation)

### Section 7: Synthesis (`signal_synthesis` and `signal_score`)

The total score is the sum of all 6 section scores. Theoretical range: -14 to +14 (since the RSI+MACD combined section re-evaluates RSI and MACD, the effective range could be wider in theory but is capped by the individual section ranges).

**Score-to-label mapping** (`_synthesis_label`):

| Total Score Range | Label |
|---|---|
| >= +7 | `strong_buy` |
| +3 to +6 | `buy` |
| -2 to +2 | `hold` |
| -6 to -3 | `sell` |
| <= -7 | `strong_sell` |

The `signal_synthesis` label is also written to the `recommendation` column on each bar.

### Per-Section Score-to-Label Mapping

Individual section scores are stored as text labels using `_score_to_label`:

| Score | Label |
|---|---|
| >= +2 | `strong_buy` |
| +1 | `buy` |
| 0 | `hold` |
| -1 | `sell` |
| <= -2 | `strong_sell` |

---

## Backtest Simulation

Implemented in `src/db.py::_run_backtest()` and `compute_backtest_stats()`, plus a client-side version in `webui.py::stock_history_page()`.

### Simulation Rules

1. **Initial capital:** $100.00
2. **State machine:** Two states -- `waiting_to_buy` and `holding`
3. **Buy trigger:** When in `waiting_to_buy` state and `signal_synthesis` is `buy` or `strong_buy`:
   - Buy at the bar's **close** price
   - Invest all available capital: `shares = capital / close_price`
4. **Sell trigger:** When in `holding` state and `signal_synthesis` is `sell` or `strong_sell`:
   - Sell at the bar's **open** price (simulates next-day execution)
   - `capital = shares * open_price`
   - This counts as one completed trade
5. **End-of-period:** If still holding at the end, final value is `shares * last_close_price`
6. **Return:** `(final_value - 100) / 100 * 100` expressed as a percentage

### Backtest Periods

| Period | Bars Used | Stored If |
|---|---|---|
| 1-year | Last 252 bars | Always |
| 2-year (full) | All available bars | Only if >= 400 bars available |

### Client-Side Backtest (History Page)

The history page (`webui.py::stock_history_page()`) runs the same simulation but also generates buy/sell marker coordinates for the interactive chart and per-bar running totals for the data table.

---

## Claude LLM Analysis

### Trading Loop Analysis (`main.py::make_ai_decisions`)

Used during automated trading loop cycles. Sends ALL portfolio and watchlist stocks to Claude in a single prompt.

**Input:** JSON-encoded stock data including:
- Current price, held quantity, average buy price
- RSI, VWAP, 50-day and 200-day moving averages (from Massive API intraday bars)
- Analyst ratings (from Robinhood)
- Account buying power and constraints

**Model:** Configured via `ANTHROPIC_MODEL_NAME` (default: `claude-sonnet-4-5`)

**Output:** JSON array of decisions, one per stock:
```json
{"symbol": "AAPL", "decision": "buy", "quantity": 2, "rationale": "RSI at 32 showing oversold..."}
```

**Post-processing:** `filter_ai_hallucinations()` removes:
- Decisions for excluded symbols (`TRADE_EXCEPTIONS`)
- Buy/sell decisions with quantity = 0
- Stocks not found in portfolio or watchlist data
- Stocks with PDT (pattern day trade) restrictions

### On-Demand Analysis (`webui.py::api_stock_analyze`)

Triggered by the user clicking "Request Analysis" in the stock detail modal.

**Model:** Same as trading loop (`ANTHROPIC_MODEL_NAME`)

**Differences from loop analysis:**
- Analyzes a single stock (not the full portfolio)
- Stored with `source = 'on_demand'` in `stock_analysis`
- No hallucination filtering applied
- No trade execution

### Haiku Backtest Analysis (`webui.py::api_backtest_analyze`)

Batch-analyzes ALL historical bars for a symbol using Claude Haiku for comparison with the rule-based signals.

**Model:** `claude-haiku-4-5-20251001` (hardcoded)

**Process:**
1. Fetches all bars for the symbol
2. Sends bars in batches of 20 to Claude Haiku
3. Each batch returns a JSON array of `{date, recommendation}` pairs
4. Recommendations are written to `stock_history.recommendation` via `update_bar_recommendations()`

**Key difference from rule-based signals:** Haiku sees only raw OHLCV data (open, high, low, close, volume, vwap) without pre-computed indicators. It makes decisions based on price action patterns rather than explicit indicator thresholds.

---

## Comparison: Rule-Based vs. LLM Analysis

| Aspect | Rule-Based Signals | Claude LLM Analysis |
|---|---|---|
| **Source code** | `src/signals.py` | `src/api/claude.py` + `main.py` |
| **Input** | Pre-computed indicators (16 columns) | Enriched stock data (price, RSI, VWAP, MAs, analyst ratings) |
| **Cost** | Zero (pure Python math) | API cost per call |
| **Speed** | Milliseconds per stock | Seconds per stock |
| **Determinism** | Fully deterministic | Non-deterministic |
| **Explainability** | Exact rules and thresholds documented | Natural-language rationale |
| **Used for** | Screener, history charts, backtests, dashboard recommendations | Trading loop decisions, on-demand analysis |
| **Decision levels** | strong_buy / buy / hold / sell / strong_sell | strong_buy / buy / hold / sell / strong_sell |
| **Historical analysis** | All 2 years computed in seconds | Haiku batch mode available (slow, costs money) |
