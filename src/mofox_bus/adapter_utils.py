from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Protocol

import orjson
from aiohttp import web as aiohttp_web
import websockets

from .types import MessageEnvelope


class CoreMessageSink(Protocol):
    async def send(self, message: MessageEnvelope) -> None: ...

    async def send_many(self, messages: list[MessageEnvelope]) -> None: ...  # pragma: no cover - optional


class WebSocketLike(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    @property
    def closed(self) -> bool: ...

    async def send(self, data: str | bytes) -> None: ...

    async def close(self) -> None: ...


@dataclass
class WebSocketAdapterOptions:
    url: str
    headers: dict[str, str] | None = None
    incoming_parser: Callable[[str | bytes], Any] | None = None
    outgoing_encoder: Callable[[MessageEnvelope], str | bytes] | None = None


@dataclass
class HttpAdapterOptions:
    host: str = "0.0.0.0"
    port: int = 8089
    path: str = "/adapter/messages"
    app: aiohttp_web.Application | None = None


AdapterTransportOptions = WebSocketAdapterOptions | HttpAdapterOptions | None


class BaseAdapter:
    """
    适配器基类：负责平台原始消息与 MessageEnvelope 之间的互转。
    子类需要实现平台入站解析与出站发送逻辑。
    """

    platform: str = "unknown"

    def __init__(self, core_sink: CoreMessageSink, transport: AdapterTransportOptions = None):
        """
        Args:
            core_sink: 核心消息入口，通常是 InProcessCoreSink 或自定义客户端。
            transport: 传入 WebSocketAdapterOptions / HttpAdapterOptions 即可自动管理监听逻辑。
        """
        self.core_sink = core_sink
        self._transport_config = transport
        self._ws: WebSocketLike | None = None
        self._ws_task: asyncio.Task | None = None
        self._http_runner: aiohttp_web.AppRunner | None = None
        self._http_site: aiohttp_web.BaseSite | None = None

    async def start(self) -> None:
        """根据配置自动启动 WS/HTTP 监听。"""
        if isinstance(self._transport_config, WebSocketAdapterOptions):
            await self._start_ws_transport(self._transport_config)
        elif isinstance(self._transport_config, HttpAdapterOptions):
            await self._start_http_transport(self._transport_config)

    async def stop(self) -> None:
        """停止自动管理的传输层。"""
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._http_site:
            await self._http_site.stop()
            self._http_site = None
        if self._http_runner:
            await self._http_runner.cleanup()
            self._http_runner = None

    async def on_platform_message(self, raw: Any) -> None:
        """处理平台下发的单条消息并交给核心。"""
        envelope = self.from_platform_message(raw)
        await self.core_sink.send(envelope)

    async def on_platform_messages(self, raw_messages: list[Any]) -> None:
        """批量推送入口，内部自动批量或逐条送入核心。"""
        envelopes = [self.from_platform_message(raw) for raw in raw_messages]
        await _send_many(self.core_sink, envelopes)

    async def send_to_platform(self, envelope: MessageEnvelope) -> None:
        """核心生成单条消息时调用，由子类或自动传输层发送。"""
        await self._send_platform_message(envelope)

    async def send_batch_to_platform(self, envelopes: list[MessageEnvelope]) -> None:
        """默认串行发送整批消息，子类可根据平台特性重写。"""
        for env in envelopes:
            await self._send_platform_message(env)

    def from_platform_message(self, raw: Any) -> MessageEnvelope:
        """子类必须实现：将平台原始结构转换为统一 MessageEnvelope。"""
        raise NotImplementedError

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:
        """子类必须实现：把 MessageEnvelope 转为平台格式并发送出去。"""
        if isinstance(self._transport_config, WebSocketAdapterOptions):
            await self._send_via_ws(envelope)
            return
        raise NotImplementedError

    async def _start_ws_transport(self, options: WebSocketAdapterOptions) -> None:
        self._ws = await websockets.connect(options.url, extra_headers=options.headers)
        self._ws_task = asyncio.create_task(self._ws_listen_loop(options))

    async def _ws_listen_loop(self, options: WebSocketAdapterOptions) -> None:
        assert self._ws is not None
        parser = options.incoming_parser or self._default_ws_parser
        try:
            async for raw in self._ws:
                payload = parser(raw)
                await self.on_platform_message(payload)
        finally:
            pass

    async def _send_via_ws(self, envelope: MessageEnvelope) -> None:
        if self._ws is None or self._ws.closed:
            raise RuntimeError("WebSocket transport is not active")
        encoder = None
        if isinstance(self._transport_config, WebSocketAdapterOptions):
            encoder = self._transport_config.outgoing_encoder
        data = encoder(envelope) if encoder else self._default_ws_encoder(envelope)
        await self._ws.send(data)

    async def _start_http_transport(self, options: HttpAdapterOptions) -> None:
        app = options.app or aiohttp_web.Application()
        app.add_routes([aiohttp_web.post(options.path, self._handle_http_request)])
        self._http_runner = aiohttp_web.AppRunner(app)
        await self._http_runner.setup()
        self._http_site = aiohttp_web.TCPSite(self._http_runner, options.host, options.port)
        await self._http_site.start()

    async def _handle_http_request(self, request: aiohttp_web.Request) -> aiohttp_web.Response:
        raw = await request.read()
        data = orjson.loads(raw) if raw else {}
        if isinstance(data, list):
            await self.on_platform_messages(data)
        else:
            await self.on_platform_message(data)
        return aiohttp_web.json_response({"status": "ok"})

    @staticmethod
    def _default_ws_parser(raw: str | bytes) -> Any:
        data = orjson.loads(raw)
        if isinstance(data, dict) and data.get("type") == "message" and "payload" in data:
            return data["payload"]
        return data

    @staticmethod
    def _default_ws_encoder(envelope: MessageEnvelope) -> bytes:
        return orjson.dumps({"type": "send", "payload": envelope})


class InProcessCoreSink:
    """
    简单的进程内 sink，实现 CoreMessageSink 协议。
    """

    def __init__(self, handler: Callable[[MessageEnvelope], Awaitable[None]]):
        self._handler = handler

    async def send(self, message: MessageEnvelope) -> None:
        await self._handler(message)

    async def send_many(self, messages: list[MessageEnvelope]) -> None:
        for message in messages:
            await self._handler(message)


async def _send_many(sink: CoreMessageSink, envelopes: list[MessageEnvelope]) -> None:
    send_many = getattr(sink, "send_many", None)
    if callable(send_many):
        await send_many(envelopes)
        return
    for env in envelopes:
        await sink.send(env)


class BatchDispatcher:
    """
    将 send 操作合并为批量发送，适合网络 IO 密集场景。
    """

    def __init__(
        self,
        sink: CoreMessageSink,
        *,
        max_batch_size: int = 50,
        flush_interval: float = 0.2,
    ) -> None:
        self._sink = sink
        self._max_batch_size = max_batch_size
        self._flush_interval = flush_interval
        self._buffer: list[MessageEnvelope] = []
        self._lock = asyncio.Lock()
        self._flush_task: asyncio.Task | None = None
        self._closed = False

    async def add(self, message: MessageEnvelope) -> None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Dispatcher closed")
            self._buffer.append(message)
            self._ensure_timer()
            if len(self._buffer) >= self._max_batch_size:
                await self._flush_locked()

    async def close(self) -> None:
        async with self._lock:
            self._closed = True
            await self._flush_locked()
            if self._flush_task:
                self._flush_task.cancel()
                self._flush_task = None

    def _ensure_timer(self) -> None:
        if self._flush_task is not None and not self._flush_task.done():
            return
        loop = asyncio.get_running_loop()
        self._flush_task = loop.create_task(self._flush_loop())

    async def _flush_loop(self) -> None:
        try:
            await asyncio.sleep(self._flush_interval)
            async with self._lock:
                await self._flush_locked()
        except asyncio.CancelledError:  # pragma: no cover - timer cancellation
            pass

    async def _flush_locked(self) -> None:
        if not self._buffer:
            return
        payload = list(self._buffer)
        self._buffer.clear()
        await self._sink.send_many(payload)


__all__ = [
    "AdapterTransportOptions",
    "BaseAdapter",
    "BatchDispatcher",
    "CoreMessageSink",
    "HttpAdapterOptions",
    "InProcessCoreSink",
    "WebSocketAdapterOptions",
]
