import pytest

from analysis_engine import analyze_market
from indicators import calculate_indicators
from storage import (
    batch_insert_candles,
    connect_database,
    initialize_database,
    register_data_source,
)


@pytest.fixture
def market_db(tmp_path):
    path = tmp_path / "analysis.db"
    initialize_database(path)
    source_id = register_data_source(
        "analysis-test", supported_markets=["forex"], db_path=path
    )
    candles = []
    for index in range(120):
        close = 100.0 + index
        candles.append({
            "symbol": "EURUSD",
            "timeframe": "M15",
            "timestamp": 900 * (index + 1),
            "open": close - 0.5,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 10.0,
        })
    batch_insert_candles(
        source_id, candles, received_at=200000, db_path=path
    )
    return path, source_id, candles


def test_market_analysis_and_reference_indicator_values(market_db):
    path, source_id, candles = market_db
    result = analyze_market(
        source_id, "EURUSD", limit=100,
        now=200000, db_path=path,
    )

    assert result["status"] == "blocked"
    assert result["eligible"] is False
    assert result["quality"]["score"] == 71
    assert result["quality"]["checks"]["macd_alignment"] is False
    assert result["candles_used"] == 100
    assert result["first_timestamp"] == candles[20]["timestamp"]
    assert result["last_timestamp"] == candles[-1]["timestamp"]

    values = result["indicators"]
    assert values["rsi_14"] == pytest.approx(100.0)
    assert values["atr_14"] == pytest.approx(2.0)
    assert values["adx_14"] == pytest.approx(100.0)
    assert values["ema_9"] == pytest.approx(215.0)
    assert values["ema_20"] == pytest.approx(209.5)
    assert values["macd"]["line"] == pytest.approx(7.0)
    assert values["macd"]["signal"] == pytest.approx(7.0)
    assert values["macd"]["histogram"] == pytest.approx(0.0)
    assert values["volume_sma_20"] == pytest.approx(10.0)


def test_analysis_rejects_insufficient_history_and_bad_limits(market_db):
    path, source_id, _ = market_db

    with pytest.raises(ValueError, match="At least 100"):
        analyze_market(
            source_id, "EURUSD", limit=99, now=200000, db_path=path
        )

    with pytest.raises(ValueError, match="At most 500"):
        analyze_market(
            source_id, "EURUSD", limit=501, now=200000, db_path=path
        )


def test_analysis_rejects_incomplete_latest_candle(market_db):
    path, source_id, candles = market_db
    now = candles[-1]["timestamp"] + 899

    with pytest.raises(ValueError, match="incomplete"):
        analyze_market(
            source_id, "EURUSD", limit=100, now=now, db_path=path
        )


def test_analysis_blocks_unresolved_gap(market_db):
    path, source_id, candles = market_db
    connection = connect_database(path)
    try:
        connection.execute(
            """DELETE FROM candles
               WHERE source_id = ? AND symbol = ? AND timeframe = ?
                 AND timestamp = ?""",
            (
                source_id, "EURUSD", "M15",
                candles[60]["timestamp"],
            ),
        )
        connection.execute(
            """INSERT INTO data_quality
               (source_id, symbol, timeframe, candle_timestamp,
                issue_type, severity, details, detected_at)
               VALUES (?, ?, ?, ?, 'missing_candle', 'warning', '', ?)""",
            (
                source_id, "EURUSD", "M15",
                candles[60]["timestamp"], 200000,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="Unresolved missing candle"):
        analyze_market(
            source_id, "EURUSD", limit=100,
            now=200000, db_path=path,
        )


def test_indicator_rejects_invalid_volume_and_ohlc():
    candles = []
    for index in range(100):
        close = 100.0 + index
        candles.append({
            "open": close - 0.5,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": 10,
        })

    invalid_volume = [dict(item) for item in candles]
    invalid_volume[-1]["volume"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        calculate_indicators(invalid_volume)

    invalid_ohlc = [dict(item) for item in candles]
    invalid_ohlc[-1]["high"] = invalid_ohlc[-1]["close"] - 1
    with pytest.raises(ValueError, match="invalid OHLC"):
        calculate_indicators(invalid_ohlc)


def test_analysis_blocks_inactive_source(market_db):
    path, source_id, _ = market_db
    connection = connect_database(path)
    try:
        connection.execute(
            "UPDATE data_sources SET status = 'inactive' WHERE id = ?",
            (source_id,),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="Data source is inactive"):
        analyze_market(source_id, "EURUSD", limit=100, now=200000, db_path=path)


def test_analysis_blocks_unresolved_candle_conflict(market_db):
    path, source_id, candles = market_db
    connection = connect_database(path)
    try:
        connection.execute(
            """INSERT INTO data_quality
               (source_id, symbol, timeframe, candle_timestamp,
                issue_type, severity, details, detected_at)
               VALUES (?, ?, ?, ?, 'candle_conflict', 'warning', '', ?)""",
            (source_id, "EURUSD", "M15", candles[60]["timestamp"], 200000),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(ValueError, match="Unresolved candle conflict"):
        analyze_market(source_id, "EURUSD", limit=100, now=200000, db_path=path)
