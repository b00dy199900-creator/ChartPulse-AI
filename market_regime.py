"""Rule-based market regime classification."""
import math


MIN_REGIME_CANDLES = 35
ATR_BASELINE_PERIODS = 20

HIGH_VOLATILITY_RATIO = 1.5
TREND_ADX_MIN = 25.0
UPTREND_RSI_MIN = 52.0
DOWNTREND_RSI_MAX = 48.0
SIDEWAYS_ADX_MAX = 20.0
SIDEWAYS_RSI_MIN = 40.0
SIDEWAYS_RSI_MAX = 60.0
SIDEWAYS_EMA_ATR_MAX = 0.25


def _metric(indicators, name):
    value = indicators.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid indicator: {name}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite indicator: {name}")
    return value


def _atr_baseline(candles):
    """Average the 20 ATR readings preceding the latest candle."""
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    closes = [float(c["close"]) for c in candles]

    period = 14
    true_ranges = [
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(candles))
    ]

    if len(true_ranges) < period + ATR_BASELINE_PERIODS:
        raise ValueError("Insufficient candles for ATR baseline")

    current = math.fsum(true_ranges[:period]) / period
    atr_values = [current]

    for value in true_ranges[period:]:
        current = (current * (period - 1) + value) / period
        atr_values.append(current)

    previous_values = atr_values[-(ATR_BASELINE_PERIODS + 1):-1]
    return math.fsum(previous_values) / ATR_BASELINE_PERIODS


def classify_market_regime(candles, indicators):
    """Classify market conditions without generating trade signals."""
    if not isinstance(candles, list) or len(candles) < MIN_REGIME_CANDLES:
        raise ValueError(
            f"At least {MIN_REGIME_CANDLES} candles are required"
        )
    if not isinstance(indicators, dict):
        raise ValueError("indicators must be an object")

    ema9 = _metric(indicators, "ema_9")
    ema20 = _metric(indicators, "ema_20")
    rsi = _metric(indicators, "rsi_14")
    adx = _metric(indicators, "adx_14")
    atr = _metric(indicators, "atr_14")

    macd = indicators.get("macd")
    if not isinstance(macd, dict):
        raise ValueError("MACD data is missing")
    histogram = _metric(macd, "histogram")

    if not 0 <= rsi <= 100 or not 0 <= adx <= 100 or atr < 0:
        raise ValueError("Indicator values are outside valid ranges")

    baseline = _atr_baseline(candles)
    volatility_ratio = atr / baseline if baseline > 0 else None

    metrics = {
        "adx_14": adx,
        "rsi_14": rsi,
        "ema_gap": abs(ema9 - ema20),
        "atr_14": atr,
        "atr_baseline_20": baseline,
        "volatility_ratio": volatility_ratio,
        "macd_histogram": histogram,
    }

    high_volatility = (
        (baseline == 0 and atr > 0)
        or (
            volatility_ratio is not None
            and volatility_ratio >= HIGH_VOLATILITY_RATIO
        )
    )

    if high_volatility:
        return {
            "state": "HIGH_VOLATILITY",
            "label_ar": "تقلب مرتفع",
            "reason": "ATR الحالي مرتفع مقارنة بمتوسط ATR السابق",
            "metrics": metrics,
        }

    if (
        adx >= TREND_ADX_MIN
        and ema9 > ema20
        and histogram > 0
        and rsi >= UPTREND_RSI_MIN
    ):
        state = "UPTREND"
        label = "اتجاه صاعد"
        reason = "اتفاق ADX وEMA وMACD وRSI على اتجاه صاعد"

    elif (
        adx >= TREND_ADX_MIN
        and ema9 < ema20
        and histogram < 0
        and rsi <= DOWNTREND_RSI_MAX
    ):
        state = "DOWNTREND"
        label = "اتجاه هابط"
        reason = "اتفاق ADX وEMA وMACD وRSI على اتجاه هابط"

    elif (
        adx < SIDEWAYS_ADX_MAX
        and SIDEWAYS_RSI_MIN <= rsi <= SIDEWAYS_RSI_MAX
        and abs(ema9 - ema20) <= SIDEWAYS_EMA_ATR_MAX * atr
    ):
        state = "SIDEWAYS"
        label = "سوق عرضي"
        reason = "ضعف ADX وتقارب المتوسطات وحياد RSI"

    else:
        state = "TRANSITIONAL"
        label = "انتقالي أو غير واضح"
        reason = "المؤشرات غير متوافقة مع تصنيف واضح"

    return {
        "state": state,
        "label_ar": label,
        "reason": reason,
        "metrics": metrics,
    }
