"""
Signal Engine - Multi-indicator AI trading signal generator
Indicators: RSI, MACD, EMA crossover, Bollinger Bands, Volume momentum, Candlestick patterns
"""

import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import Optional


@dataclass
class SignalResult:
    signal: str          # buy / sell / hold
    confidence: float    # 0-100
    rsi: float
    macd: float
    macd_signal: float
    ema_fast: float
    ema_slow: float
    bb_upper: float
    bb_lower: float
    bb_mid: float
    bb_position: str     # above_upper / below_lower / middle
    volume_signal: str   # high / low / normal
    pattern: str         # bullish_engulfing / bearish_engulfing / doji / none
    reasoning: str


def calculate_rsi(closes: pd.Series, period: int = 14) -> float:
    delta = closes.diff()
    gain = delta.where(delta > 0, 0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return round(float(rsi.iloc[-1]), 2)


def calculate_macd(closes: pd.Series):
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    macd_line = ema12 - ema26
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    histogram = macd_line - signal_line
    return (
        round(float(macd_line.iloc[-1]), 6),
        round(float(signal_line.iloc[-1]), 6),
        round(float(histogram.iloc[-1]), 6)
    )


def calculate_ema(closes: pd.Series, fast: int = 9, slow: int = 21):
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    return round(float(ema_fast.iloc[-1]), 4), round(float(ema_slow.iloc[-1]), 4)


def calculate_bollinger(closes: pd.Series, period: int = 20, std: int = 2):
    mid = closes.rolling(period).mean()
    std_dev = closes.rolling(period).std()
    upper = mid + (std_dev * std)
    lower = mid - (std_dev * std)
    price = closes.iloc[-1]
    if price > upper.iloc[-1]:
        position = "above_upper"
    elif price < lower.iloc[-1]:
        position = "below_lower"
    else:
        position = "middle"
    return (
        round(float(upper.iloc[-1]), 4),
        round(float(lower.iloc[-1]), 4),
        round(float(mid.iloc[-1]), 4),
        position
    )


def calculate_volume_signal(volumes: pd.Series) -> str:
    avg_vol = volumes.rolling(20).mean().iloc[-1]
    curr_vol = volumes.iloc[-1]
    ratio = curr_vol / avg_vol if avg_vol > 0 else 1
    if ratio > 1.5:
        return "high"
    elif ratio < 0.5:
        return "low"
    return "normal"


def detect_candlestick_pattern(opens, highs, lows, closes) -> str:
    o1, o2 = float(opens.iloc[-2]), float(opens.iloc[-1])
    h1, h2 = float(highs.iloc[-2]), float(highs.iloc[-1])
    l1, l2 = float(lows.iloc[-2]), float(lows.iloc[-1])
    c1, c2 = float(closes.iloc[-2]), float(closes.iloc[-1])

    body1 = abs(c1 - o1)
    body2 = abs(c2 - o2)

    # Bullish engulfing
    if c1 < o1 and c2 > o2 and c2 > o1 and o2 < c1 and body2 > body1:
        return "bullish_engulfing"

    # Bearish engulfing
    if c1 > o1 and c2 < o2 and c2 < o1 and o2 > c1 and body2 > body1:
        return "bearish_engulfing"

    # Doji
    full_range = h2 - l2
    if full_range > 0 and body2 / full_range < 0.1:
        return "doji"

    # Hammer (bullish)
    lower_wick = min(o2, c2) - l2
    upper_wick = h2 - max(o2, c2)
    if lower_wick > 2 * body2 and upper_wick < body2:
        return "hammer"

    # Shooting star (bearish)
    if upper_wick > 2 * body2 and lower_wick < body2:
        return "shooting_star"

    return "none"


def generate_signal(candles: list) -> SignalResult:
    """
    Main signal function. candles = list of dicts with:
    {timestamp, open, high, low, close, volume}
    """
    df = pd.DataFrame(candles)
    df.columns = [c.lower() for c in df.columns]

    closes  = df["close"].astype(float)
    opens   = df["open"].astype(float)
    highs   = df["high"].astype(float)
    lows    = df["low"].astype(float)
    volumes = df["volume"].astype(float)

    # Calculate indicators
    rsi                              = calculate_rsi(closes)
    macd, macd_sig, macd_hist        = calculate_macd(closes)
    ema_fast, ema_slow               = calculate_ema(closes)
    bb_upper, bb_lower, bb_mid, bb_pos = calculate_bollinger(closes)
    vol_signal                       = calculate_volume_signal(volumes)
    pattern                          = detect_candlestick_pattern(opens, highs, lows, closes)

    # ── Scoring System ────────────────────────────────────────────────────────
    buy_score  = 0
    sell_score = 0
    reasons    = []

    # RSI
    if rsi < 30:
        buy_score += 25
        reasons.append(f"RSI oversold ({rsi})")
    elif rsi < 40:
        buy_score += 10
        reasons.append(f"RSI approaching oversold ({rsi})")
    elif rsi > 70:
        sell_score += 25
        reasons.append(f"RSI overbought ({rsi})")
    elif rsi > 60:
        sell_score += 10
        reasons.append(f"RSI approaching overbought ({rsi})")

    # MACD
    if macd > macd_sig and macd_hist > 0:
        buy_score += 20
        reasons.append("MACD bullish crossover")
    elif macd < macd_sig and macd_hist < 0:
        sell_score += 20
        reasons.append("MACD bearish crossover")

    # EMA crossover
    if ema_fast > ema_slow:
        buy_score += 15
        reasons.append("EMA fast above slow (bullish trend)")
    elif ema_fast < ema_slow:
        sell_score += 15
        reasons.append("EMA fast below slow (bearish trend)")

    # Bollinger Bands
    if bb_pos == "below_lower":
        buy_score += 20
        reasons.append("Price below Bollinger lower band (oversold)")
    elif bb_pos == "above_upper":
        sell_score += 20
        reasons.append("Price above Bollinger upper band (overbought)")

    # Volume confirmation
    if vol_signal == "high":
        if buy_score > sell_score:
            buy_score += 10
            reasons.append("High volume confirms buy signal")
        elif sell_score > buy_score:
            sell_score += 10
            reasons.append("High volume confirms sell signal")

    # Candlestick patterns
    pattern_scores = {
        "bullish_engulfing": ("buy", 10),
        "hammer":            ("buy", 8),
        "bearish_engulfing": ("sell", 10),
        "shooting_star":     ("sell", 8),
        "doji":              ("hold", 0),
    }
    if pattern in pattern_scores:
        direction, score = pattern_scores[pattern]
        if direction == "buy":
            buy_score += score
            reasons.append(f"Candlestick pattern: {pattern}")
        elif direction == "sell":
            sell_score += score
            reasons.append(f"Candlestick pattern: {pattern}")

    # ── Final Signal ──────────────────────────────────────────────────────────
    total = buy_score + sell_score
    if total == 0:
        signal = "hold"
        confidence = 0.0
    elif buy_score > sell_score and buy_score >= 25:
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
        reasoning=" | ".join(reasons) if reasons else "No strong signal"
    )
