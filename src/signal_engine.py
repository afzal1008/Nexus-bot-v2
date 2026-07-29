"""
Signal Engine - Multi-indicator AI trading signal generator
Pure Python implementation - no pandas/numpy required

v2: adds ADX (trend-strength filter) and ATR (volatility, used for dynamic
stop-loss/take-profit sizing). MACD/EMA trend-following points are discounted
when ADX shows a weak/ranging market, since those indicators are unreliable
outside real trends.
"""
from dataclasses import dataclass
from typing import Optional
import math


@dataclass
class SignalResult:
    signal: str
    confidence: float
    rsi: float
    macd: float
    macd_signal: float
    ema_fast: float
    ema_slow: float
    bb_upper: float
    bb_lower: float
    bb_mid: float
    bb_position: str
    volume_signal: str
    pattern: str
    adx: float
    atr: float
    atr_pct: float
    reasoning: str


def calculate_rsi(closes: list, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0 for d in deltas[-period:]]
    losses = [-d if d < 0 else 0 for d in deltas[-period:]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


def ema(values: list, span: int) -> list:
    k = 2 / (span + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def calculate_macd(closes: list):
    if len(closes) < 26:
        return 0.0, 0.0, 0.0
    ema12 = ema(closes, 12)
    ema26 = ema(closes, 26)
    macd_line = [ema12[i] - ema26[i] for i in range(len(closes))]
    signal_line = ema(macd_line, 9)
    hist = macd_line[-1] - signal_line[-1]
    return round(macd_line[-1], 6), round(signal_line[-1], 6), round(hist, 6)


def calculate_ema_cross(closes: list, fast: int = 9, slow: int = 21):
    if len(closes) < slow:
        return closes[-1], closes[-1]
    ema_fast = ema(closes, fast)[-1]
    ema_slow = ema(closes, slow)[-1]
    return round(ema_fast, 4), round(ema_slow, 4)


def calculate_bollinger(closes: list, period: int = 20):
    if len(closes) < period:
        price = closes[-1]
        return price * 1.02, price * 0.98, price, "middle"
    recent = closes[-period:]
    mid = sum(recent) / period
    variance = sum((x - mid) ** 2 for x in recent) / period
    std = math.sqrt(variance)
    upper = mid + 2 * std
    lower = mid - 2 * std
    price = closes[-1]
    if price > upper:
        pos = "above_upper"
    elif price < lower:
        pos = "below_lower"
    else:
        pos = "middle"
    return round(upper, 4), round(lower, 4), round(mid, 4), pos


def calculate_volume_signal(volumes: list) -> str:
    if len(volumes) < 20:
        return "normal"
    avg = sum(volumes[-20:]) / 20
    curr = volumes[-1]
    ratio = curr / avg if avg > 0 else 1
    if ratio > 1.5:
        return "high"
    elif ratio < 0.5:
        return "low"
    return "normal"


def detect_pattern(candles: list) -> str:
    if len(candles) < 2:
        return "none"
    c1 = candles[-2]
    c2 = candles[-1]
    o1, h1, l1, cl1 = c1["open"], c1["high"], c1["low"], c1["close"]
    o2, h2, l2, cl2 = c2["open"], c2["high"], c2["low"], c2["close"]
    body1 = abs(cl1 - o1)
    body2 = abs(cl2 - o2)
    if cl1 < o1 and cl2 > o2 and cl2 > o1 and o2 < cl1 and body2 > body1:
        return "bullish_engulfing"
    if cl1 > o1 and cl2 < o2 and cl2 < o1 and o2 > cl1 and body2 > body1:
        return "bearish_engulfing"
    full_range = h2 - l2
    if full_range > 0 and body2 / full_range < 0.1:
        return "doji"
    lower_wick = min(o2, cl2) - l2
    upper_wick = h2 - max(o2, cl2)
    if lower_wick > 2 * body2 and upper_wick < body2:
        return "hammer"
    if upper_wick > 2 * body2 and lower_wick < body2:
        return "shooting_star"
    return "none"


def calculate_atr(candles: list, period: int = 14) -> float:
    """Average True Range — measures how much a coin actually moves, in price units.
    Used to size stop-loss/take-profit relative to each coin's own volatility
    instead of a single fixed percentage for every coin."""
    if len(candles) < period + 1:
        # Not enough history — fall back to a rough 2% of price as a conservative estimate
        return candles[-1]["close"] * 0.02

    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    recent_trs = trs[-period:]
    atr = sum(recent_trs) / period
    return round(atr, 8)


def _wilder_smooth(values: list, period: int) -> list:
    """Wilder's smoothing method — used for ADX's DM/TR averaging."""
    if len(values) < period:
        avg = sum(values) / len(values) if values else 0
        return [avg]
    smoothed = sum(values[:period])
    result = [smoothed]
    for v in values[period:]:
        smoothed = smoothed - (smoothed / period) + v
        result.append(smoothed)
    return result


def calculate_adx(candles: list, period: int = 14) -> float:
    """Average Directional Index — measures TREND STRENGTH (not direction).
    ADX > 25: real trend underway, MACD/EMA-style signals are reliable.
    ADX < 20: choppy/ranging market, trend-following signals are unreliable
    and should be discounted in favor of mean-reversion signals (RSI, Bollinger).
    This is a simplified approximation of the standard ADX calculation —
    good enough as a trend-strength filter, not intended for precise charting."""
    if len(candles) < period * 2:
        return 15.0  # not enough data — treat as weak/neutral trend (conservative, discounts trend signals)

    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        up_move = candles[i]["high"] - candles[i-1]["high"]
        down_move = candles[i-1]["low"] - candles[i]["low"]
        plus_dm.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dm.append(down_move if (down_move > up_move and down_move > 0) else 0)
        high = candles[i]["high"]
        low = candles[i]["low"]
        prev_close = candles[i-1]["close"]
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))

    smoothed_tr = _wilder_smooth(trs, period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)

    n = min(len(smoothed_tr), len(smoothed_plus_dm), len(smoothed_minus_dm))
    dx_values = []
    for i in range(n):
        str_ = smoothed_tr[i]
        if str_ == 0:
            dx_values.append(0)
            continue
        plus_di = 100 * (smoothed_plus_dm[i] / str_)
        minus_di = 100 * (smoothed_minus_dm[i] / str_)
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        dx_values.append(dx)

    if not dx_values:
        return 15.0
    window = dx_values[-period:] if len(dx_values) >= period else dx_values
    return round(sum(window) / len(window), 2)


