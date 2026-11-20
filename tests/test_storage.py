import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage import (
    SCHEMA_VERSION,
    connect_database,
    initialize_database,
    register_data_source,
    batch_insert_candles,
    get_recent_candles,
    get_candles_between,
)


class TestStorage(unittest.TestCase):

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "test.db"
        self.result = initialize_database(self.db)
        self.con = connect_database(self.db)

    def tearDown(self):
        self.con.close()
        self.temp.cleanup()

    def add_source(self, name="test-provider"):
        cursor = self.con.execute(
            """
            INSERT INTO data_sources (name, created_at, updated_at)
            VALUES (?, ?, ?)
            """,
            (name, 1000, 1000),
        )
        self.con.commit()
        return cursor.lastrowid

    def add_candle(self, source_id, timestamp=900, volume=100):
        return self.con.execute(
            """
            INSERT INTO candles (
                source_id, symbol, timeframe, timestamp,
                open, high, low, close, volume,
                received_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id, "EURUSD", "M15", timestamp,
                1.10, 1.12, 1.09, 1.11, volume,
                1000, 1000,
            ),
        )

    def test_schema_and_wal(self):
        self.assertEqual(self.result["journal_mode"].lower(), "wal")
        self.assertEqual(self.result["schema_version"], SCHEMA_VERSION)

        tables = {
            row["name"]
            for row in self.con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({
            "candles", "data_sources", "data_quality", "schema_info"
        }.issubset(tables))

    def test_initialization_is_idempotent(self):
        initialize_database(self.db)
        count = self.con.execute(
            "SELECT COUNT(*) FROM schema_info"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_schema_info_rejects_second_record(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                "INSERT INTO schema_info (id, version) VALUES (2, ?)",
                (SCHEMA_VERSION,),
            )

    def test_incompatible_schema_version_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "version.db"
            con = sqlite3.connect(db)
            con.execute("""
                CREATE TABLE schema_info (
                    version INTEGER NOT NULL
                )
            """)
            con.execute("INSERT INTO schema_info VALUES (999)")
            con.commit()
            con.close()

            with self.assertRaisesRegex(
                RuntimeError, "Database schema version mismatch"
            ):
                initialize_database(db)

    def test_failed_initialization_rolls_back_created_tables(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "rollback.db"
            con = sqlite3.connect(db)
            con.execute("""
                CREATE TABLE schema_info (
                    version INTEGER NOT NULL
                )
            """)
            con.execute("INSERT INTO schema_info VALUES (999)")
            con.commit()
            con.close()

            with self.assertRaises(RuntimeError):
                initialize_database(db)

            con = sqlite3.connect(db)
            tables = {
                row[0] for row in con.execute("""
                    SELECT name FROM sqlite_master
                    WHERE type='table'
                """)
            }
            con.close()

            self.assertEqual(tables, {"schema_info"})

    def test_historical_data_survives_reinitialization(self):
        source = self.add_source()
        self.add_candle(source, timestamp=900)
        self.con.commit()

        initialize_database(self.db)

        count = self.con.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0]
        self.assertEqual(count, 1)

        candle = self.con.execute(
            "SELECT close FROM candles WHERE timestamp=900"
        ).fetchone()
        self.assertAlmostEqual(candle["close"], 1.11)

    def test_duplicate_candle_rejected(self):
        source = self.add_source()
        self.add_candle(source)

        with self.assertRaises(sqlite3.IntegrityError):
            self.add_candle(source)

    def test_different_sources_can_store_same_candle(self):
        source1 = self.add_source()
        source2 = self.add_source("second-provider")

        self.add_candle(source1)
        self.add_candle(source2)

        count = self.con.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_invalid_ohlc_rejected(self):
        source = self.add_source()

        with self.assertRaises(sqlite3.IntegrityError):
            self.con.execute(
                """
                INSERT INTO candles (
                    source_id, symbol, timeframe, timestamp,
                    open, high, low, close, received_at, created_at
                )
                VALUES (?, 'EURUSD', 'M15', 900,
                        1.10, 1.00, 1.09, 1.11, 1000, 1000)
                """,
                (source,),
            )

    def test_invalid_source_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.add_candle(999999)

    def test_missing_volume_allowed(self):
        source = self.add_source()
        self.add_candle(source, volume=None)

        volume = self.con.execute(
            "SELECT volume FROM candles"
        ).fetchone()["volume"]
        self.assertIsNone(volume)

    def test_negative_volume_rejected(self):
        source = self.add_source()

        with self.assertRaises(sqlite3.IntegrityError):
            self.add_candle(source, volume=-1)

    def test_data_quality_record(self):
        source = self.add_source()
        self.con.execute(
            """
            INSERT INTO data_quality (
                source_id, symbol, timeframe,
                issue_type, severity, details, detected_at
            )
            VALUES (?, 'EURUSD', 'M15', 'missing_candle',
                    'warning', 'Test record', 1000)
            """,
            (source,),
        )

        count = self.con.execute(
            "SELECT COUNT(*) FROM data_quality"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_general_candle_index_is_used(self):
        plan = self.con.execute("""
            EXPLAIN QUERY PLAN
            SELECT timestamp, open, high, low, close
            FROM candles
            WHERE symbol = 'EURUSD' AND timeframe = 'M15'
            ORDER BY timestamp DESC
            LIMIT 100
        """).fetchall()

        details = " ".join(row["detail"] for row in plan)
        self.assertIn("idx_candles_lookup", details)

    def test_redundant_source_index_is_absent(self):
        indexes = {
            row["name"]
            for row in self.con.execute("PRAGMA index_list(candles)")
        }
        self.assertNotIn("idx_candles_source_lookup", indexes)


    def test_register_source_is_idempotent(self):
        first = register_data_source(
            "provider-a", supported_markets=["forex"], db_path=self.db
        )
        second = register_data_source(
            "provider-a", status="active",
            supported_markets=["forex", "stocks"], db_path=self.db
        )
        self.assertEqual(first, second)
        row = self.con.execute(
            "SELECT status, supported_markets FROM data_sources WHERE id=?",
            (first,),
        ).fetchone()
        self.assertEqual(row["status"], "active")
        self.assertEqual(row["supported_markets"], '["forex", "stocks"]')

    def test_batch_insert_and_duplicate_detection(self):
        source = self.add_source()
        candles = [
            {"symbol": "EURUSD", "timeframe": "M15", "timestamp": ts,
             "open": 1.10, "high": 1.12, "low": 1.09,
             "close": 1.11, "volume": 100}
            for ts in (900, 1800)
        ]
        result = batch_insert_candles(
            source, candles, received_at=2000, db_path=self.db
        )
        self.assertEqual(result, {
            "inserted": 2, "duplicates": 0, "conflicts": 0, "total": 2
        })

        repeated = batch_insert_candles(
            source, candles, received_at=2001, db_path=self.db
        )
        self.assertEqual(repeated["inserted"], 0)
        self.assertEqual(repeated["duplicates"], 2)
        self.assertEqual(repeated["conflicts"], 0)

    def test_conflict_is_logged_without_overwriting_original(self):
        source = self.add_source()
        original = {
            "symbol": "EURUSD", "timeframe": "M15", "timestamp": 900,
            "open": 1.10, "high": 1.12, "low": 1.09,
            "close": 1.11, "volume": 100,
        }
        batch_insert_candles(source, [original], db_path=self.db)

        changed = dict(original, close=1.115)
        result = batch_insert_candles(
            source, [changed], received_at=3000, db_path=self.db
        )
        self.assertEqual(result["conflicts"], 1)

        row = self.con.execute(
            "SELECT close FROM candles WHERE source_id=? AND timestamp=900",
            (source,),
        ).fetchone()
        self.assertAlmostEqual(row["close"], 1.11)

        quality = self.con.execute(
            "SELECT issue_type, details FROM data_quality"
        ).fetchone()
        self.assertEqual(quality["issue_type"], "candle_conflict")
        self.assertIn("1.115", quality["details"])

    def test_recent_candles_are_bounded_and_ascending(self):
        source = self.add_source()
        candles = [
            {"symbol": "EURUSD", "timeframe": "M15", "timestamp": ts,
             "open": 1.10, "high": 1.12, "low": 1.09,
             "close": 1.11, "volume": 100}
            for ts in (900, 1800, 2700, 3600)
        ]
        batch_insert_candles(source, candles, db_path=self.db)

        result = get_recent_candles(
            source, "EURUSD", "M15", limit=2, db_path=self.db
        )
        self.assertEqual([c["timestamp"] for c in result], [2700, 3600])

    def test_batch_failure_rolls_back_every_insert(self):
        source = self.add_source()
        self.con.execute("""
            CREATE TRIGGER reject_second_candle
            BEFORE INSERT ON candles
            WHEN NEW.timestamp = 1800
            BEGIN
                SELECT RAISE(ABORT, 'forced test failure');
            END
        """)
        self.con.commit()

        candles = [
            {"symbol": "EURUSD", "timeframe": "M15", "timestamp": ts,
             "open": 1.10, "high": 1.12, "low": 1.09,
             "close": 1.11, "volume": 100}
            for ts in (900, 1800)
        ]
        with self.assertRaises(sqlite3.IntegrityError):
            batch_insert_candles(source, candles, db_path=self.db)

        count = self.con.execute(
            "SELECT COUNT(*) FROM candles WHERE source_id=?", (source,)
        ).fetchone()[0]
        self.assertEqual(count, 0)

    def test_invalid_candle_rejected_before_writing(self):
        source = self.add_source()
        invalid = {
            "symbol": "EURUSD", "timeframe": "M15", "timestamp": 900,
            "open": 1.10, "high": 1.00, "low": 1.09,
            "close": 1.11, "volume": 100,
        }
        with self.assertRaises(ValueError):
            batch_insert_candles(source, [invalid], db_path=self.db)

        count = self.con.execute(
            "SELECT COUNT(*) FROM candles"
        ).fetchone()[0]
        self.assertEqual(count, 0)


    def test_candles_between_includes_boundaries_and_is_ascending(self):
        source = self.add_source()
        candles = [
            {"symbol": "EURUSD", "timeframe": "M15", "timestamp": ts,
             "open": 1.10, "high": 1.12, "low": 1.09,
             "close": 1.11, "volume": 100}
            for ts in (900, 1800, 2700, 3600, 4500)
        ]
        batch_insert_candles(source, candles, db_path=self.db)

        result = get_candles_between(
            source, "EURUSD", "M15", 1800, 3600, db_path=self.db
        )
        self.assertEqual(
            [c["timestamp"] for c in result],
            [1800, 2700, 3600],
        )

    def test_candles_between_respects_limit(self):
        source = self.add_source()
        candles = [
            {"symbol": "EURUSD", "timeframe": "M15", "timestamp": ts,
             "open": 1.10, "high": 1.12, "low": 1.09,
             "close": 1.11, "volume": 100}
            for ts in (900, 1800, 2700, 3600)
        ]
        batch_insert_candles(source, candles, db_path=self.db)

        result = get_candles_between(
            source, "EURUSD", "M15", 900, 3600,
            limit=2, db_path=self.db
        )
        self.assertEqual(
            [c["timestamp"] for c in result],
            [900, 1800],
        )

    def test_candles_between_rejects_invalid_range(self):
        source = self.add_source()

        with self.assertRaises(ValueError):
            get_candles_between(
                source, "EURUSD", "M15", 1800, 900, db_path=self.db
            )

        with self.assertRaises(ValueError):
            get_candles_between(
                source, "EURUSD", "M15", 900, 1800,
                limit=0, db_path=self.db
            )

    def test_candles_between_uses_index(self):
        source = self.add_source()
        plan = self.con.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT timestamp, open, high, low, close, volume
            FROM candles
            WHERE source_id = ?
              AND symbol = ?
              AND timeframe = ?
              AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (source, "EURUSD", "M15", 900, 3600, 100),
        ).fetchall()

        details = " ".join(row["detail"] for row in plan).upper()
        self.assertIn("INDEX", details)
        self.assertNotIn("TEMP B-TREE", details)


if __name__ == "__main__":
    unittest.main()
