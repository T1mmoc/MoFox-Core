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


MessagePayload = Dict[str, Any]
MessageHandler = Callable[[MessagePayload], Awaitable[None] | None]
DisconnectCallback = Callable[[str, str], Awaitable[None] | None]


def _attach_raw_bytes(payload: Any, raw_bytes: bytes) -> Any:
    """
    将原始字节数据附加到消息负载中

    Args:
        payload: 消息负载
        raw_bytes: 原始字节数据

    Returns:
        附加了原始数据的消息负载
    """
    if isinstance(payload, dict):
        payload.setdefault("raw_bytes", raw_bytes)
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                item.setdefault("raw_bytes", raw_bytes)
    return payload


def _encode_for_ws_send(message: Any, *, use_raw_bytes: bool = False) -> tuple[str | bytes, bool]:
    """
    编码消息用于 WebSocket 发送

    Args:
        message: 要发送的消息
        use_raw_bytes: 是否使用原始字节数据

    Returns:
        (编码后的数据, 是否为二进制格式)
    """
    if isinstance(message, (bytes, bytearray)):
        return bytes(message), True
    if use_raw_bytes and isinstance(message, dict):
        raw = message.get("raw_bytes")
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw), True
    payload = message
    if isinstance(payload, dict) and "raw_bytes" in payload and not use_raw_bytes:
        payload = {k: v for k, v in payload.items() if k != "raw_bytes"}
    data = orjson.dumps(payload)
    if use_raw_bytes:
        return data, True
    return data.decode("utf-8"), False