def generate_signal(candles: list) -> SignalResult:
    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]

    rsi                              = calculate_rsi(closes)
    macd, macd_sig, macd_hist        = calculate_macd(closes)
    ema_fast, ema_slow               = calculate_ema_cross(closes)
    bb_upper, bb_lower, bb_mid, bb_pos = calculate_bollinger(closes)
    vol_signal                       = calculate_volume_signal(volumes)
    pattern                          = detect_pattern(candles)
    adx                              = calculate_adx(candles)
    atr                              = calculate_atr(candles)
    atr_pct                          = round((atr / closes[-1]) * 100, 3) if closes[-1] > 0 else 2.0

    # Trend-strength filter: discount trend-following signals (MACD, EMA) when
    # ADX shows a weak/ranging market — they're unreliable outside real trends.
    trend_multiplier = 1.0 if adx >= 25 else (0.5 if adx < 20 else 0.75)

    buy_score  = 0
    sell_score = 0
    reasons    = []

    # Mean-reversion signals (RSI, Bollinger) — reliable regardless of trend regime
    if rsi < 30:
        buy_score += 25; reasons.append(f"RSI oversold ({rsi})")
    elif rsi < 40:
        buy_score += 10; reasons.append(f"RSI approaching oversold ({rsi})")
    elif rsi > 70:
        sell_score += 25; reasons.append(f"RSI overbought ({rsi})")
    elif rsi > 60:
        sell_score += 10; reasons.append(f"RSI approaching overbought ({rsi})")

    if bb_pos == "below_lower":
        buy_score += 20; reasons.append("Price below Bollinger lower band")
    elif bb_pos == "above_upper":
        sell_score += 20; reasons.append("Price above Bollinger upper band")

    # Trend-following signals (MACD, EMA) — discounted in weak/ranging markets
    if macd > macd_sig and macd_hist > 0:
        pts = round(20 * trend_multiplier)
        buy_score += pts; reasons.append(f"MACD bullish crossover (x{trend_multiplier}, ADX={adx})")
    elif macd < macd_sig and macd_hist < 0:
        pts = round(20 * trend_multiplier)
        sell_score += pts; reasons.append(f"MACD bearish crossover (x{trend_multiplier}, ADX={adx})")

    if ema_fast > ema_slow:
        pts = round(15 * trend_multiplier)
        buy_score += pts; reasons.append(f"EMA fast above slow, bullish (x{trend_multiplier})")
    elif ema_fast < ema_slow:
        pts = round(15 * trend_multiplier)
        sell_score += pts; reasons.append(f"EMA fast below slow, bearish (x{trend_multiplier})")

    if vol_signal == "high":
        if buy_score > sell_score:
            buy_score += 10; reasons.append("High volume confirms buy")
        elif sell_score > buy_score:
            sell_score += 10; reasons.append("High volume confirms sell")

    pattern_scores = {
        "bullish_engulfing": ("buy", 10),
        "hammer":            ("buy", 8),
        "bearish_engulfing": ("sell", 10),
        "shooting_star":     ("sell", 8),
    }
    if pattern in pattern_scores:
        direction, score = pattern_scores[pattern]
        if direction == "buy":
            buy_score += score; reasons.append(f"Pattern: {pattern}")
        elif direction == "sell":
            sell_score += score; reasons.append(f"Pattern: {pattern}")

    if buy_score > sell_score and buy_score >= 25:
        signal = "buy"
        confidence = round(min((buy_score / 100) * 100, 99), 1)
    elif sell_score > buy_score and sell_score >= 25:
        signal = "sell"
        confidence = round(min((sell_score / 100) * 100, 99), 1)
    else:
        signal = "hold"
        confidence = round(max(buy_score, sell_score), 1)

    return SignalResult(
        signal=signal,
        confidence=confidence,
        rsi=rsi,
        macd=macd,
        macd_signal=macd_sig,
        ema_fast=ema_fast,
        ema_slow=ema_slow,
        bb_upper=bb_upper,
        bb_lower=bb_lower,
        bb_mid=bb_mid,
        bb_position=bb_pos,
        volume_signal=vol_signal,
        pattern=pattern,
        adx=adx,
        atr=atr,
        atr_pct=atr_pct,
        reasoning=" | ".join(reasons) if reasons else "No strong signal"
    )


class SignalEngine:
    def generate(self, candles: list) -> SignalResult:
        return generate_signal(candles)
