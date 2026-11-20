"""Expert Option market-data connector for ChartPulse-AI.

Data-only integration. No order placement or trading execution.
"""

import asyncio
import os
import time
from datetime import datetime, timezone
from pathlib import Path


SERVER_ORDER = (
    "INDIA",
    "HONG_KONG",
    "SINGAPORE",
    "UNITED_STATES",
)

TIMEFRAME_SECONDS = 900
TIMEFRAME_NAME = "M15"
SOURCE_NAME = "Expert Option"


def _load_client():
    """Load the independent local data-only client."""
    from expert_option.client import ExpertOptionClient

    servers = {
        "INDIA": "wss://fr24g1in.expertoption.com/",
        "HONG_KONG": "wss://fr24g1hk.expertoption.com/",
        "SINGAPORE": "wss://fr24g1sg.expertoption.com/",
        "UNITED_STATES": "wss://fr24g1us.expertoption.com/",
    }
    return servers, ExpertOptionClient


def _utc_timestamp(value):
    """Convert a candle time to a UTC Unix timestamp."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 1_000_000_000_000:
            numeric /= 1000
        return int(numeric)
    else:
        raise ValueError("Unsupported candle timestamp")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return int(dt.timestamp())


def _asset_symbol(asset):
    """Return an explicit platform symbol; never invent one."""
    for key in ("symbol", "ticker", "name"):
        value = asset.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(
        f"Asset {asset.get('id')} has no usable symbol"
    )


def normalize_candles(asset, candles, now=None):
    """Convert legacy-library candles into the storage ingestion format."""
    if not isinstance(asset, dict):
        raise ValueError("Invalid asset metadata")

    asset_id = int(asset["id"])
    if asset_id <= 0:
        raise ValueError("Invalid asset ID")

    symbol = _asset_symbol(asset)
    current_time = int(now if now is not None else time.time())

    # Platform candle timestamps must be confirmed as candle-start times.
    if os.getenv("EXPERT_OPTION_CANDLE_TIME_IS_START") != "true":
        raise RuntimeError(
            "Candle timestamp semantics are not confirmed. "
            "Set EXPERT_OPTION_CANDLE_TIME_IS_START=true only "
            "after verifying that platform timestamps mark candle starts."
        )

    if not isinstance(candles, (list, tuple)):
        raise ValueError("Invalid candles response")

    normalized = []
    seen = set()

    for candle in candles:
        if not isinstance(candle, dict):
            raise ValueError("Invalid candle item")

        candle_timeframe = candle.get("timeframe")
        if candle_timeframe is not None:
            if int(candle_timeframe) != TIMEFRAME_SECONDS:
                raise ValueError("Unexpected candle timeframe")

        timestamp = _utc_timestamp(candle.get("time"))

        if timestamp % TIMEFRAME_SECONDS != 0:
            raise ValueError("Candle timestamp is not M15-aligned")

        # Only completed candles are eligible for ingestion.
        if timestamp + TIMEFRAME_SECONDS > current_time:
            continue

        if timestamp in seen:
            continue

        item = {
            "symbol": symbol,
            "timeframe": TIMEFRAME_NAME,
            "timestamp": timestamp,
            "open": candle["open"],
            "high": candle["high"],
            "low": candle["low"],
            "close": candle["close"],
            "volume": candle.get("volume"),
        }

        normalized.append(item)
        seen.add(timestamp)

    normalized.sort(key=lambda item: item["timestamp"])
    return normalized


class ExpertOptionProvider:
    """Manage a real-account market-data connection and M15 ingestion."""

    def __init__(self):
        self.token = os.getenv("EXPERT_OPTION_SESSION_TOKEN")

        self.client = None
        self.server_name = None
        self.assets = []
        self.source_id = None
        self.collection_task = None

    async def connect(self):
        """Connect only when explicitly enabled in the environment."""
        if os.getenv("EXPERT_OPTION_ENABLED", "").lower() != "true":
            raise RuntimeError("Expert Option connector is disabled")
        if not self.token:
            raise RuntimeError("EXPERT_OPTION_SESSION_TOKEN is not configured")
        servers, client_class = _load_client()
        last_error = None

        for server_name in SERVER_ORDER:
            client = None

            try:
                client = client_class(
                    token=self.token,
                    demo=False,
                    url=servers[server_name],
                )

                await asyncio.wait_for(client.connect(), timeout=40)

                profile = await asyncio.wait_for(
                    client.fetch_profile(),
                    timeout=25,
                )

                if not isinstance(profile, dict):
                    raise RuntimeError("Invalid account profile")

                if int(profile.get("is_demo", -1)) != 0:
                    raise RuntimeError("Account is not verified as real")

                profile_id = (
                    profile.get("profile_id")
                    or profile.get("id")
                    or profile.get("user_id")
                )
                if not str(profile_id or "").isdigit():
                    raise RuntimeError("Account identity is invalid")
                if int(profile_id) <= 0:
                    raise RuntimeError("Account identity is invalid")

                assets = await asyncio.wait_for(
                    client.fetch_assets(),
                    timeout=40,
                )

                if not isinstance(assets, list):
                    raise RuntimeError("Invalid assets response")

                active_assets = [
                    asset for asset in assets
                    if isinstance(asset, dict)
                    and int(asset.get("is_active", 0)) == 1
                ]

                if not active_assets:
                    raise RuntimeError("No active assets were returned")

                self.client = client
                self.server_name = server_name
                self.assets = active_assets

                from storage import register_data_source

                self.source_id = register_data_source(
                    name=SOURCE_NAME,
                    status="active",
                    supported_markets=["Expert Option"],
                )

                return {
                    "connected": True,
                    "account_mode": "REAL",
                    "server": server_name,
                    "active_assets": len(active_assets),
                    "source_id": self.source_id,
                    "trading_execution": "disabled",
                }

            except Exception as exc:
                last_error = exc

                if client is not None:
                    try:
                        await client.disconnect()
                    except Exception:
                        pass

        raise RuntimeError(
            f"Could not establish a verified real-account connection: "
            f"{type(last_error).__name__ if last_error else 'unknown error'}"
        ) from last_error

    async def ingest_asset(self, asset):
        """Fetch and ingest completed M15 candles for one active asset."""
        if self.client is None or self.source_id is None:
            raise RuntimeError("Provider is not connected")

        asset_id = int(asset["id"])

        candles = await asyncio.wait_for(
            self.client.get_candles(
                asset_id=asset_id,
                period=TIMEFRAME_SECONDS,
                offset=0,
                duration=TIMEFRAME_SECONDS * 80,
                count_request=1,
            ),
            timeout=35,
        )

        normalized = normalize_candles(asset, candles)

        if not normalized:
            return {
                "asset_id": asset_id,
                "accepted": True,
                "inserted": 0,
                "reason": "NO_COMPLETED_CANDLES",
            }

        from ingestion import ingest_candles

        result = ingest_candles(
            source_id=self.source_id,
            candles=normalized,
            now=int(time.time()),
        )

        return {
            "asset_id": asset_id,
            **result,
        }

    async def ingest_cycle(self):
        """Ingest one M15 batch for each active asset."""
        if self.client is None:
            raise RuntimeError("Provider is not connected")

        results = []

        for asset in self.assets:
            try:
                result = await self.ingest_asset(asset)
                results.append(result)
            except Exception as exc:
                results.append({
                    "asset_id": asset.get("id"),
                    "accepted": False,
                    "error": type(exc).__name__,
                })

            await asyncio.sleep(0.2)

        return results

    async def start_collection(self, interval=60):
        """Explicitly start continuous market-data collection."""
        if self.collection_task and not self.collection_task.done():
            return {"status": "already_running"}

        if self.client is None:
            await self.connect()

        interval = max(5, int(interval))
        self.collection_task = asyncio.create_task(
            self._collection_loop(interval)
        )
        return {"status": "started", "interval": interval}

    async def _collection_loop(self, interval):
        while self.client is not None:
            try:
                await self.ingest_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Market data collection cycle failed"
                )
            await asyncio.sleep(interval)

    async def disconnect(self):
        task = self.collection_task
        self.collection_task = None

        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        """Close the connection without invoking trading operations."""
        client = self.client
        self.client = None
        self.assets = []

        if client is not None:
            await client.disconnect()
