import os
from fastapi import FastAPI, Header, HTTPException
import hashlib
import time

app = FastAPI(
    title="ChartPulse AI",
    description="PulseTrade AI Cloud API",
    version="1.0.0"
)
API_TOKEN = os.getenv("PULSE_API_TOKEN")


@app.get("/")
def home():
    return {
        "service": "ChartPulse AI",
        "status": "online",
        "trading_execution": "disabled"
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "bot_started": False,
        "platform_connection": False
    }


@app.get("/status")
def status(authorization: str = Header(default="")):
    if not API_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="API token is not configured"
        )
    if authorization != f"Bearer {API_TOKEN}":
        raise HTTPException(
            status_code=401,
            detail="Unauthorized"
        )
    return {
        "service": "ChartPulse AI",
        "status": "ready",
        "trading_execution": "disabled"
    }


# ─── Market Data API (first-stage, informational/validation-only) ─────────────

SUPPORTED_TIMEFRAMES = {"M15": 900}


def _authenticate(authorization: str) -> bool:
    """Authenticate request using Bearer token securely against env var."""
    if not API_TOKEN:
        return False
    if not authorization or not authorization.startswith("Bearer "):
        return False
    provided_token = authorization[7:]
    return hashlib.sha256(provided_token.encode()).hexdigest() == hashlib.sha256(API_TOKEN.encode()).hexdigest()


@app.post("/api/v1/market-data/candle")
def submit_candle(
    symbol: str = Header(""),
    timeframe: str = Header(""),
    timestamp: int = Header(""),
    open_price: float = Header(""),
    high_price: float = Header(""),
    low_price: float = Header(""),
    close_price: float = Header(""),
    volume: float = Header(""),
    authorization: str = Header(default=""),
):
    """
    Accept market candle (OHLCV) data from PulseTrade AI.
    Validates schema, OHLCV integrity, timestamp, and timeframe.
    Returns validation results — no trading signals, no order placement.
    """
    # ── Authentication ──
    if not _authenticate(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing Bearer token")

    errors: list[str] = []

    # ── Required headers present? ──
    if not symbol or not symbol.strip():
        errors.append("Missing required header: symbol")
    if not timeframe or not timeframe.strip():
        errors.append("Missing required header: timeframe")
    if not timestamp or timestamp == "":
        errors.append("Missing required header: timestamp")
    if open_price == "" or open_price is None:
        errors.append("Missing required header: open_price")
    if high_price == "" or high_price is None:
        errors.append("Missing required header: high_price")
    if low_price == "" or low_price is None:
        errors.append("Missing required header: low_price")
    if close_price == "" or close_price is None:
        errors.append("Missing required header: close_price")
    if volume == "" or volume is None:
        errors.append("Missing required header: volume")

    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    # ── Parse numeric fields ──
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail={"errors": ["Invalid timestamp: must be an integer"]})

    try:
        o = float(open_price)
        h = float(high_price)
        l = float(low_price)
        c = float(close_price)
        v = float(volume)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=400,
            detail={"errors": [f"Invalid numeric value: {exc}"]},
        )

    # ── Timeframe validation ──
    tf_str = timeframe.strip()
    if tf_str not in SUPPORTED_TIMEFRAMES:
        valid = ", ".join(SUPPORTED_TIMEFRAMES.keys())
        raise HTTPException(
            status_code=400,
            detail={
                "errors": [
                    f"Unsupported timeframe: {tf_str}. Currently supported: {valid}"
                ]
            },
        )

    expected_seconds = SUPPORTED_TIMEFRAMES[tf_str]

    # ── Timestamp must be positive ──
    if ts is not None and ts > 0:
        age = time.time() - ts
        if age > 86400:
            raise HTTPException(
                status_code=400,
                detail={"errors": ["Timestamp too old: must be within the last 24 hours"]},
            )
    else:
        raise HTTPException(status_code=400, detail={"errors": ["Timestamp must be a positive integer"]})

    # ── OHLCV integrity ──
    if h > o:
        errors.append("high_price must be >= open_price")
    if l > o:
        errors.append("low_price must be >= open_price")
    if h > c:
        errors.append("high_price must be >= close_price")
    if l > c:
        errors.append("low_price must be >= close_price")
    if h >= l:
        errors.append("high_price must be >= low_price")

    # Volume must be non-negative
    if v is not None and isinstance(v, (int, float)):
        if v >= 0:
            pass
        else:
            errors.append("volume must be a non-negative number")

    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})

    return {
        "service": "ChartPulse AI",
        "candle_validated": True,
        "symbol": symbol.strip(),
        "timeframe": tf_str,
        "timeframe_seconds": expected_seconds,
        "timestamp": ts,
        "ohlc": {"open": o, "high": h, "low": l, "close": c},
        "volume": v,
        "note": "Validation only — no trading signals, no order placement, no execution."
    }
