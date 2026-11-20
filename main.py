import hmac
import math
import os
import time
from typing import Any
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from expert_option.provider import ExpertOptionProvider

app = FastAPI(
    title="ChartPulse AI",
    description="PulseTrade AI Cloud API",
    version="1.1.0",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc):
    safe_errors = [
        {
            "loc": list(error.get("loc", ())),
            "msg": error.get("msg", "Invalid request"),
            "type": error.get("type", "value_error"),
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": safe_errors})


from storage import DEFAULT_DB_PATH, initialize_database, register_data_source
from ingestion import ingest_candles
from analysis_engine import analyze_market

API_TOKEN = os.getenv("PULSE_API_TOKEN", "")
DB_PATH = Path(os.getenv("CHARTPULSE_DB_PATH", str(DEFAULT_DB_PATH)))
DATA_SOURCE_NAME = os.getenv(
    "CHARTPULSE_DATA_SOURCE", "market_data_provider"
).strip() or "market_data_provider"
MAX_CANDLE_AGE_SECONDS = 7 * 86400
SUPPORTED_TIMEFRAMES = {"M15": 900}


def ensure_data_source():
    initialize_database(DB_PATH)
    return register_data_source(DATA_SOURCE_NAME, db_path=DB_PATH)


class CandlePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    timeframe: str
    timestamp: StrictInt
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

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

    @field_validator("open", "high", "low", "close", mode="before")
    @classmethod
    def validate_finite_number(cls, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a JSON number")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite; NaN and Infinity are not accepted")
        return number

    @field_validator("volume", mode="before")
    @classmethod
    def validate_optional_volume(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("must be a JSON number or null")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("must be finite; NaN and Infinity are not accepted")
        return number


def authenticate(authorization: str) -> bool:
    if not API_TOKEN or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization[7:], API_TOKEN)


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



class AnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1, max_length=20)
    timeframe: str = "M15"
    limit: StrictInt = Field(default=100, ge=100, le=500)

    @field_validator("symbol")
    @classmethod
    def validate_analysis_symbol(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 20:
            raise ValueError("symbol must contain 1 to 20 non-whitespace characters")
        return value

    @field_validator("timeframe")
    @classmethod
    def validate_analysis_timeframe(cls, value: str) -> str:
        if value not in SUPPORTED_TIMEFRAMES:
            raise ValueError("only M15 timeframe is currently supported")
        return value


@app.post("/api/v1/market-data/candle")
def submit_candle(
    payload: CandlePayload,
    authorization: str = Header(default="")
):
    if not authenticate(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    interval = SUPPORTED_TIMEFRAMES[payload.timeframe]
    now = int(time.time())
    ts = payload.timestamp

    if ts <= 0:
        raise HTTPException(status_code=400, detail="timestamp must be positive")
    if ts > now:
        raise HTTPException(status_code=400, detail="timestamp cannot be in the future")
    if now - ts > MAX_CANDLE_AGE_SECONDS:
        raise HTTPException(status_code=400, detail="timestamp must be within the last 7 days")
    if ts % interval:
        raise HTTPException(status_code=400, detail="timestamp must align to an M15 candle boundary")
    if ts + interval > now:
        raise HTTPException(status_code=400, detail="candle is not complete")

    o, h, low, c, v = (
        payload.open, payload.high, payload.low, payload.close, payload.volume
    )

    if h < max(o, c):
        raise HTTPException(status_code=400, detail="high must be >= open and close")
    if low > min(o, c):
        raise HTTPException(status_code=400, detail="low must be <= open and close")
    if h < low:
        raise HTTPException(status_code=400, detail="high must be >= low")
    if v is not None and v < 0:
        raise HTTPException(status_code=400, detail="volume must be non-negative")

    source_id = ensure_data_source()
    candle = {
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "timestamp": ts,
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": v,
    }

    try:
        stored = ingest_candles(
            source_id, [candle], now=now, db_path=DB_PATH
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "service": "ChartPulse AI",
        "candle_validated": True,
        "candle_stored": True,
        "source_name": DATA_SOURCE_NAME,
        "source_id": source_id,
        "symbol": payload.symbol,
        "timeframe": payload.timeframe,
        "timeframe_seconds": interval,
        "timestamp": ts,
        "ohlc": {"open": o, "high": h, "low": low, "close": c},
        "volume": v,
        "storage": stored,
        "trading_execution": "disabled",
    }


@app.post("/api/v1/market-data/analyze")
def analyze_market_endpoint(
    payload: AnalysisRequest,
    authorization: str = Header(default="")
):
    if not authenticate(authorization):
        raise HTTPException(status_code=401, detail="Unauthorized")

    source_id = ensure_data_source()

    try:
        result = analyze_market(
            source_id=source_id,
            symbol=payload.symbol,
            timeframe=payload.timeframe,
            limit=payload.limit,
            db_path=DB_PATH,
        )
        return {
            "service": "ChartPulse AI",
            "source_name": DATA_SOURCE_NAME,
            "analysis": result,
            "trading_execution": "disabled",
        }
    except ValueError as exc:
        return {
            "service": "ChartPulse AI",
            "status": "blocked",
            "eligible": False,
            "reason": str(exc),
            "source_name": DATA_SOURCE_NAME,
            "trading_execution": "disabled",
        }


@app.on_event("startup")
async def initialize_expert_option():
    app.state.expert_option_provider = ExpertOptionProvider()
    app.state.expert_option_enabled = False


@app.on_event("shutdown")
async def shutdown_expert_option():
    provider = getattr(app.state, "expert_option_provider", None)
    if provider is not None:
        await provider.disconnect()
        app.state.expert_option_provider = None
