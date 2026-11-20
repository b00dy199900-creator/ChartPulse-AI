"""Independent, data-only Expert Option client."""

import asyncio
import time
from uuid import uuid4

from expert_option.connection import WebSocketConnection


class ExpertOptionClient:
    def __init__(self, token, demo=False, url=None):
        if not url:
            raise ValueError("WebSocket server URL is required")

        self.token = token
        self.demo = demo
        self.url = url
        self.connection = WebSocketConnection(url, token)
        self.connection.reconnect_callback = self._restore_after_reconnect

        self.profile_data = None
        self.assets_data = None
        self.timeframes_data = None
        self.active_subscriptions = set()
        self.active_subscription_timeframes = {}

        self.candle_cache = {}

    async def connect(self):
        await self.connection.connect()
        await self._initialize()

    async def _initialize(self):
        await self.connection.send_action(
            "setContext",
            {"is_demo": 1 if self.demo else 0},
        )

        actions = []
        for action in (
            "userGroup",
            "profile",
            "assets",
            "getCurrency",
            "getCountries",
            "getCandlesTimeframes",
        ):
            actions.append({
                "action": action,
                "ns": str(uuid4()),
                "token": self.token,
            })

        await self.connection.send_multiple_action(actions, timeout=8)

    async def _restore_after_reconnect(self):
        saved = {
            asset_id: set(timeframes)
            for asset_id, timeframes in self.active_subscription_timeframes.items()
        }

        await self._initialize()

        self.active_subscriptions.clear()
        self.active_subscription_timeframes.clear()

        for asset_id, timeframes in saved.items():
            await self._ensure_candle_subscription(asset_id, sorted(timeframes))

    async def fetch_profile(self):
        response = await self.connection.request("profile", timeout=15)
        profile = response.get("profile")

        if not isinstance(profile, dict):
            message = response.get("message", {})
            profile = message.get("profile") if isinstance(message, dict) else None

        if not isinstance(profile, dict):
            raise RuntimeError("Invalid account profile response")

        self.profile_data = profile
        return profile

    async def fetch_assets(self):
        response = await self.connection.request("assets", timeout=20)
        assets = response.get("assets")

        if not isinstance(assets, list):
            message = response.get("message", {})
            assets = message.get("assets") if isinstance(message, dict) else None

        if not isinstance(assets, list):
            raise RuntimeError("Invalid assets response")

        self.assets_data = assets
        return assets

    async def fetch_timeframes(self):
        response = await self.connection.request(
            "getCandlesTimeframes", timeout=10
        )

        message = response.get("message", {})
        values = (
            response.get("timeframes")
            or (message.get("timeframes") if isinstance(message, dict) else None)
        )

        if not isinstance(values, list):
            raise RuntimeError("Invalid timeframe response")

        self.timeframes_data = values
        return values

    async def fetch_asset_timeframes(self, asset_id):
        return await self.fetch_timeframes()

    async def verify_asset_subscription(self, asset_id, timeframe):
        assets = await self.fetch_assets()
        asset = next(
            (item for item in assets
             if isinstance(item, dict) and str(item.get("id")) == str(asset_id)),
            None,
        )

        if not asset or not asset.get("is_active"):
            return False

        timeframes = await self.fetch_asset_timeframes(asset_id)
        return int(timeframe) in [int(value) for value in timeframes]

    async def _ensure_candle_subscription(self, asset_id, timeframes):
        asset_id = int(asset_id)
        requested = {int(value) for value in timeframes}
        active = self.active_subscription_timeframes.setdefault(asset_id, set())
        missing = requested - active

        if not missing:
            return

        await self.connection.send_action(
            "subscribeCandles",
            {"assets": [{
                "id": asset_id,
                "timeframes": sorted(missing),
            }]},
        )

        active.update(missing)
        self.active_subscriptions.update(
            (asset_id, value) for value in missing
        )

    @staticmethod
    def _parse_candles(response, asset_id, timeframe):
        message = response.get("message", {})
        if not isinstance(message, dict):
            raise ValueError("Invalid historical candle response")

        response_asset = message.get("assetId")
        if response_asset is not None and int(response_asset) != int(asset_id):
            raise ValueError("Historical response asset mismatch")

        result = []
        groups = message.get("candles", [])

        for group in groups if isinstance(groups, list) else []:
            if isinstance(group, dict) and "periods" in group:
                if int(group.get("tf", -1)) != int(timeframe):
                    continue

                for period_data in group.get("periods", []):
                    if not isinstance(period_data, (list, tuple)) or len(period_data) < 2:
                        continue

                    start = period_data[0]
                    if isinstance(start, str):
                        from datetime import datetime, timezone
                        start = start.replace("Z", "+00:00")
                        dt = datetime.fromisoformat(start)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        start = dt.timestamp()
                    else:
                        start = float(start)

                    if start > 1e12:
                        start /= 1000

                    for values in period_data[1]:
                        if not isinstance(values, (list, tuple)) or len(values) < 4:
                            continue

                        result.append({
                            "time": start,
                            "open": float(values[0]),
                            "high": float(values[1]),
                            "low": float(values[2]),
                            "close": float(values[3]),
                            "timeframe": int(timeframe),
                        })
                        start += int(timeframe)

            elif isinstance(group, dict) and "t" in group and "v" in group:
                if int(group.get("tf", timeframe)) != int(timeframe):
                    continue
                values = group["v"]
                if len(values) < 4:
                    continue

                timestamp = float(group["t"])
                if timestamp > 1e12:
                    timestamp /= 1000

                result.append({
                    "time": timestamp,
                    "open": float(values[0]),
                    "high": float(values[1]),
                    "low": float(values[2]),
                    "close": float(values[3]),
                    "timeframe": int(timeframe),
                })

        return result

    async def get_candles(
        self, asset_id, period, offset=0,
        duration=1800, count_request=1,
        start_time=None, as_dataframe=False,
    ):
        asset_id = int(asset_id)
        period = int(period)

        if not await self.verify_asset_subscription(asset_id, period):
            raise ValueError("Asset or timeframe is unavailable")

        await self._ensure_candle_subscription(asset_id, [period])

        end_time = int(start_time or (time.time() - offset))
        all_candles = {}

        for _ in range(max(1, int(count_request))):
            response = await self.connection.request(
                "assetHistoryCandles",
                {
                    "assetid": asset_id,
                    "periods": [[end_time - int(duration), end_time]],
                    "timeframes": [period],
                },
                timeout=15,
            )

            candles = self._parse_candles(response, asset_id, period)
            for candle in candles:
                all_candles[candle["time"]] = candle

            if not candles:
                break

            earliest = min(item["time"] for item in candles)
            if earliest >= end_time:
                break
            end_time = int(earliest) - 1

        result = [all_candles[key] for key in sorted(all_candles)]
        self.candle_cache.setdefault(asset_id, {})[period] = result
        return result

    async def subscribe_symbol(self, asset_id, timeframe):
        asset_id = int(asset_id)
        timeframe = int(timeframe)

        if not await self.verify_asset_subscription(asset_id, timeframe):
            raise ValueError("Asset or timeframe is unavailable")

        queue = self.connection.register_candle_subscriber(
            asset_id, timeframe
        )

        try:
            await self._ensure_candle_subscription(asset_id, [timeframe])

            while True:
                candle = await queue.get()
                if not isinstance(candle, dict):
                    continue

                values = candle.get("v")
                timestamp = candle.get("t")

                if not isinstance(values, (list, tuple)) or len(values) < 4:
                    continue

                if timestamp is None:
                    continue

                timestamp = float(timestamp)
                if timestamp > 1e12:
                    timestamp /= 1000

                yield {
                    "asset_id": asset_id,
                    "timeframe": timeframe,
                    "time": timestamp,
                    "open": float(values[0]),
                    "high": float(values[1]),
                    "low": float(values[2]),
                    "close": float(values[3]),
                }
        finally:
            self.connection.unregister_candle_subscriber(
                asset_id, timeframe, queue
            )

    async def disconnect(self):
        await self.connection.disconnect()
        self.active_subscriptions.clear()
        self.active_subscription_timeframes.clear()
