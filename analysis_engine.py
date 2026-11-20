"""Market analysis orchestration, isolated from storage and indicators."""
import time

from indicators import calculate_indicators
from market_regime import classify_market_regime
from quality_filters import evaluate_trade_quality
from storage import (
    DEFAULT_DB_PATH,
    _positive_integer,
    connect_database,
    get_recent_candles,
)

TIMEFRAME_SECONDS = {"M15": 900}
MIN_ANALYSIS_CANDLES = 100
MAX_ANALYSIS_CANDLES = 500


def analyze_market(
    source_id,
    symbol,
    timeframe="M15",
    limit=100,
    now=None,
    db_path=DEFAULT_DB_PATH,
):
    """Load validated completed candles and calculate technical indicators."""
    source_id = _positive_integer(source_id, "source_id")

    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    symbol = symbol.strip()

    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError("Unsupported timeframe")

    limit = _positive_integer(limit, "limit")
    if limit < MIN_ANALYSIS_CANDLES:
        raise ValueError(
            f"At least {MIN_ANALYSIS_CANDLES} candles are required"
        )
    if limit > MAX_ANALYSIS_CANDLES:
        raise ValueError(
            f"At most {MAX_ANALYSIS_CANDLES} candles can be analyzed"
        )

    now = int(time.time()) if now is None else _positive_integer(now, "now")
    interval = TIMEFRAME_SECONDS[timeframe]

    candles = get_recent_candles(
        source_id, symbol, timeframe, limit=limit, db_path=db_path
    )
    if len(candles) < MIN_ANALYSIS_CANDLES:
        raise ValueError(
            f"Insufficient history: {len(candles)} candles; "
            f"{MIN_ANALYSIS_CANDLES} required"
        )

    previous_timestamp = None
    for index, candle in enumerate(candles):
        timestamp = _positive_integer(candle.get("timestamp"), "timestamp")
        if timestamp % interval:
            raise ValueError(f"Candle {index} timestamp is not M15-aligned")
        if timestamp + interval > now:
            raise ValueError(f"Candle {index} is incomplete")
        if previous_timestamp is not None and timestamp <= previous_timestamp:
            raise ValueError("Candles must be strictly chronological")
        previous_timestamp = timestamp

    first_timestamp = candles[0]["timestamp"]
    last_timestamp = candles[-1]["timestamp"]

    connection = connect_database(db_path)
    try:
        source = connection.execute(
            "SELECT id, status FROM data_sources WHERE id = ?", (source_id,)
        ).fetchone()
        if source is None:
            raise ValueError("Unknown data source")
        if source["status"] in ("inactive", "error"):
            raise ValueError(f"Data source is {source['status']}")

        unresolved_gap = connection.execute(
            """SELECT candle_timestamp FROM data_quality
               WHERE source_id = ? AND symbol = ? AND timeframe = ?
                 AND issue_type = 'missing_candle'
                 AND resolved_at IS NULL
                 AND candle_timestamp BETWEEN ? AND ?
               ORDER BY candle_timestamp ASC LIMIT 1""",
            (
                source_id, symbol, timeframe,
                first_timestamp, last_timestamp,
            ),
        ).fetchone()

        if unresolved_gap is not None:
            raise ValueError(
                "Unresolved missing candle at timestamp "
                f"{unresolved_gap['candle_timestamp']}"
            )

        unresolved_conflict = connection.execute(
            """SELECT candle_timestamp FROM data_quality
               WHERE source_id = ? AND symbol = ? AND timeframe = ?
               AND issue_type = 'candle_conflict'
               AND resolved_at IS NULL
               AND candle_timestamp BETWEEN ? AND ?
               ORDER BY candle_timestamp ASC LIMIT 1""",
            (source_id, symbol, timeframe, first_timestamp, last_timestamp),
        ).fetchone()
        if unresolved_conflict is not None:
            raise ValueError(
                "Unresolved candle conflict at timestamp "
                f"{unresolved_conflict['candle_timestamp']}"
            )
    finally:
        connection.close()

    indicators = calculate_indicators(candles)
    regime = classify_market_regime(candles, indicators)
    quality = evaluate_trade_quality(indicators, regime)

    return {
        "status": "ready" if quality["eligible"] else "blocked",
        "eligible": quality["eligible"],
        "quality": quality,
        "block_reasons": quality["reasons"],
        "source_id": source_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "candles_used": len(candles),
        "first_timestamp": first_timestamp,
        "last_timestamp": last_timestamp,
        "indicators": indicators,
        "market_regime": regime,
    }
