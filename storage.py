"""Persistent, provider-independent SQLite storage for ChartPulse AI."""

import json
import math
import sqlite3
import time
from numbers import Real
from pathlib import Path

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "data" / "chartpulse.db"
SCHEMA_VERSION = 2


def connect_database(db_path=DEFAULT_DB_PATH):
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(path), timeout=30)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection
    except Exception:
        connection.close()
        raise


def initialize_database(db_path=DEFAULT_DB_PATH):
    connection = connect_database(db_path)

    statements = [
        """
        CREATE TABLE IF NOT EXISTS data_sources (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('unknown', 'active', 'inactive', 'error')),
            supported_markets TEXT NOT NULL DEFAULT '[]',
            last_received_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS candles (
            id INTEGER PRIMARY KEY,
            source_id INTEGER NOT NULL,
            symbol TEXT NOT NULL CHECK (length(trim(symbol)) > 0),
            timeframe TEXT NOT NULL CHECK (length(trim(timeframe)) > 0),
            timestamp INTEGER NOT NULL CHECK (timestamp > 0),
            open REAL NOT NULL CHECK (abs(open) <= 1.0e308),
            high REAL NOT NULL CHECK (abs(high) <= 1.0e308),
            low REAL NOT NULL CHECK (abs(low) <= 1.0e308),
            close REAL NOT NULL CHECK (abs(close) <= 1.0e308),
            volume REAL CHECK (
                volume IS NULL OR
                (volume >= 0 AND volume <= 1.0e308)
            ),
            received_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY (source_id) REFERENCES data_sources(id),
            UNIQUE (source_id, symbol, timeframe, timestamp),
            CHECK (high >= low),
            CHECK (high >= open AND high >= close),
            CHECK (low <= open AND low <= close)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_candles_lookup
        ON candles (symbol, timeframe, timestamp DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS data_quality (
            id INTEGER PRIMARY KEY,
            source_id INTEGER,
            symbol TEXT,
            timeframe TEXT,
            candle_timestamp INTEGER,
            issue_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'warning'
                CHECK (severity IN ('info', 'warning', 'error', 'critical')),
            details TEXT NOT NULL DEFAULT '',
            detected_at INTEGER NOT NULL,
            resolved_at INTEGER,
            FOREIGN KEY (source_id) REFERENCES data_sources(id)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_quality_source_time
        ON data_quality (source_id, detected_at DESC)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_quality_candle
        ON data_quality (symbol, timeframe, candle_timestamp)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_quality_unresolved
        ON data_quality (resolved_at, detected_at DESC)
        """,
        """
        CREATE TABLE IF NOT EXISTS schema_info (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL
        )
        """,
    ]

    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("BEGIN IMMEDIATE")

        for statement in statements:
            connection.execute(statement)

        current = connection.execute(
            "SELECT version FROM schema_info LIMIT 1"
        ).fetchone()

        if current is None:
            connection.execute(
                "INSERT INTO schema_info (id, version) VALUES (1, ?)",
                (SCHEMA_VERSION,),
            )
        elif current["version"] != SCHEMA_VERSION:
            raise RuntimeError("Database schema version mismatch")

        connection.commit()

        return {
            "database": str(Path(db_path)),
            "schema_version": SCHEMA_VERSION,
            "journal_mode": connection.execute(
                "PRAGMA journal_mode"
            ).fetchone()[0],
            "tables": [
                "candles",
                "data_sources",
                "data_quality",
                "schema_info",
            ],
        }

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _now():
    return int(time.time())


