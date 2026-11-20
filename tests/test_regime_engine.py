import pytest

from market_regime import classify_market_regime


@pytest.fixture
def candles():
    result = []
    for i in range(120):
        close = 100.0 + i * 0.1
        result.append({
            "open": close - 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
        })
    return result


def metrics(
    ema9=102.0, ema20=101.0, rsi=55.0,
    adx=30.0, atr=2.0, histogram=0.2,
):
    return {
        "ema_9": ema9,
        "ema_20": ema20,
        "rsi_14": rsi,
        "adx_14": adx,
        "atr_14": atr,
        "macd": {"histogram": histogram},
    }


def test_uptrend_requires_indicator_agreement(candles):
    result = classify_market_regime(candles, metrics())
    assert result["state"] == "UPTREND"


def test_downtrend_requires_indicator_agreement(candles):
    result = classify_market_regime(
        candles,
        metrics(ema9=99.0, ema20=100.0, rsi=45.0,
                adx=30.0, histogram=-0.2),
    )
    assert result["state"] == "DOWNTREND"


def test_sideways_requires_weak_trend_and_close_averages(candles):
    result = classify_market_regime(
        candles,
        metrics(ema9=100.1, ema20=100.0, rsi=50.0,
                adx=15.0, histogram=0.0),
    )
    assert result["state"] == "SIDEWAYS"


def test_conflicting_indicators_are_transitional(candles):
    result = classify_market_regime(
        candles,
        metrics(ema9=102.0, ema20=101.0, rsi=50.0,
                adx=30.0, histogram=-0.2),
    )
    assert result["state"] == "TRANSITIONAL"


def test_high_volatility_has_priority(candles):
    values = metrics(atr=10.0)
    result = classify_market_regime(candles, values)
    assert result["state"] == "HIGH_VOLATILITY"
    assert result["metrics"]["volatility_ratio"] >= 1.5


def test_invalid_or_insufficient_inputs_are_rejected(candles):
    with pytest.raises(ValueError, match="At least"):
        classify_market_regime(candles[:33], metrics())

    invalid = metrics()
    invalid["rsi_14"] = float("nan")
    with pytest.raises(ValueError, match="Non-finite"):
        classify_market_regime(candles, invalid)

    invalid = metrics()
    invalid["adx_14"] = 101
    with pytest.raises(ValueError, match="valid ranges"):
        classify_market_regime(candles, invalid)