class BaseMessageHandler:
    """基础消息处理器，提供消息处理和任务管理功能"""

    def __init__(self) -> None:
        self.message_handlers: list[MessageHandler] = []
        self.background_tasks: set[asyncio.Task] = set()

    def register_message_handler(self, handler: MessageHandler) -> None:
        """
        注册消息处理器

        Args:
            handler: 消息处理函数
        """
        if handler not in self.message_handlers:
            self.message_handlers.append(handler)

    async def process_message(self, message: MessagePayload) -> None:
        """
        处理单条消息，并发执行所有注册的处理器

        Args:
            message: 消息负载
        """
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
                logging.getLogger("mofox_bus.server").exception("消息处理失败")
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
        queue_maxsize: int = 1000,
        worker_count: int = 1,
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
        self._message_queue: asyncio.Queue[MessagePayload] = asyncio.Queue(maxsize=queue_maxsize)
        self._worker_count = max(1, worker_count)
        self._worker_tasks: list[asyncio.Task] = []
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
                        raw_bytes = msg.get("bytes")
                        if raw_bytes is None and msg.get("text") is not None:
                            raw_bytes = msg["text"].encode("utf-8")
                        if not raw_bytes:
                            continue
                        try:
                            payload = orjson.loads(raw_bytes)
                        except orjson.JSONDecodeError:
                            logging.getLogger("mofox_bus.server").warning("Invalid JSON payload")
                            continue
                        payload = _attach_raw_bytes(payload, raw_bytes)
                        if isinstance(payload, list):
                            for item in payload:
                                await self._enqueue_message(item)
                        else:
                            await self._enqueue_message(payload)
                    elif msg["type"] == "websocket.disconnect":
                        break
            except WebSocketDisconnect:
                pass
            finally:
                await self._remove_connection(websocket, platform)

    async def _enqueue_message(self, payload: MessagePayload) -> None:
        if not self._worker_tasks:
            self._start_workers()
        try:
            self._message_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logging.getLogger("mofox_bus.server").warning("Message queue full, dropping message")

    def _start_workers(self) -> None:
        if self._worker_tasks:
            return
        self._running = True
        for _ in range(self._worker_count):
            task = asyncio.create_task(self._consumer_worker())
            self._worker_tasks.append(task)

    async def _stop_workers(self) -> None:
        if not self._worker_tasks:
            return
        self._running = False
        for task in self._worker_tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()
        while not self._message_queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._message_queue.get_nowait()
                self._message_queue.task_done()

    async def _consumer_worker(self) -> None:
        while self._running:
            try:
                payload = await self._message_queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self.process_message(payload)
            except Exception:  # pragma: no cover - best effort logging
                logging.getLogger("mofox_bus.server").exception("Error processing message")
            finally:
                self._message_queue.task_done()

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

    async def broadcast_message(self, message: MessagePayload | bytes, *, use_raw_bytes: bool = False) -> None:
        payload: MessagePayload | bytes = message
        data, is_binary = _encode_for_ws_send(payload, use_raw_bytes=use_raw_bytes)
        async with self._conn_lock:
            targets = list(self._connections)
        for ws in targets:
            if is_binary:
                await ws.send_bytes(data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8"))
            else:
                await ws.send_text(data if isinstance(data, str) else data.decode("utf-8"))

    async def broadcast_to_platform(
        self, platform: str, message: MessagePayload | bytes, *, use_raw_bytes: bool = False
    ) -> None:
        ws = self._platform_connections.get(platform)
        if ws is None:
            raise RuntimeError(f"平台 {platform} 没有活跃的连接")
        payload: MessagePayload | bytes = message
        data, is_binary = _encode_for_ws_send(payload, use_raw_bytes=use_raw_bytes)
        if is_binary:
            await ws.send_bytes(data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8"))
        else:
            await ws.send_text(data if isinstance(data, str) else data.decode("utf-8"))

    async def send_message(
        self, message: MessagePayload, *, prefer_raw_bytes: bool = False
    ) -> None:
        platform = message.get("message_info", {}).get("platform")
        if not platform:
            raise ValueError("message_info.platform is required to route the message")
        await self.broadcast_to_platform(platform, message, use_raw_bytes=prefer_raw_bytes)
    
    def run_sync(self) -> None:
        if not self._own_app:
            return
        asyncio.run(self.run())

    async def run(self) -> None:
        self._start_workers()
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
        await self._stop_workers()
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

    def __init__(
        self,
        mode: Literal["ws", "tcp"] = "ws",
        *,
        reconnect_interval: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
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
        self._on_disconnect: DisconnectCallback | None = None
        self._reconnect_interval = reconnect_interval
        self._logger = logger or logging.getLogger("mofox_bus.client")

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
        self._closed = False
        await self._establish_connection()

    def set_disconnect_callback(self, callback: DisconnectCallback) -> None:
        self._on_disconnect = callback

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

    async def _connect_once(self) -> None:
        await self._establish_connection()

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                    raw_bytes = msg.data if isinstance(msg.data, (bytes, bytearray)) else msg.data.encode("utf-8")
                    try:
                        payload = orjson.loads(raw_bytes)
                    except orjson.JSONDecodeError:
                        logging.getLogger("mofox_bus.client").warning("Invalid JSON payload")
                        continue
                    payload = _attach_raw_bytes(payload, raw_bytes)
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
            if not self._closed:
                await self._notify_disconnect("websocket disconnected")
                await self._reconnect()
            if self._ws:
                await self._ws.close()
            self._ws = None

    async def run(self) -> None:
        self._closed = False
        while not self._closed:
            if self._receive_task is None:
                await self._establish_connection()
            task = self._receive_task
            if task is None:
                break
            try:
                await task
            except asyncio.CancelledError:  # pragma: no cover - cancellation path
                raise

    async def send_message(self, message: MessagePayload | bytes, *, use_raw_bytes: bool = False) -> bool:
        ws = await self._ensure_ws()
        data, is_binary = _encode_for_ws_send(message, use_raw_bytes=use_raw_bytes)
        if is_binary:
            await ws.send_bytes(data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8"))
        else:
            await ws.send_str(data if isinstance(data, str) else data.decode("utf-8"))
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

    async def _notify_disconnect(self, reason: str) -> None:
        if self._on_disconnect is None:
            return
        try:
            result = self._on_disconnect(self._platform, reason)
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # pragma: no cover - best effort notification
            logging.getLogger("mofox_bus.client").exception("Disconnect callback failed")

    async def _reconnect(self) -> None:
        self._logger.info(f"WebSocket 连接断开, 正在 {self._reconnect_interval:.1f} 秒后重试")
        await asyncio.sleep(self._reconnect_interval)
        await self._connect_once()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def _ensure_ws(self) -> aiohttp.ClientWebSocketResponse:
        if self._ws is None or self._ws.closed:
            await self._connect_once()
        assert self._ws is not None
        return self._ws

    async def __aenter__(self) -> "MessageClient":
        if not self._url or not self._platform:
            raise RuntimeError("connect() must be called before using MessageClient as a context manager")
        await self._ensure_session()
        await self._ensure_ws()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.stop()


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
