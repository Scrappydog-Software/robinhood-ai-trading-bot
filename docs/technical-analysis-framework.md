# Stock Analysis Prompt — Buy/Sell Signal Evaluation

You are a systematic technical analyst. Analyze the stock provided below using the rule-based signal framework defined in this document. Work through each indicator section in order, state whether each signal fires (BUY / SELL / NEUTRAL / N/A), and conclude with a synthesized overall assessment.

-----

## Input Data Required

Provide the following daily OHLCV data for the stock:

- **Ticker:** `[e.g. AAPL]`
- **Date range:** Minimum 200 trading days recommended (for SMA-200 and long-term context)
- **Fields per day:** Date, Open, High, Low, Close, Volume

If you have fewer than 200 days, note which indicators cannot be fully computed and skip them.

-----

## Analysis Instructions

For each section below:

1. Compute the indicator from the data provided
1. Evaluate whether the signal condition is met **as of the most recent trading day**
1. Label the signal: `BUY` / `SELL` / `NEUTRAL` / `WATCH` / `N/A (insufficient data)`
1. Briefly explain *why* the signal fired or did not

-----

## Section 1 — Moving Average Crossover (Trend Direction)

### Calculations Required

- SMA(10): 10-day simple moving average of Close
- SMA(50): 50-day simple moving average of Close
- SMA(200): 200-day simple moving average of Close
- EMA(12): 12-day exponential moving average of Close
- EMA(26): 26-day exponential moving average of Close

### Signal Rules

| Signal | Condition | Label |
|---|---|---|
| Short-term trend | SMA(10) crossed above SMA(50) today or within last 3 days | BUY |
| Short-term trend | SMA(10) crossed below SMA(50) today or within last 3 days | SELL |
| Golden Cross | SMA(50) crossed above SMA(200) recently | BUY (strong) |
| Death Cross | SMA(50) crossed below SMA(200) recently | SELL (strong) |
| Long-term filter | Close > SMA(200) | Uptrend bias |
| Long-term filter | Close < SMA(200) | Downtrend bias |
| Triple MA alignment | SMA(10) > SMA(50) > SMA(200) | BUY (high conviction) |
| Triple MA alignment | SMA(10) < SMA(50) < SMA(200) | SELL (high conviction) |

### Notes

- MA crossover signals are **lagging** — they confirm a move already in progress
- Only take BUY signals when Close > SMA(200) (long-term uptrend filter)
- Crossovers in low-volatility/sideways markets produce frequent false signals (whipsaws)

-----

## Section 2 — RSI (Momentum / Overbought-Oversold)

### Calculations Required

- RSI(14): 14-period Relative Strength Index on Close price
  - Formula: RSI = 100 - [100 / (1 + RS)], where RS = Avg Gain / Avg Loss over 14 days

### Signal Rules

| Signal | Condition | Label |
|---|---|---|
| Oversold bounce | RSI(14) crosses above 30 (was below 30, now above) | BUY |
| Overbought reversal | RSI(14) crosses below 70 (was above 70, now below) | SELL |
| Extreme oversold | RSI(14) < 20 | WATCH (potential reversal) |
| Extreme overbought | RSI(14) > 80 | WATCH (potential reversal) |
| Trend bias bullish | RSI(14) > 50 | Bullish bias |
| Trend bias bearish | RSI(14) < 50 | Bearish bias |
| Bullish divergence | Price makes new low but RSI makes a higher low | BUY (reversal warning) |
| Bearish divergence | Price makes new high but RSI makes a lower high | SELL (reversal warning) |

### RSI(2) Short-Term Rules (optional, aggressive)

| Condition | Label |
|---|---|
| RSI(2) < 10 | BUY (extreme short-term oversold) |
| RSI(2) > 90 | SELL (extreme short-term overbought) |

### Notes

- RSI can remain above 70 for weeks in a strong uptrend — do NOT sell based on RSI alone in trending markets
- Divergence signals are more reliable than simple threshold crossings
- Combine with MACD for confirmation before acting

-----

## Section 3 — MACD (Trend + Momentum)

### Calculations Required

- EMA(12) and EMA(26) of Close
- MACD Line = EMA(12) - EMA(26)
- Signal Line = EMA(9) of MACD Line
- Histogram = MACD Line - Signal Line

