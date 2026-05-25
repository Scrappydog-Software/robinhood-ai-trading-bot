"""Rule-based technical analysis signal scoring engine.

Implements the 7-section framework from docs/technical-analysis-framework.md
using pre-computed indicators stored in stock_history. Pure Python — no LLM
calls, no API calls. Operates on a list of bar dicts (oldest first) with
all indicator columns already populated.

Each section evaluates to a numeric score:
    +2 = strong_buy, +1 = buy, 0 = neutral, -1 = sell, -2 = strong_sell

The overall signal_score is the sum of all 7 section scores (-14 to +14).
The signal_synthesis label maps the total to a conviction level.
"""


def _score_to_label(score):
    """Map a numeric score to a text label."""
    if score >= 2:
        return 'strong_buy'
    elif score >= 1:
        return 'buy'
    elif score <= -2:
        return 'strong_sell'
    elif score <= -1:
        return 'sell'
    return 'hold'


def _synthesis_label(total_score):
    """Map the total score (-14 to +14) to a conviction label."""
    if total_score >= 7:
        return 'strong_buy'
    elif total_score >= 3:
        return 'buy'
    elif total_score <= -7:
        return 'strong_sell'
    elif total_score <= -3:
        return 'sell'
    return 'hold'


def _crossed_above(current, previous, threshold):
    """True if value crossed above threshold between previous and current."""
    if current is None or previous is None:
        return False
    return previous <= threshold and current > threshold


def _crossed_below(current, previous, threshold):
    """True if value crossed below threshold between previous and current."""
    if current is None or previous is None:
        return False
    return previous >= threshold and current < threshold


def score_ma(bars, i):
    """Section 1: Moving Average Crossover.

    Evaluates SMA crossovers, Golden/Death Cross, Triple MA alignment,
    and long-term SMA(200) filter.
    """
    bar = bars[i]
    score = 0

    sma10 = bar.get('sma_10')
    sma50 = bar.get('sma_50')
    sma200 = bar.get('sma_200')
    close = bar.get('close')

    if sma10 is None or sma50 is None:
        return 0

    # Triple MA alignment (high conviction)
    if sma200 is not None:
        if sma10 > sma50 > sma200:
            score = 2  # strong buy (may be capped by overextension below)
        elif sma10 < sma50 < sma200:
            return -2  # strong sell

    # SMA(10)/SMA(50) crossover — check last 3 bars
    for lookback in range(max(0, i - 3), i + 1):
        if lookback > 0:
            prev_sma10 = bars[lookback - 1].get('sma_10')
            prev_sma50 = bars[lookback - 1].get('sma_50')
            curr_sma10 = bars[lookback].get('sma_10')
            curr_sma50 = bars[lookback].get('sma_50')
            if all(v is not None for v in [prev_sma10, prev_sma50, curr_sma10, curr_sma50]):
                if prev_sma10 <= prev_sma50 and curr_sma10 > curr_sma50:
                    score = 1
                    break
                elif prev_sma10 >= prev_sma50 and curr_sma10 < curr_sma50:
                    score = -1
                    break

    # Golden/Death Cross — SMA(50) vs SMA(200) crossover in last 5 bars
    if sma200 is not None:
        for lookback in range(max(0, i - 5), i + 1):
            if lookback > 0:
                prev_50 = bars[lookback - 1].get('sma_50')
                prev_200 = bars[lookback - 1].get('sma_200')
                curr_50 = bars[lookback].get('sma_50')
                curr_200 = bars[lookback].get('sma_200')
                if all(v is not None for v in [prev_50, prev_200, curr_50, curr_200]):
                    if prev_50 <= prev_200 and curr_50 > curr_200:
                        score = 2  # Golden Cross
                        break
                    elif prev_50 >= prev_200 and curr_50 < curr_200:
                        score = -2  # Death Cross
                        break

    # Long-term filter: downgrade buy signals if below SMA(200)
    if sma200 is not None and close is not None:
        if close < sma200 and score > 0:
            score = 0

    # Overextension filter: if price is >30% above SMA(200), cap at hold
    if sma200 is not None and close is not None and sma200 > 0:
        extension_pct = (close - sma200) / sma200
        if extension_pct > 0.30 and score > 0:
            score = 0

    return score


