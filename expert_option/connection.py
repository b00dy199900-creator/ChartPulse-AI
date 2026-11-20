"""Independent, data-only Expert Option WebSocket transport."""

import asyncio
import json
import logging
import ssl
import time
from collections import defaultdict
from uuid import uuid4

import websockets
from websockets.exceptions import ConnectionClosed


class WebSocketConnection:
    def __init__(self, url, token, logger=None):
        self.url = url
        self.token = token
        self.logger = logger or logging.getLogger(__name__)

        self.ws = None
        self.connected = False
        self.closing = False
        self._replacing_connection = False

        self.pending = {}
        self.early_responses = {}
        self.recv_lock = asyncio.Lock()
        self.connect_lock = asyncio.Lock()

        self.receive_task = None
        self.ping_task = None
        self.reconnect_task = None
        self.reconnect_callback = None

        self.candle_subscribers = defaultdict(set)

    async def connect(self):
        async with self.connect_lock:
            if self.connected and self.ws:
                return True

            self.closing = False

            self._replacing_connection = True
            current = asyncio.current_task()
            old_tasks = (
                self.receive_task,
                self.ping_task,
            )

            for task in old_tasks:
                if task and task is not current and not task.done():
                    task.cancel()

            await asyncio.gather(
                *(
                    task for task in old_tasks
                    if task and task is not current
                ),
                return_exceptions=True,
            )

            self.receive_task = None
            self.ping_task = None

            await self._close_socket()

            headers = {
                "Origin": "https://app.expertoption.com",
                "User-Agent": "Mozilla/5.0",
            }

            self.ws = await websockets.connect(
                self.url,
                ssl=ssl.create_default_context(),
                additional_headers=headers,
                ping_interval=None,
                close_timeout=5,
            )

            self.connected = True
            self.receive_task = asyncio.create_task(self._receive_loop())
            self.ping_task = asyncio.create_task(self._ping_loop())
            self._replacing_connection = False

            self.logger.info("Market data WebSocket connected")
            return True

    async def send(self, payload):
        if not self.connected or self.ws is None:
            raise ConnectionError("WebSocket is not connected")

        data = payload if isinstance(payload, str) else json.dumps(payload)

        try:
            await self.ws.send(data)
        except Exception:
            self.connected = False
            self._schedule_reconnect()
            raise

    async def send_action(self, action, message=None):
        await self.send({
            "action": action,
            "message": message or {},
            "ns": str(uuid4()),
            "token": self.token,
        })

    async def request(self, action, message=None, timeout=10):
        ns = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self.pending[ns] = future

        payload = {
            "action": action,
            "message": message or {},
            "ns": ns,
            "token": self.token,
        }

        try:
            early = self.early_responses.pop(ns, None)
            if early is not None:
                return early

            await self.send(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending.pop(ns, None)

    async def send_multiple_action(self, actions, timeout=8):
        outer_ns = str(uuid4())
        futures = {}

        for item in actions:
            ns = item.get("ns")
            if ns:
                futures[ns] = asyncio.get_running_loop().create_future()
                self.pending[ns] = futures[ns]

        payload = {
            "action": "multipleAction",
            "message": {"actions": actions},
            "ns": outer_ns,
            "token": self.token,
        }

        try:
            await self.send(payload)
            if not futures:
                return {}

            results = await asyncio.gather(
                *(
                    asyncio.wait_for(future, timeout=timeout)
                    for future in futures.values()
                ),
                return_exceptions=True,
            )
            return dict(zip(futures.keys(), results))
        finally:
            for ns in futures:
                self.pending.pop(ns, None)

    async def _receive_loop(self):
        try:
            async for raw in self.ws:
                try:
                    response = json.loads(raw)
                except (TypeError, ValueError):
                    continue

                if not isinstance(response, dict):
                    continue

                self._route_response(response)

                message = response.get("message")
                if isinstance(message, dict):
                    nested = message.get("actions")
                    if isinstance(nested, list):
                        for item in nested:
                            if isinstance(item, dict):
                                self._route_response(item)

                action = response.get("action")
                if action in ("candles", "assetHistoryCandles"):
                    self._dispatch_candles(response)

        except asyncio.CancelledError:
            raise
        except ConnectionClosed:
            self.logger.warning("Market data WebSocket closed")
        except Exception:
            self.logger.exception("WebSocket receive loop failed")
        finally:
            self.connected = False
            self._fail_pending()
            if not self.closing and not self._replacing_connection:
                self._schedule_reconnect()

    def _route_response(self, response):
        ns = response.get("ns")
        if not ns:
            return

        future = self.pending.get(ns)
        if future is not None and not future.done():
            if response.get("action") == "error":
                future.set_exception(
                    RuntimeError("Expert Option returned an error response")
                )
            else:
                future.set_result(response)
        else:
            self.early_responses[ns] = response

        if len(self.early_responses) > 1000:
            self.early_responses.clear()

    def register_candle_subscriber(self, asset_id, timeframe):
        queue = asyncio.Queue(maxsize=500)
        key = (int(asset_id), int(timeframe))
        self.candle_subscribers[key].add(queue)
        return queue

    def unregister_candle_subscriber(self, asset_id, timeframe, queue):
        key = (int(asset_id), int(timeframe))
        subscribers = self.candle_subscribers.get(key)
        if subscribers:
            subscribers.discard(queue)
            if not subscribers:
                self.candle_subscribers.pop(key, None)

    def _dispatch_candles(self, response):
        message = response.get("message", {})
        if not isinstance(message, dict):
            return

        asset_id = message.get("assetId", message.get("asset_id"))
        candles = message.get("candles", [])

        if asset_id is None:
            return

        try:
            asset_id = int(asset_id)
        except (TypeError, ValueError):
            return

        for candle in candles if isinstance(candles, list) else []:
            if not isinstance(candle, dict):
                continue
            try:
                timeframe = int(candle.get("tf"))
            except (TypeError, ValueError):
                continue

            key = (asset_id, timeframe)
            for queue in tuple(self.candle_subscribers.get(key, ())):
                try:
                    queue.put_nowait(candle)
                except asyncio.QueueFull:
                    try:
                        queue.get_nowait()
                        queue.put_nowait(candle)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    async def _ping_loop(self):
        try:
            while self.connected and not self.closing:
                await asyncio.sleep(30)
                if self.connected:
                    await self.send({
                        "action": "ping",
                        "message": {},
                        "ns": str(uuid4()),
                        "token": self.token,
                    })
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.warning("WebSocket ping failed")
            self.connected = False
            self._schedule_reconnect()

    def _fail_pending(self):
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("WebSocket disconnected"))

    def _schedule_reconnect(self):
        if self.closing:
            return
        if self.reconnect_task and not self.reconnect_task.done():
            return
        self.reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self):
        delay = 2
        while not self.closing:
            try:
                await asyncio.sleep(delay)
                await self.connect()
                if self.reconnect_callback:
                    await self.reconnect_callback()
                self.logger.info("WebSocket reconnected")
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger.warning("WebSocket reconnect attempt failed")
                delay = min(delay * 2, 30)

    async def _close_socket(self):
        socket = self.ws
        self.ws = None
        self.connected = False
        if socket is not None:
            try:
                await socket.close()
            except Exception:
                pass

    async def disconnect(self):
        self.closing = True
        self.connected = False

        current = asyncio.current_task()
        tasks = (
            self.ping_task,
            self.receive_task,
            self.reconnect_task,
        )

        for task in tasks:
            if task and task is not current and not task.done():
                task.cancel()

        await self._close_socket()
        self._fail_pending()
        self.early_responses.clear()
        self.candle_subscribers.clear()

        await asyncio.gather(
            *(
                task for task in tasks
                if task and task is not current
            ),
            return_exceptions=True,
        )