def _positive_integer(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _finite_number(value, field):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{field} must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    return value


def _prepare_candle(candle):
    if not isinstance(candle, dict):
        raise ValueError("Each candle must be a dictionary")

    symbol = candle.get("symbol")
    timeframe = candle.get("timeframe")

    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("timeframe must be a non-empty string")

    timestamp = _positive_integer(candle.get("timestamp"), "timestamp")
    open_price = _finite_number(candle.get("open"), "open")
    high = _finite_number(candle.get("high"), "high")
    low = _finite_number(candle.get("low"), "low")
    close = _finite_number(candle.get("close"), "close")

    if high < low or high < max(open_price, close) or low > min(open_price, close):
        raise ValueError("Invalid OHLC relationship")

    volume = candle.get("volume")
    if volume is not None:
        volume = _finite_number(volume, "volume")
        if volume < 0:
            raise ValueError("volume cannot be negative")

    return {
        "symbol": symbol.strip(),
        "timeframe": timeframe.strip(),
        "timestamp": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def register_data_source(
    name,
    status="unknown",
    supported_markets=None,
    db_path=DEFAULT_DB_PATH,
):
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string")
    if status not in ("unknown", "active", "inactive", "error"):
        raise ValueError("Invalid source status")

    markets = [] if supported_markets is None else supported_markets
    if not isinstance(markets, (list, tuple)) or not all(
        isinstance(item, str) and item.strip() for item in markets
    ):
        raise ValueError("supported_markets must be a list of non-empty strings")

    now = _now()
    connection = connect_database(db_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO data_sources
                (name, status, supported_markets, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                status = excluded.status,
                supported_markets = excluded.supported_markets,
                updated_at = excluded.updated_at
            """,
            (name.strip(), status, json.dumps(list(markets)), now, now),
        )
        row = connection.execute(
            "SELECT id FROM data_sources WHERE name = ?",
            (name.strip(),),
        ).fetchone()
        connection.commit()
        return row["id"]
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def batch_insert_candles(
    source_id,
    candles,
    received_at=None,
    db_path=DEFAULT_DB_PATH,
):
    source_id = _positive_integer(source_id, "source_id")
    if not isinstance(candles, (list, tuple)) or not candles:
        raise ValueError("candles must be a non-empty list or tuple")

    prepared = [_prepare_candle(candle) for candle in candles]
    received_at = _now() if received_at is None else _positive_integer(
        received_at, "received_at"
    )

    connection = connect_database(db_path)
    inserted = duplicates = conflicts = 0

    try:
        connection.execute("BEGIN IMMEDIATE")

        source = connection.execute(
            "SELECT id FROM data_sources WHERE id = ?",
            (source_id,),
        ).fetchone()
        if source is None:
            raise ValueError("Unknown data source")

        for candle in prepared:
            values = (
                source_id,
                candle["symbol"],
                candle["timeframe"],
                candle["timestamp"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
                received_at,
                received_at,
            )

            cursor = connection.execute(
                """
                INSERT INTO candles (
                    source_id, symbol, timeframe, timestamp,
                    open, high, low, close, volume,
                    received_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, symbol, timeframe, timestamp) DO NOTHING
                """,
                values,
            )

            if cursor.rowcount == 1:
                inserted += 1
                continue

            existing = connection.execute(
                """
                SELECT open, high, low, close, volume
                FROM candles
                WHERE source_id = ? AND symbol = ?
                  AND timeframe = ? AND timestamp = ?
                """,
                (
                    source_id,
                    candle["symbol"],
                    candle["timeframe"],
                    candle["timestamp"],
                ),
            ).fetchone()

            if existing is None:
                raise RuntimeError("Candle insert was ignored unexpectedly")

            incoming_values = tuple(
                candle[key] for key in ("open", "high", "low", "close", "volume")
            )
            existing_values = tuple(existing[key] for key in (
                "open", "high", "low", "close", "volume"
            ))

            if incoming_values == existing_values:
                duplicates += 1
            else:
                conflicts += 1
                details = json.dumps(
                    {
                        "existing": dict(existing),
                        "incoming": {
                            key: candle[key]
                            for key in ("open", "high", "low", "close", "volume")
                        },
                    },
                    allow_nan=False,
                    sort_keys=True,
                )
                connection.execute(
                    """
                    INSERT INTO data_quality (
                        source_id, symbol, timeframe, candle_timestamp,
                        issue_type, severity, details, detected_at
                    ) VALUES (?, ?, ?, ?, 'candle_conflict',
                              'warning', ?, ?)
                    """,
                    (
                        source_id,
                        candle["symbol"],
                        candle["timeframe"],
                        candle["timestamp"],
                        details,
                        received_at,
                    ),
                )

        connection.execute(
            """
            UPDATE data_sources
            SET last_received_at = CASE
                WHEN last_received_at IS NULL OR last_received_at < ?
                THEN ? ELSE last_received_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (received_at, received_at, received_at, source_id),
        )

        connection.commit()
        return {
            "inserted": inserted,
            "duplicates": duplicates,
            "conflicts": conflicts,
            "total": len(prepared),
        }

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_recent_candles(
    source_id,
    symbol,
    timeframe,
    limit=100,
    db_path=DEFAULT_DB_PATH,
):
    source_id = _positive_integer(source_id, "source_id")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("timeframe must be a non-empty string")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("limit must be a positive integer")

    connection = connect_database(db_path)
    try:
        rows = connection.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM candles
            WHERE source_id = ? AND symbol = ? AND timeframe = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (source_id, symbol.strip(), timeframe.strip(), limit),
        ).fetchall()

        return [dict(row) for row in reversed(rows)]
    finally:
        connection.close()

def get_candles_between(
    source_id,
    symbol,
    timeframe,
    start_timestamp,
    end_timestamp,
    limit=1000,
    db_path=DEFAULT_DB_PATH,
):
    source_id = _positive_integer(source_id, "source_id")
    start_timestamp = _positive_integer(start_timestamp, "start_timestamp")
    end_timestamp = _positive_integer(end_timestamp, "end_timestamp")

    if start_timestamp > end_timestamp:
        raise ValueError("start_timestamp cannot exceed end_timestamp")

    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    if not isinstance(timeframe, str) or not timeframe.strip():
        raise ValueError("timeframe must be a non-empty string")

    limit = _positive_integer(limit, "limit")

    connection = connect_database(db_path)
    try:
        rows = connection.execute(
            """
            SELECT timestamp, open, high, low, close, volume
            FROM candles
            WHERE source_id = ?
              AND symbol = ?
              AND timeframe = ?
              AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp ASC
            LIMIT ?
            """,
            (
                source_id,
                symbol.strip(),
                timeframe.strip(),
                start_timestamp,
                end_timestamp,
                limit,
            ),
        ).fetchall()

        return [dict(row) for row in rows]
    finally:
        connection.close()
