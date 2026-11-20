import math
import unittest

from indicators import calculate_indicators


def make_candles(direction, count=34):
    candles = []

    for i in range(count):
        if direction == "up":
            o, c = 100 + i, 100.5 + i
            h, low = 101 + i, 99.5 + i
        elif direction == "down":
            o, c = 200 - i, 199.5 - i
            h, low = 200.5 - i, 198.5 - i
        else:
            o, c, h, low = 100, 100, 101, 99

        candles.append({
            "open": o,
            "high": h,
            "low": low,
            "close": c,
            "volume": 100,
        })

    return candles


class TestIndicators(unittest.TestCase):

    def test_uptrend_adx_and_rsi(self):
        result = calculate_indicators(make_candles("up"))
        self.assertAlmostEqual(result["adx_14"], 100.0)
        self.assertAlmostEqual(result["rsi_14"], 100.0)

    def test_downtrend_adx_and_rsi(self):
        result = calculate_indicators(make_candles("down"))
        self.assertAlmostEqual(result["adx_14"], 100.0)
        self.assertAlmostEqual(result["rsi_14"], 0.0)

    def test_sideways_market(self):
        result = calculate_indicators(make_candles("sideways"))
        self.assertAlmostEqual(result["adx_14"], 0.0)
        self.assertAlmostEqual(result["rsi_14"], 50.0)

    def test_required_indicator_fields(self):
        result = calculate_indicators(make_candles("up"))

        for key in ("ema_9", "ema_20", "rsi_14", "macd",
                    "atr_14", "adx_14", "volume_sma_20", "candle"):
            self.assertIn(key, result)

        self.assertTrue(math.isfinite(result["adx_14"]))
        self.assertEqual(result["volume_sma_20"], 100.0)

    def test_missing_volume_is_allowed(self):
        candles = make_candles("up")
        for candle in candles:
            candle.pop("volume")

        result = calculate_indicators(candles)
        self.assertIsNone(result["volume_sma_20"])

    def test_insufficient_candles_rejected(self):
        with self.assertRaises(ValueError):
            calculate_indicators(make_candles("up", 33))

    def test_invalid_ohlc_rejected(self):
        candles = make_candles("up")
        candles[-1]["high"] = 1

        with self.assertRaises(ValueError):
            calculate_indicators(candles)

    def test_non_finite_price_rejected(self):
        candles = make_candles("up")
        candles[-1]["close"] = float("nan")

        with self.assertRaises(ValueError):
            calculate_indicators(candles)


if __name__ == "__main__":
    unittest.main()
