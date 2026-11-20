"""Technical indicators calculated from completed market candles."""

import math
from typing import Any


MIN_CANDLES = 34


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value


def _ema(values: list[float], period: int) -> list[float | None]:
    result = [None] * len(values)
    if len(values) < period:
        return result

    current = math.fsum(values[:period]) / period
    result[period - 1] = current
    alpha = 2.0 / (period + 1)

    for i in range(period, len(values)):
        current = alpha * values[i] + (1 - alpha) * current
        result[i] = current

    return result


def _rsi(closes: list[float], period: int = 14) -> float:
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(change, 0.0) for change in changes]
    losses = [max(-change, 0.0) for change in changes]

    avg_gain = math.fsum(gains[:period]) / period
    avg_loss = math.fsum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_gain == 0 and avg_loss == 0:
        return 50.0
    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    true_ranges = [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(closes))
    ]

    current = math.fsum(true_ranges[:period]) / period
    for value in true_ranges[period:]:
        current = (current * (period - 1) + value) / period

    return current



def _adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int = 14,
) -> float:
    if len(closes) < (2 * period):
        raise ValueError("insufficient candles for ADX")

    true_ranges = []
    plus_dm = []
    minus_dm = []

    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]

        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        true_ranges.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))

    smooth_tr = math.fsum(true_ranges[:period])
    smooth_plus = math.fsum(plus_dm[:period])
    smooth_minus = math.fsum(minus_dm[:period])
    dx_values = []

    for i in range(period - 1, len(true_ranges)):
        if i >= period:
            smooth_tr = smooth_tr - smooth_tr / period + true_ranges[i]
            smooth_plus = smooth_plus - smooth_plus / period + plus_dm[i]
            smooth_minus = smooth_minus - smooth_minus / period + minus_dm[i]

        if smooth_tr == 0:
            dx_values.append(0.0)
            continue

        plus_di = 100.0 * smooth_plus / smooth_tr
        minus_di = 100.0 * smooth_minus / smooth_tr
        denominator = plus_di + minus_di

        dx_values.append(
            0.0 if denominator == 0
            else 100.0 * abs(plus_di - minus_di) / denominator
        )

    if len(dx_values) < period:
        raise ValueError("insufficient DX values for ADX")

    adx_value = math.fsum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx_value = (adx_value * (period - 1) + dx) / period

    return adx_value

def calculate_indicators(candles: list[dict]) -> dict:
    """Calculate indicators from oldest-to-newest completed candles.

    Candle completion must be verified by the upstream data layer.
    Each candle requires open, high, low and close.
    Volume is optional.
    """
    if not isinstance(candles, list) or len(candles) < MIN_CANDLES:
        raise ValueError(f"at least {MIN_CANDLES} completed candles are required")

    opens, highs, lows, closes, volumes = [], [], [], [], []

    for index, candle in enumerate(candles):
        if not isinstance(candle, dict):
            raise ValueError(f"candle {index} must be an object")

        o = _number(candle.get("open"), "open")
        h = _number(candle.get("high"), "high")
        low = _number(candle.get("low"), "low")
        c = _number(candle.get("close"), "close")

        if h < max(o, c, low) or low > min(o, c):
            raise ValueError(f"invalid OHLC values in candle {index}")

        opens.append(o)
        highs.append(h)
        lows.append(low)
        closes.append(c)

        volume = candle.get("volume")
        if volume is None:
            volumes.append(None)
        else:
            volume = _number(volume, "volume")
            if volume < 0:
                raise ValueError("volume must be non-negative")
            volumes.append(volume)

    ema9 = _ema(closes, 9)
    ema20 = _ema(closes, 20)
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)

    macd_values = [
        ema12[i] - ema26[i]
        for i in range(len(closes))
        if ema12[i] is not None and ema26[i] is not None
    ]
    signal_values = _ema(macd_values, 9)
    macd_line = macd_values[-1]
    macd_signal = signal_values[-1]
    macd_histogram = macd_line - macd_signal

    last_open = opens[-1]
    last_close = closes[-1]
    last_high = highs[-1]
    last_low = lows[-1]

    volume_sma20 = None
    recent_volumes = volumes[-20:]
    if all(value is not None for value in recent_volumes):
        volume_sma20 = math.fsum(recent_volumes) / 20

    return {
        "ema_9": ema9[-1],
        "ema_20": ema20[-1],
        "rsi_14": _rsi(closes, 14),
        "macd": {
            "line": macd_line,
            "signal": macd_signal,
            "histogram": macd_histogram,
        },
        "atr_14": _atr(highs, lows, closes, 14),
        "adx_14": _adx(highs, lows, closes, 14),
        "volume_sma_20": volume_sma20,
        "candle": {
            "direction": (
                "bullish" if last_close > last_open
                else "bearish" if last_close < last_open
                else "neutral"
            ),
            "body": abs(last_close - last_open),
            "range": last_high - last_low,
        },
        "candles_used": len(candles),
    }
