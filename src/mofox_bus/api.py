from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from typing import Any, Awaitable, Callable, Dict, Literal, Optional

import aiohttp
import orjson
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from .message_models import MessageBase

MessagePayload = Dict[str, Any]
MessageHandler = Callable[[MessagePayload], Awaitable[None] | None]


class BaseMessageHandler:
    def __init__(self) -> None:
        self.message_handlers: list[MessageHandler] = []
        self.background_tasks: set[asyncio.Task] = set()

    def register_message_handler(self, handler: MessageHandler) -> None:
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    async def process_message(self, message: MessagePayload) -> None:
        tasks: list[asyncio.Task] = []
        for handler in self.message_handlers:
            try:
                result = handler(message)
                if asyncio.iscoroutine(result):
                    task = asyncio.create_task(result)
                    tasks.append(task)
                    self.background_tasks.add(task)
                    task.add_done_callback(self.background_tasks.discard)
            except Exception:  # pragma: no cover - logging only
                logging.getLogger("mofox_bus.server").exception("Failed to handle message")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class MessageServer(BaseMessageHandler):
    """
    WebSocket 消息服务器，支持与 FastAPI 应用共享事件循环。
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 18000,
        *,
        enable_token: bool = False,
        app: FastAPI | None = None,
        path: str = "/ws",
        ssl_certfile: str | None = None,
        ssl_keyfile: str | None = None,
        mode: Literal["ws", "tcp"] = "ws",
        custom_logger: logging.Logger | None = None,
        enable_custom_uvicorn_logger: bool = False,
    ) -> None:
        super().__init__()
        if mode != "ws":
            raise NotImplementedError("Only WebSocket mode is supported in mofox_bus")
        if custom_logger:
            logging.getLogger("mofox_bus.server").handlers = custom_logger.handlers
        self.host = host
        self.port = port
        self._app = app or FastAPI()
        self._own_app = app is None
        self._path = path
        self._ssl_certfile = ssl_certfile
        self._ssl_keyfile = ssl_keyfile
        self._enable_token = enable_token
        self._valid_tokens: set[str] = set()
        self._connections: set[WebSocket] = set()
        self._platform_connections: dict[str, WebSocket] = {}
        self._conn_lock = asyncio.Lock()
        self._server: uvicorn.Server | None = None
        self._running = False
        self._setup_routes()

    def _setup_routes(self) -> None:
        @_self_websocket(self._app, self._path)
        async def websocket_endpoint(websocket: WebSocket) -> None:
            platform = websocket.headers.get("platform", "unknown")
            token = websocket.headers.get("authorization") or websocket.headers.get("Authorization")
            if self._enable_token and not await self.verify_token(token):
                await websocket.close(code=1008, reason="invalid token")
                return

            await websocket.accept()
            await self._register_connection(websocket, platform)
            try:
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.receive":
                        data = msg.get("text")
                        if data is None and msg.get("bytes") is not None:
                            data = msg["bytes"].decode("utf-8")
                        if not data:
                            continue
                        try:
                            payload = orjson.loads(data)
                        except orjson.JSONDecodeError:
                            logging.getLogger("mofox_bus.server").warning("Invalid JSON payload")
                            continue
                        if isinstance(payload, list):
                            for item in payload:
                                await self.process_message(item)
                        else:
                            await self.process_message(payload)
                    elif msg["type"] == "websocket.disconnect":
                        break
            except WebSocketDisconnect:
                pass
            finally:
                await self._remove_connection(websocket, platform)

    async def verify_token(self, token: str | None) -> bool:
        if not self._enable_token:
            return True
        return token in self._valid_tokens

    def add_valid_token(self, token: str) -> None:
        self._valid_tokens.add(token)

    def remove_valid_token(self, token: str) -> None:
        self._valid_tokens.discard(token)

    async def _register_connection(self, websocket: WebSocket, platform: str) -> None:
        async with self._conn_lock:
            self._connections.add(websocket)
            if platform:
                previous = self._platform_connections.get(platform)
                if previous and previous.client_state.name != "DISCONNECTED":
                    await previous.close(code=1000, reason="replaced")
                self._platform_connections[platform] = websocket

    async def _remove_connection(self, websocket: WebSocket, platform: str) -> None:
        async with self._conn_lock:
            self._connections.discard(websocket)
            if platform and self._platform_connections.get(platform) is websocket:
                del self._platform_connections[platform]

    async def broadcast_message(self, message: MessagePayload) -> None:
        data = orjson.dumps(message).decode("utf-8")
        async with self._conn_lock:
            targets = list(self._connections)
        for ws in targets:
            await ws.send_text(data)

    async def broadcast_to_platform(self, platform: str, message: MessagePayload) -> None:
        ws = self._platform_connections.get(platform)
        if ws is None:
            raise RuntimeError(f"No active connection for platform {platform}")
        await ws.send_text(orjson.dumps(message).decode("utf-8"))

    async def send_message(self, message: MessageBase | MessagePayload) -> None:
        payload = message.to_dict() if isinstance(message, MessageBase) else message
        platform = payload.get("message_info", {}).get("platform")
        if not platform:
            raise ValueError("message_info.platform is required to route the message")
        await self.broadcast_to_platform(platform, payload)

    def run_sync(self) -> None:
        if not self._own_app:
            return
        asyncio.run(self.run())

    async def run(self) -> None:
        self._running = True
        if not self._own_app:
            return
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            ssl_certfile=self._ssl_certfile,
            ssl_keyfile=self._ssl_keyfile,
            log_config=None,
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        try:
            await self._server.serve()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            pass

    async def stop(self) -> None:
        self._running = False
        if self._server:
            self._server.should_exit = True
            await self._server.shutdown()
            self._server = None
        async with self._conn_lock:
            targets = list(self._connections)
            self._connections.clear()
            self._platform_connections.clear()
        for ws in targets:
            try:
                await ws.close(code=1001, reason="server shutting down")
            except Exception:  # pragma: no cover - best effort
                pass
        for task in list(self.background_tasks):
            if not task.done():
                task.cancel()
        if self.background_tasks:
            await asyncio.gather(*self.background_tasks, return_exceptions=True)
        self.background_tasks.clear()


class MessageClient(BaseMessageHandler):
    """
    WebSocket 消息客户端，实现双向传输。
    """

    def __init__(self, mode: Literal["ws", "tcp"] = "ws") -> None:
        super().__init__()
        if mode != "ws":
            raise NotImplementedError("Only WebSocket mode is supported in mofox_bus")
        self._mode = mode
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task | None = None
        self._url: str = ""
        self._platform: str = ""
        self._token: str | None = None
        self._ssl_verify: str | None = None
        self._closed = False

    async def connect(
        self,
        *,
        url: str,
        platform: str,
        token: str | None = None,
        ssl_verify: str | None = None,
    ) -> None:
        self._url = url
        self._platform = platform
        self._token = token
        self._ssl_verify = ssl_verify
        await self._establish_connection()

    async def _establish_connection(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        headers = {"platform": self._platform}
        if self._token:
            headers["authorization"] = self._token
        ssl_context = None
        if self._ssl_verify:
            ssl_context = ssl.create_default_context(cafile=self._ssl_verify)
        self._ws = await self._session.ws_connect(self._url, headers=headers, ssl=ssl_context)
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    data = msg.data if isinstance(msg.data, str) else msg.data.decode("utf-8")
                    try:
                        payload = orjson.loads(data)
                    except orjson.JSONDecodeError:
                        logging.getLogger("mofox_bus.client").warning("Invalid JSON payload")
                        continue
                    if isinstance(payload, list):
                        for item in payload:
                            await self.process_message(item)
                    else:
                        await self.process_message(payload)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    break
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            pass
        finally:
            if self._ws:
                await self._ws.close()
            self._ws = None

    async def run(self) -> None:
        if self._receive_task is None:
            await self._establish_connection()
        try:
            if self._receive_task:
                await self._receive_task
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            pass

    async def send_message(self, message: MessagePayload) -> bool:
        if self._ws is None or self._ws.closed:
            raise RuntimeError("WebSocket connection is not established")
        await self._ws.send_str(orjson.dumps(message).decode("utf-8"))
        return True

    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def stop(self) -> None:
        self._closed = True
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._receive_task
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._session:
            await self._session.close()
            self._session = None


def _self_websocket(app: FastAPI, path: str):
    """
    装饰器工厂，兼容 FastAPI websocket 路由的声明方式。
    FastAPI 不允许直接重复注册同一路径，因此这里封装一个可复用的装饰器。
    """

    def decorator(func):
        app.add_api_websocket_route(path, func)
        return func

    return decorator


__all__ = ["BaseMessageHandler", "MessageClient", "MessageServer"]
