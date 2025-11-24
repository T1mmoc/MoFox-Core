from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Iterable, List, Sequence

import aiohttp

from ..codec import dumps_messages, loads_messages
from ..types import MessageEnvelope

IncomingHandler = Callable[[MessageEnvelope], Awaitable[None]]


class WsMessageClient:
    """
    管理 WebSocket 连接，提供 send/receive API，并在后台读取消息
    """

    def __init__(
        self,
        url: str,
        *,
        handler: IncomingHandler | None = None,
        session: aiohttp.ClientSession | None = None,
        reconnect_interval: float = 5.0,
    ) -> None:
        self._url = url
        self._handler = handler
        self._session = session
        self._reconnect_interval = reconnect_interval
        self._owns_session = session is None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._receive_task: asyncio.Task | None = None
        self._closed = False
        self._logger = logging.getLogger("mofox_bus.ws_client")

    async def connect(self) -> None:
        await self._ensure_session()
        await self._connect_once()

    async def _connect_once(self) -> None:
        assert self._session is not None
        self._ws = await self._session.ws_connect(self._url)
        self._logger.info(f"已连接到 {self._url}")
        self._receive_task = asyncio.create_task(self._receive_loop())

    async def send_messages(self, messages: Sequence[MessageEnvelope]) -> None:
        if not messages:
            return
        ws = await self._ensure_ws()
        payload = dumps_messages(messages)
        await ws.send_bytes(payload)

    async def send_message(self, message: MessageEnvelope) -> None:
        await self.send_messages([message])

    async def close(self) -> None:
        self._closed = True
        if self._receive_task:
            self._receive_task.cancel()
        if self._ws:
            await self._ws.close()
            self._ws = None
        if self._owns_session and self._session:
            await self._session.close()
            self._session = None

    async def _receive_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if msg.type in (aiohttp.WSMsgType.BINARY, aiohttp.WSMsgType.TEXT):
                    envelopes = loads_messages(msg.data)
                    for env in envelopes:
                        if self._handler is not None:
                            await self._handler(env)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self._logger.warning(f"WebSocket 错误: {msg.data}")
                    break
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            return
        finally:
            if not self._closed:
                await self._reconnect()

    async def _reconnect(self) -> None:
        self._logger.info(f"WebSocket 断开, 正在 {self._reconnect_interval:.1f} 秒后重试")
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

    async def __aenter__(self) -> "WsMessageClient":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
