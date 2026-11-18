

import os
from fastapi import FastAPI, Header, HTTPException

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
