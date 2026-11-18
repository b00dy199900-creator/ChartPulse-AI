import hmac
import math
import os
import time
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator


app = FastAPI(
    title="ChartPulse AI",
    description="PulseTrade AI Cloud API",
    version="1.1.0",
)

API_TOKEN = os.getenv("PULSE_API_TOKEN", "")
SUPPORTED_TIMEFRAMES = {"M15": 900}


class CandlePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    timeframe: str
    timestamp: StrictInt
    open: float
    high: float
    low: float
    close: float
    volume: float

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 20:
            raise ValueError("symbol must contain 1 to 20 non-whitespace characters")
        return value

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            raise ValueError("only M15 timeframe is currently supported")
        return value

    @field_validator("open", "high", "low", "close", "volume", mode="before")
    @classmethod
    def validate_finite_number(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a JSON number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite; NaN and Infinity are not accepted")
        return number


def authenticate(authorization: str) -> bool:
    if not API_TOKEN or not authorization.startswith("Bearer "):
        return False
    provided_token = authorization[7:]
    return hmac.compare_digest(provided_token, API_TOKEN)


@app.get("/")
def home():
    return {
        "service": "ChartPulse AI",
        "status": "online",
        "trading_execution": "disabled",
    }


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "bot_started": False,
        "platform_connection": False,
    }


@app.get("/status")
def status(authorization: str = Header(default="")):
    if not API_TOKEN:
        raise HTTPException(status_code=503, detail="API token is not configured")
    if not authenticate(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {
        "service": "ChartPulse AI",
        "status": "ready",
        "trading_execution": "disabled",
    }


@app.post("/api/v1/market-data/candle")
def submit_candle(
    payload: CandlePayload,
    authorization: str = Header(default=""),
):
    if not authenticate(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized: invalid or missing Bearer token")

    interval = SUPPORTED_TIMEFRAMES[payload.timeframe]
    now = int(time.time())
    candle_time = payload.timestamp

    if candle_time % interval != 0:
        raise HTTPException(status_code=400, detail="timestamp must align to an M15 candle boundary")
    if candle_time + interval > now:
        raise HTTPException(status_code=400, detail="candle is not complete")

    o, h, low, c, v = payload.open, payload.high, payload.low, payload.close, payload.volume
    if h &lt; max(o, c):
        raise HTTPException(status_code=400, detail="high must be greater than or equal to open and close")
    if low > min(o, c):
        raise HTTPException(status_code=400, detail="low must be less than or equal to open and close")
    if h &lt; low:
        raise HTTPException(status_code=400, detail="high must be greater than or equal to low")
    if v &lt; 0:
        raise HTTPException(status_code=400, detail="volume must be non-negative")

    return {
        "service": "ChartPulse AI",
        "candle_validated": True,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "timeframe_seconds": interval,
        "timestamp": candle_time,
        "ohlc": {"open": o, "high": h, "low": low, "close": c},
        "volume": v,
        "note": "Validation only — no trading signals, no order placement, no execution.",
    }
