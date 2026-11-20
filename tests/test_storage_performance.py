import sqlite3
import time
import pytest

import storage


@pytest.mark.parametrize('total', [5000, 10000, 50000])
def test_recent_candles_query_uses_index_and_limit(tmp_path, total):
    db_path = tmp_path / "performance.db"
    storage.initialize_database(db_path)

    source_id = storage.register_data_source(
        "performance-provider",
        db_path=db_path,
    )

    batch_size = 500

    write_started = time.perf_counter()
    for start in range(0, total, batch_size):
        candles = [
            {
                "symbol": "EURUSD",
                "timeframe": "M15",
                "timestamp": 900 * (start + index + 1),
                "open": 1.1000,
                "high": 1.1010,
                "low": 1.0990,
                "close": 1.1005,
                "volume": 100,
            }
            for index in range(batch_size)
        ]

        result = storage.batch_insert_candles(
            source_id,
            candles,
            db_path=db_path,
        )
        assert result["inserted"] == batch_size
    write_seconds = time.perf_counter() - write_started

    started = time.perf_counter()
    recent = storage.get_recent_candles(
        source_id,
        "EURUSD",
        "M15",
        limit=100,
        db_path=db_path,
    )
    read_seconds = time.perf_counter() - started

    assert len(recent) == 100
    assert recent == sorted(recent, key=lambda candle: candle["timestamp"])
    assert recent[0]["timestamp"] == 900 * (total - 99)
    assert recent[-1]["timestamp"] == 900 * total

    with sqlite3.connect(db_path) as con:
        plan = con.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT *
            FROM candles
            WHERE source_id = ?
              AND symbol = ?
              AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT 100
            """,
            (source_id, "EURUSD", "M15"),
        ).fetchall()

    plan_text = " ".join(str(row) for row in plan).upper()

    assert "INDEX" in plan_text
    assert "TEMP B-TREE" not in plan_text

    print(
        f"\nSTORAGE_PERFORMANCE: rows={total}, "
        f"returned={len(recent)}, write_seconds={write_seconds:.6f}, "
f"read_seconds={read_seconds:.6f}"
    )
    print("BOUNDED_QUERY_OK")
    print("INDEX_QUERY_PLAN_OK")