### Signal Rules

| Signal | Condition | Label |
|---|---|---|
| Bullish crossover | MACD Line crosses above Signal Line | BUY |
| Bearish crossover | MACD Line crosses below Signal Line | SELL |
| Zero-line bullish | MACD Line crosses above 0 | BUY (trend confirmed) |
| Zero-line bearish | MACD Line crosses below 0 | SELL (trend confirmed) |
| Momentum building | Histogram bars increasing in the positive direction | Bullish momentum |
| Momentum fading | Histogram bars shrinking from a peak | WATCH (momentum fading) |
| Bullish divergence | Price at new low, MACD at higher low | BUY (reversal warning) |
| Bearish divergence | Price at new high, MACD at lower high | SELL (reversal warning) |

### Notes

- MACD is a **lagging indicator** — best used for trend confirmation, not catching exact tops/bottoms
- A MACD crossover below the zero line is a weaker buy signal than one above the zero line
- Histogram shrinkage is an **early warning** that momentum is reversing — watch for a crossover to follow

-----

## Section 4 — RSI + MACD Combined (Dual-Confirmation Strategy)

This is the primary signal engine. **Only act when both RSI and MACD agree.**

### Signal Rules

| Signal | Condition | Label |
|---|---|---|
| Strong Buy | RSI crosses above 30 AND MACD bullish crossover (within 1-3 days of each other) | STRONG BUY |
| Strong Sell | RSI crosses below 70 AND MACD bearish crossover (within 1-3 days of each other) | STRONG SELL |
| Pullback Buy | MACD bullish crossover seen; RSI subsequently pulls back near 30 then bounces | BUY (trend pullback entry) |
| No signal | RSI and MACD signals conflict (one bullish, one bearish) | NO TRADE — stay flat |

### Decision Logic

```
IF RSI_signal == BUY AND MACD_signal == BUY:
    -> STRONG BUY — high conviction entry

IF RSI_signal == SELL AND MACD_signal == SELL:
    -> STRONG SELL — high conviction exit/short

IF RSI_signal != MACD_signal:
    -> NEUTRAL — do not trade; wait for alignment

IF Close > SMA(200) AND RSI_MACD == STRONG BUY:
    -> HIGHEST CONVICTION BUY
```

-----

## Section 5 — Bollinger Bands (Volatility)

### Calculations Required

- Middle Band = SMA(20) of Close
- Upper Band = SMA(20) + (2 x 20-day standard deviation of Close)
- Lower Band = SMA(20) - (2 x 20-day standard deviation of Close)
- Band Width = (Upper - Lower) / Middle x 100

### Mode Selection

Determine current market mode first:

| Market Mode | Condition | Strategy to Apply |
|---|---|---|
| Trending | Price consistently above/below Middle Band; bands expanding | Breakout rules |
| Range-bound | Price oscillating across Middle Band; bands flat or contracting | Mean Reversion rules |
| Squeeze | Band Width at a 6-month low | Prepare for breakout — direction unknown |

### Mean Reversion Signal Rules (range-bound markets)

| Signal | Condition | Label |
|---|---|---|
| Buy setup | Price closes back inside Lower Band after touching/piercing it | BUY |
| Sell setup | Price closes back inside Upper Band after touching/piercing it | SELL |
| Target | Middle Band (SMA-20) is the mean reversion target | TAKE PROFIT zone |

### Breakout Signal Rules (trending / post-squeeze markets)

| Signal | Condition | Label |
|---|---|---|
| Bullish breakout | After a squeeze, price closes above Upper Band with volume above average | BUY BREAKOUT |
| Bearish breakout | After a squeeze, price closes below Lower Band with volume above average | SELL BREAKOUT |
| False breakout warning | Price closes outside band but volume is below average | WATCH — do not chase |

### W-Bottom Pattern (reversal)

| Signal | Condition | Label |
|---|---|---|
| W-Bottom | First low pierces Lower Band; second low holds above Lower Band; RSI confirms higher low | STRONG BUY |
| M-Top | First high pierces Upper Band; second high holds below Upper Band; RSI confirms lower high | STRONG SELL |

### Notes