def score_rsi(bars, i):
    """Section 2: RSI (Momentum / Overbought-Oversold).

    Evaluates RSI threshold crossings and divergence signals.
    """
    bar = bars[i]
    rsi = bar.get('rsi_14')
    if rsi is None:
        return 0

    prev_rsi = bars[i - 1].get('rsi_14') if i > 0 else None

    # Oversold bounce (RSI crosses above 30)
    if prev_rsi is not None and prev_rsi < 30 and rsi >= 30:
        return 1

    # Overbought reversal (RSI crosses below 70)
    if prev_rsi is not None and prev_rsi > 70 and rsi <= 70:
        return -1

    # Extreme oversold — potential reversal watch
    if rsi < 20:
        return 1

    # Extreme overbought — potential reversal watch
    if rsi > 80:
        return -1

    # Bullish/bearish divergence (price vs RSI over last 10 bars)
    if i >= 10:
        recent_closes = [bars[j].get('close') for j in range(i - 10, i + 1)]
        recent_rsis = [bars[j].get('rsi_14') for j in range(i - 10, i + 1)]
        if all(v is not None for v in recent_closes) and all(v is not None for v in recent_rsis):
            # Bullish divergence: price new low but RSI higher low
            price_min_idx = recent_closes.index(min(recent_closes))
            if price_min_idx >= 8:  # recent price low
                early_rsi_min = min(recent_rsis[:5])
                late_rsi = recent_rsis[price_min_idx]
                if late_rsi > early_rsi_min + 3:
                    return 2  # bullish divergence

            # Bearish divergence: price new high but RSI lower high
            price_max_idx = recent_closes.index(max(recent_closes))
            if price_max_idx >= 8:
                early_rsi_max = max(recent_rsis[:5])
                late_rsi = recent_rsis[price_max_idx]
                if late_rsi < early_rsi_max - 3:
                    return -2  # bearish divergence

    # Trend bias
    if rsi > 55:
        return 0  # mild bullish, not worth a signal
    elif rsi < 45:
        return 0  # mild bearish, not worth a signal

    return 0


def score_macd(bars, i):
    """Section 3: MACD (Trend + Momentum).

    Evaluates MACD/Signal crossovers, zero-line crosses, and histogram momentum.
    """
    bar = bars[i]
    macd = bar.get('macd_line')
    signal = bar.get('macd_signal')
    hist = bar.get('macd_histogram')

    if macd is None or signal is None:
        return 0

    prev_macd = bars[i - 1].get('macd_line') if i > 0 else None
    prev_signal = bars[i - 1].get('macd_signal') if i > 0 else None
    prev_hist = bars[i - 1].get('macd_histogram') if i > 0 else None

    score = 0

    # Bullish crossover (MACD crosses above Signal)
    if prev_macd is not None and prev_signal is not None:
        if prev_macd <= prev_signal and macd > signal:
            score = 1
            # Stronger if above zero line
            if macd > 0:
                score = 2
        elif prev_macd >= prev_signal and macd < signal:
            score = -1
            if macd < 0:
                score = -2

    # Zero-line cross (if no crossover signal)
    if score == 0 and prev_macd is not None:
        if prev_macd <= 0 and macd > 0:
            score = 1
        elif prev_macd >= 0 and macd < 0:
            score = -1

    # Divergence check (price vs MACD over last 10 bars)
    if score == 0 and i >= 10:
        recent_closes = [bars[j].get('close') for j in range(i - 10, i + 1)]
        recent_macd = [bars[j].get('macd_line') for j in range(i - 10, i + 1)]
        if all(v is not None for v in recent_closes) and all(v is not None for v in recent_macd):
            price_min_idx = recent_closes.index(min(recent_closes))
            if price_min_idx >= 8:
                early_macd_min = min(recent_macd[:5])
                if recent_macd[price_min_idx] > early_macd_min:
                    score = 2  # bullish divergence

            price_max_idx = recent_closes.index(max(recent_closes))
            if price_max_idx >= 8:
                early_macd_max = max(recent_macd[:5])
                if recent_macd[price_max_idx] < early_macd_max:
                    score = -2  # bearish divergence

    return score


