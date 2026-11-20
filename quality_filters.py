"""Deterministic market quality filters. No trade execution."""
import math

QUALITY_THRESHOLD = 80
HIGH_VOLATILITY_RATIO = 1.5

def number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Invalid quality metric: {name}")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite quality metric: {name}")
    return value

def evaluate_trade_quality(indicators, regime):
    if not isinstance(indicators, dict) or not isinstance(regime, dict):
        raise ValueError("Invalid quality input")

    state = regime.get("state")
    direction = {"UPTREND": "bullish", "DOWNTREND": "bearish"}.get(state)

    ema9 = number(indicators.get("ema_9"), "ema_9")
    ema20 = number(indicators.get("ema_20"), "ema_20")
    rsi = number(indicators.get("rsi_14"), "rsi_14")
    adx = number(indicators.get("adx_14"), "adx_14")
    atr = number(indicators.get("atr_14"), "atr_14")
    macd = indicators.get("macd")
    candle = indicators.get("candle")
    metrics = regime.get("metrics")

    if not isinstance(macd, dict) or not isinstance(candle, dict):
        raise ValueError("Missing MACD or candle structure")
    if not isinstance(metrics, dict):
        raise ValueError("Missing regime metrics")
    if not 0 <= rsi <= 100 or not 0 <= adx <= 100 or atr < 0:
        raise ValueError("Indicator values outside valid ranges")

    histogram = number(macd.get("histogram"), "macd_histogram")
    volatility = metrics.get("volatility_ratio")
    if volatility is not None:
        volatility = number(volatility, "volatility_ratio")

    body = number(candle.get("body"), "candle_body")
    candle_range = number(candle.get("range"), "candle_range")
    candle_direction = candle.get("direction")
    if body < 0 or candle_range < 0 or body > candle_range:
        raise ValueError("Invalid candle structure")

    scoring_direction = direction or (
        "bullish" if ema9 > ema20 else
        "bearish" if ema9 < ema20 else None
    )

    adx_points = 28 if adx >= 35 else 24 if adx >= 30 else 20 if adx >= 25 else 0

    ema_ratio = abs(ema9 - ema20) / atr if atr > 0 else 0
    ema_points = 0
    if scoring_direction:
        ema_points = (
            22 if ema_ratio >= 0.10 else
            18 if ema_ratio >= 0.075 else
            14 if ema_ratio >= 0.05 else
            8 if ema_ratio >= 0.025 else 0
        )

    macd_aligned = (
        scoring_direction == "bullish" and histogram > 0
    ) or (
        scoring_direction == "bearish" and histogram < 0
    )
    macd_ratio = abs(histogram) / atr if atr > 0 else 0
    macd_points = (
        (17 if macd_ratio >= 0.05 else 11 if macd_ratio >= 0.025 else 6)
        if macd_aligned else 0
    )

    rsi_points = 0
    if scoring_direction == "bullish":
        rsi_points = 17 if rsi >= 60 else 12 if rsi >= 55 else 8 if rsi >= 52 else 0
    elif scoring_direction == "bearish":
        rsi_points = 17 if rsi <= 40 else 12 if rsi <= 45 else 8 if rsi <= 48 else 0

    body_ratio = body / candle_range if candle_range > 0 else 0
    candle_points = 0
    if candle_direction == scoring_direction:
        candle_points = (
            16 if body_ratio >= 0.70 else
            12 if body_ratio >= 0.50 else
            8 if body_ratio >= 0.35 else
            4 if body_ratio >= 0.25 else 0
        )

    score = adx_points + ema_points + macd_points + rsi_points + candle_points

    checks = {
        "directional_regime": direction is not None,
        "trend_quality": adx >= 25,
        "ema_alignment": direction is not None and (
            (direction == "bullish" and ema9 > ema20) or
            (direction == "bearish" and ema9 < ema20)
        ),
        "macd_alignment": direction is not None and macd_aligned,
        "momentum": (
            direction == "bullish" and rsi >= 52
        ) or (
            direction == "bearish" and rsi <= 48
        ),
        "volatility": volatility is not None and volatility < HIGH_VOLATILITY_RATIO,
        "candle_structure": (
            direction is not None and candle_direction == direction
            and candle_range > 0 and body_ratio >= 0.25
        ),
    }

    labels = {
        "directional_regime": "السوق ليس في اتجاه صاعد أو هابط واضح",
        "trend_quality": "قوة الاتجاه ADX أقل من 25",
        "ema_alignment": "المتوسطات لا تؤكد اتجاه السوق",
        "macd_alignment": "MACD لا يؤكد اتجاه السوق",
        "momentum": "RSI لا يؤكد زخم الاتجاه",
        "volatility": "التقلب مرتفع أو بياناته غير متاحة",
        "candle_structure": "بنية الشمعة لا تؤكد الاتجاه",
    }
    reasons = [labels[key] for key, passed in checks.items() if not passed]
    if score < QUALITY_THRESHOLD:
        reasons.append(f"درجة الجودة {score}/100 أقل من {QUALITY_THRESHOLD}")

    return {
        "eligible": all(checks.values()) and score >= QUALITY_THRESHOLD,
        "score": score,
        "maximum_score": 100,
        "threshold": QUALITY_THRESHOLD,
        "breakdown": {
            "adx": adx_points, "ema": ema_points,
            "macd": macd_points, "rsi": rsi_points,
            "candle_structure": candle_points,
        },
        "checks": checks,
        "reasons": reasons,
        "volume_confirmation": "not_used_optional",
    }
