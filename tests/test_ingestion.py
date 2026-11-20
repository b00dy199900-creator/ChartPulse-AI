import pytest

from ingestion import detect_gaps, ingest_candles
from storage import (
    get_recent_candles,
    initialize_database,
    register_data_source,
    connect_database,
)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "phase3.db"
    initialize_database(path)
    source_id = register_data_source(
        "phase3-test",
        supported_markets=["forex", "indices", "stocks", "crypto"],
        db_path=path,
    )
    return path, source_id


def candle(timestamp, close=1.0):
    return {
        "symbol": "EURUSD",
        "timeframe": "M15",
        "timestamp": timestamp,
        "open": close,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close + 0.1,
        "volume": 10,
    }


def test_ingestion_sorts_persists_and_handles_duplicates(database):
    path, source_id = database
    batch = [candle(1800), candle(900)]
    first = ingest_candles(source_id, batch, now=4000, db_path=path)
    second = ingest_candles(source_id, batch, now=4000, db_path=path)
    rows = get_recent_candles(source_id, "EURUSD", "M15", db_path=path)
    assert first["inserted"] == 2
    assert second["duplicates"] == 2
    assert [row["timestamp"] for row in rows] == [900, 1800]


def test_ingestion_rejects_incomplete_misaligned_and_unsupported(database):
    path, source_id = database
    with pytest.raises(ValueError, match="Incomplete"):
        ingest_candles(source_id, [candle(900)], now=1000, db_path=path)
    with pytest.raises(ValueError, match="aligned"):
        ingest_candles(source_id, [candle(901)], now=4000, db_path=path)
    invalid = candle(900)
    invalid["timeframe"] = "H1"
    with pytest.raises(ValueError, match="Unsupported"):
        ingest_candles(source_id, [invalid], now=4000, db_path=path)


def test_gap_detection_and_recovery(database):
    path, source_id = database
    ingest_candles(source_id, [candle(900), candle(2700)], now=4000, db_path=path)
    expected = [900, 1800, 2700]
    first = detect_gaps(
        source_id, "EURUSD", "M15", expected, now=4000, db_path=path
    )
    second = detect_gaps(
        source_id, "EURUSD", "M15", expected, now=4000, db_path=path
    )
    assert first == {"checked": 3, "missing": [1800]}
    assert second == first

    ingest_candles(source_id, [candle(1800)], now=4000, db_path=path)
    connection = connect_database(path)
    try:
        row = connection.execute(
            """SELECT resolved_at FROM data_quality
               WHERE source_id = ? AND candle_timestamp = 1800
                 AND issue_type = 'missing_candle'""",
            (source_id,),
        ).fetchone()
        assert row is not None and row["resolved_at"] == 4000
    finally:
        connection.close()


def test_gap_detection_handles_large_lists_and_rejects_unfinished(database):
    path, source_id = database
    expected = list(range(900, 900 * 1201, 900))
    result = detect_gaps(
        source_id, "EURUSD", "M15", expected, now=expected[-1] + 900,
        db_path=path,
    )
    assert result["checked"] == 1200
    assert len(result["missing"]) == 1200

    with pytest.raises(ValueError, match="incomplete or future"):
        detect_gaps(
            source_id, "EURUSD", "M15", [expected[-1] + 900],
            now=expected[-1] + 1000, db_path=path,
        )