def score_rsi_macd(bars, i):
    """Section 4: RSI + MACD Combined (Dual-Confirmation).

    Only fires when both RSI and MACD agree. Conflicting signals = neutral.
    """
    rsi_score = score_rsi(bars, i)
    macd_score = score_macd(bars, i)

    # Both bullish
    if rsi_score > 0 and macd_score > 0:
        return 2  # strong buy — dual confirmation

    # Both bearish
    if rsi_score < 0 and macd_score < 0:
        return -2  # strong sell — dual confirmation

    # Conflicting — no trade
    if (rsi_score > 0 and macd_score < 0) or (rsi_score < 0 and macd_score > 0):
        return 0

    # One neutral, one directional — weak signal
    if rsi_score > 0 or macd_score > 0:
        return 1
    if rsi_score < 0 or macd_score < 0:
        return -1

    return 0


def score_bb(bars, i):
    """Section 5: Bollinger Bands (Volatility).

    Evaluates mean reversion vs breakout mode, W-bottom/M-top patterns.
    """
    bar = bars[i]
    close = bar.get('close')
    bb_upper = bar.get('bb_upper')
    bb_lower = bar.get('bb_lower')
    bb_width = bar.get('bb_width')
    sma20 = bar.get('sma_20')
    vol_ratio = bar.get('vol_ratio')

    if close is None or bb_upper is None or bb_lower is None:
        return 0

    prev_close = bars[i - 1].get('close') if i > 0 else None
    prev_bb_lower = bars[i - 1].get('bb_lower') if i > 0 else None
    prev_bb_upper = bars[i - 1].get('bb_upper') if i > 0 else None

    # Detect squeeze (very low bb_width — use 6-month lookback)
    is_squeeze = False
    if bb_width is not None and i >= 120:
        past_widths = [bars[j].get('bb_width') for j in range(i - 120, i) if bars[j].get('bb_width') is not None]
        if past_widths and bb_width <= min(past_widths) * 1.1:
            is_squeeze = True

    # Breakout mode (post-squeeze or trending)
    if is_squeeze or (bb_width is not None and bb_width > 8):
        # Bullish breakout: close above upper band with volume
        if close > bb_upper and vol_ratio is not None and vol_ratio > 1.5:
            return 2
        # Bearish breakout: close below lower band with volume
        if close < bb_lower and vol_ratio is not None and vol_ratio > 1.5:
            return -2
        # False breakout: outside band but low volume
        if close > bb_upper and (vol_ratio is None or vol_ratio < 1.0):
            return 0  # watch, don't chase

    # Mean reversion mode (range-bound)
    if prev_close is not None and prev_bb_lower is not None:
        # Buy: price was at/below lower band, now back inside
        if prev_close <= prev_bb_lower and close > bb_lower:
            return 1
    if prev_close is not None and prev_bb_upper is not None:
        # Sell: price was at/above upper band, now back inside
        if prev_close >= prev_bb_upper and close < bb_upper:
            return -1

    # W-Bottom pattern (simplified): two lows near lower band, second higher
    if i >= 20:
        recent_lows = [(j, bars[j].get('low')) for j in range(i - 20, i + 1)
                       if bars[j].get('low') is not None and bars[j].get('bb_lower') is not None]
        band_touches = [(j, low) for j, low in recent_lows if low <= bars[j].get('bb_lower', 0) * 1.01]
        if len(band_touches) >= 2:
            first_low = band_touches[0][1]
            last_low = band_touches[-1][1]
            if last_low > first_low and (i - band_touches[-1][0]) <= 3:
                return 2  # W-bottom

    return 0


