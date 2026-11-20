"""Provider-independent M15 ingestion and market-calendar gap detection."""
import json
import time

from storage import (
    _positive_integer,
    _prepare_candle,
    batch_insert_candles,
    connect_database,
)

TIMEFRAMES = {"M15": 900}
SQL_CHUNK_SIZE = 500


def _database(db_path):
    return connect_database() if db_path is None else connect_database(db_path)


def ingest_candles(source_id, candles, now=None, db_path=None):
    """Validate, persist completed M15 candles, and resolve recovered gaps."""
    source_id = _positive_integer(source_id, "source_id")
    if not isinstance(candles, (list, tuple)) or not candles:
        raise ValueError("candles must be a non-empty list or tuple")

    now = int(time.time()) if now is None else _positive_integer(now, "now")
    prepared = [_prepare_candle(item) for item in candles]

    for candle in prepared:
        timeframe = candle["timeframe"]
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        interval = TIMEFRAMES[timeframe]
        timestamp = candle["timestamp"]
        if timestamp % interval:
            raise ValueError("Candle timestamp is not aligned to M15")
        if timestamp + interval > now:
            raise ValueError("Incomplete candle cannot be ingested")

    prepared.sort(
        key=lambda item: (item["symbol"], item["timeframe"], item["timestamp"])
    )

    kwargs = {} if db_path is None else {"db_path": db_path}
    result = batch_insert_candles(source_id, prepared, **kwargs)

    # Resolve previously recorded gaps only after the candle is persisted.
    connection = _database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        groups = {}
        for candle in prepared:
            key = (candle["symbol"], candle["timeframe"])
            groups.setdefault(key, []).append(candle["timestamp"])

        for (symbol, timeframe), timestamps in groups.items():
            for offset in range(0, len(timestamps), SQL_CHUNK_SIZE):
                chunk = timestamps[offset:offset + SQL_CHUNK_SIZE]
                placeholders = ",".join("?" for _ in chunk)
                connection.execute(
                    f"""UPDATE data_quality SET resolved_at = ?
                        WHERE source_id = ? AND symbol = ? AND timeframe = ?
                          AND issue_type = 'missing_candle'
                          AND resolved_at IS NULL
                          AND candle_timestamp IN ({placeholders})""",
                    (now, source_id, symbol, timeframe, *chunk),
                )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    return {"accepted": True, "source_id": source_id, **result}


def detect_gaps(
    source_id, symbol, timeframe, expected_timestamps, now=None, db_path=None
):
    """Record gaps only among timestamps explicitly supplied by a market calendar."""
    source_id = _positive_integer(source_id, "source_id")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if timeframe not in TIMEFRAMES:
        raise ValueError("Unsupported timeframe")
    if not isinstance(expected_timestamps, (list, tuple)):
        raise ValueError("expected_timestamps must be a list or tuple")

    expected = sorted({
        _positive_integer(ts, "expected timestamp")
        for ts in expected_timestamps
    })
    interval = TIMEFRAMES[timeframe]
    if any(ts % interval for ts in expected):
        raise ValueError("Expected timestamp is not aligned to M15")

    if not expected:
        return {"checked": 0, "missing": []}

    now = int(time.time()) if now is None else _positive_integer(now, "now")
    if any(ts + interval > now for ts in expected):
        raise ValueError("Cannot check an incomplete or future candle as missing")

    connection = _database(db_path)
    missing = []
    try:
        connection.execute("BEGIN IMMEDIATE")
        source = connection.execute(
            "SELECT id FROM data_sources WHERE id = ?", (source_id,)
        ).fetchone()
        if source is None:
            raise ValueError("Unknown data source")

        existing = set()
        for offset in range(0, len(expected), SQL_CHUNK_SIZE):
            chunk = expected[offset:offset + SQL_CHUNK_SIZE]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""SELECT timestamp FROM candles
                    WHERE source_id = ? AND symbol = ? AND timeframe = ?
                      AND timestamp IN ({placeholders})""",
                (source_id, symbol.strip(), timeframe, *chunk),
            ).fetchall()
            existing.update(row["timestamp"] for row in rows)

        for timestamp in expected:
            if timestamp in existing:
                continue
            missing.append(timestamp)
            connection.execute(
                """INSERT INTO data_quality
                   (source_id, symbol, timeframe, candle_timestamp,
                    issue_type, severity, details, detected_at)
                   SELECT ?, ?, ?, ?, 'missing_candle', 'warning', ?, ?
                   WHERE NOT EXISTS (
                       SELECT 1 FROM data_quality
                       WHERE source_id = ? AND symbol = ? AND timeframe = ?
                         AND candle_timestamp = ?
                         AND issue_type = 'missing_candle'
                         AND resolved_at IS NULL
                   )""",
                (
                    source_id, symbol.strip(), timeframe, timestamp,
                    json.dumps({"reason": "Expected by market calendar but not received"}),
                    now, source_id, symbol.strip(), timeframe, timestamp,
                ),
            )

        connection.commit()
        return {"checked": len(expected), "missing": missing}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