- **Mean Reversion and Breakout strategies are opposites** — never apply both simultaneously
- Always confirm Bollinger signals with RSI or volume before acting
- A Bollinger Squeeze alone does not tell you the breakout direction — wait for price to commit

-----

## Section 6 — Volume Confirmation

### Calculations Required

- Volume SMA(20): 20-day average of daily volume
- Volume ratio: Today's Volume / Volume SMA(20)
- OBV (On-Balance Volume): Running total where up-day volume is added, down-day volume is subtracted

### Signal Rules

| Signal | Condition | Label |
|---|---|---|
| Confirmed breakout | Price breaks above resistance AND Volume ratio > 1.5x | BUY (confirmed) |
| Confirmed breakdown | Price breaks below support AND Volume ratio > 1.5x | SELL (confirmed) |
| Weak breakout | Price breaks key level but Volume ratio < 1.0x | WATCH — likely false |
| Climactic reversal | Volume ratio > 3.0x at a multi-week low with reversal candle | BUY (capitulation) |
| Distribution warning | Price at new high but volume declining over last 5 days | SELL WATCH |
| OBV bullish trend | OBV trending up while price is flat or rising | Accumulation signal |
| OBV divergence | OBV declining while price rising | SELL WATCH (institutional exit) |

### Notes

- Volume should be used to **confirm** signals from other indicators, not generate signals alone
- A price breakout without volume is the most common false signal in trading
- Rising OBV during a consolidation = institutional accumulation = bullish

-----

## Section 7 — Synthesis & Final Signal

After completing all sections above, produce a summary table:

### Signal Summary Table

| Section | Indicator | Signal | Strength |
|---|---|---|---|
| 1 | MA Crossover | BUY/SELL/NEUTRAL | Low/Med/High |
| 1 | Long-term filter (SMA 200) | BUY/SELL/NEUTRAL | — |
| 2 | RSI(14) | BUY/SELL/NEUTRAL | Low/Med/High |
| 3 | MACD | BUY/SELL/NEUTRAL | Low/Med/High |
| 4 | RSI+MACD Combined | BUY/SELL/NO TRADE | Low/Med/High |
| 5 | Bollinger Bands | BUY/SELL/NEUTRAL | Low/Med/High |
| 6 | Volume | Confirmed/Weak/WATCH | — |

### Conviction Scoring

Count the directional signals:

- **Bullish signals:** [count]
- **Bearish signals:** [count]
- **Neutral/Conflicting:** [count]

Apply the following overall rating:

| Score | Rating |
|---|---|
| 5-7 bullish signals aligned | STRONG BUY |
| 3-4 bullish, no strong opposing | BUY |
| Mixed / equal signals | NEUTRAL — hold / no new position |
| 3-4 bearish, no strong opposing | SELL |
| 5-7 bearish signals aligned | STRONG SELL |

### Final Output Format

Provide:

1. **Overall Signal:** [STRONG BUY / BUY / NEUTRAL / SELL / STRONG SELL]
1. **Primary Reason:** One sentence on the single most important signal driving the conclusion
1. **Key Risk:** One sentence on the main factor that could invalidate this signal
1. **Suggested Action:** Hold / Buy on pullback / Buy now / Reduce position / Exit / Short
1. **Invalidation Condition:** The specific price level or indicator change that would flip the signal

-----

## General Rules & Guardrails

- **Never act on a single indicator alone** — always require at least 2 confirming signals
- **The SMA(200) is the master filter** — avoid long positions in stocks below their 200-day MA
- **Volume must confirm breakouts** — a breakout without elevated volume is likely a trap
- **Conflicting RSI + MACD = no trade** — patience is a position
- **Divergence signals override simple threshold signals** — RSI divergence is more reliable than RSI crossing 70/30
- **Trending markets favor MA + MACD** — Oscillators like RSI produce false signals in strong trends
- **Range-bound markets favor RSI + Bollinger Bands** — Trend indicators whipsaw in flat markets
- **Past signals do not guarantee future results** — all rule-based systems have regimes where they fail

-----

*Framework based on: SMA/EMA crossovers, RSI(14), MACD(12,26,9), Bollinger Bands(20,2), OBV, and Volume analysis applied to daily OHLCV data.*