def score_volume(bars, i):
    """Section 6: Volume Confirmation.

    Evaluates volume relative to average, OBV trends, and distribution.
    """
    bar = bars[i]
    vol_ratio = bar.get('vol_ratio')
    obv = bar.get('obv')
    close = bar.get('close')

    if vol_ratio is None:
        return 0

    prev_close = bars[i - 1].get('close') if i > 0 else None

    # Climactic reversal: huge volume at a recent low with recovery
    if vol_ratio > 3.0 and i >= 5:
        recent_closes = [bars[j].get('close') for j in range(i - 5, i) if bars[j].get('close') is not None]
        if recent_closes and close is not None:
            if close > min(recent_closes) and min(recent_closes) == min(
                [bars[j].get('close') for j in range(max(0, i - 20), i) if bars[j].get('close') is not None] or [close]
            ):
                return 2  # capitulation reversal

    # Breakout confirmation: price up + high volume
    if prev_close is not None and close is not None and close > prev_close:
        if vol_ratio > 1.5:
            return 1  # confirmed move up
    elif prev_close is not None and close is not None and close < prev_close:
        if vol_ratio > 1.5:
            return -1  # confirmed move down

    # Weak moves: price change without volume support
    if prev_close is not None and close is not None:
        if abs(close - prev_close) / prev_close > 0.02 and vol_ratio < 0.8:
            return 0  # suspect move

    # OBV divergence (OBV declining while price rising over 5 days)
    if obv is not None and i >= 5:
        recent_obv = [bars[j].get('obv') for j in range(i - 5, i + 1) if bars[j].get('obv') is not None]
        recent_prices = [bars[j].get('close') for j in range(i - 5, i + 1) if bars[j].get('close') is not None]
        if len(recent_obv) >= 5 and len(recent_prices) >= 5:
            price_rising = recent_prices[-1] > recent_prices[0]
            obv_falling = recent_obv[-1] < recent_obv[0]
            price_falling = recent_prices[-1] < recent_prices[0]
            obv_rising = recent_obv[-1] > recent_obv[0]

            if price_rising and obv_falling:
                return -1  # distribution — institutional exit
            if price_falling and obv_rising:
                return 1  # accumulation

    return 0


def compute_signals_for_bars(bars):
    """Evaluate all 7 signal sections for each bar in a list.

    Args:
        bars: list of dicts (oldest first) with all indicator columns populated.

    Returns:
        list of dicts with signal columns added to each bar:
        signal_ma, signal_rsi, signal_macd, signal_rsi_macd,
        signal_bb, signal_volume, signal_synthesis, signal_score
    """
    for i in range(len(bars)):
        # Need at least some history context for meaningful signals
        if i < 14:
            bars[i]['signal_ma'] = 'hold'
            bars[i]['signal_rsi'] = 'hold'
            bars[i]['signal_macd'] = 'hold'
            bars[i]['signal_rsi_macd'] = 'hold'
            bars[i]['signal_bb'] = 'hold'
            bars[i]['signal_volume'] = 'hold'
            bars[i]['signal_synthesis'] = 'hold'
            bars[i]['signal_score'] = 0
            continue

        ma = score_ma(bars, i)
        rsi = score_rsi(bars, i)
        macd = score_macd(bars, i)
        rsi_macd = score_rsi_macd(bars, i)
        bb = score_bb(bars, i)
        volume = score_volume(bars, i)

        total = ma + rsi + macd + rsi_macd + bb + volume

        # --- Global filters (applied after individual sections) ---
        bar = bars[i]
        rsi_val = bar.get('rsi_14')
        vol_r = bar.get('vol_ratio')
        close = bar.get('close')
        bb_up = bar.get('bb_upper')

        # 1. Volume confirmation: strong_buy requires vol_ratio >= 0.8
        #    Low volume rallies are unsustainable
        if total >= 7 and vol_r is not None and vol_r < 0.8:
            total = min(total, 4)  # cap at buy level

        # 2. RSI overbought suppression: no strong_buy when RSI > 68
        #    Momentum exhaustion risk
        if total >= 7 and rsi_val is not None and rsi_val > 68:
            total = min(total, 4)  # cap at buy level

        # 3. Above upper Bollinger Band: reduce score by 2
        #    Price is overextended relative to recent range
        if close is not None and bb_up is not None and close > bb_up:
            if total > 0:
                total = max(total - 2, 0)

        bars[i]['signal_ma'] = _score_to_label(ma)
        bars[i]['signal_rsi'] = _score_to_label(rsi)
        bars[i]['signal_macd'] = _score_to_label(macd)
        bars[i]['signal_rsi_macd'] = _score_to_label(rsi_macd)
        bars[i]['signal_bb'] = _score_to_label(bb)
        bars[i]['signal_volume'] = _score_to_label(volume)
        bars[i]['signal_synthesis'] = _synthesis_label(total)
        bars[i]['signal_score'] = total

    return bars
