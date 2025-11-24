from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Awaitable, Callable, Iterable, List, Set

from aiohttp import WSMsgType, web

from ..codec import dumps_messages, loads_messages
from ..types import MessageEnvelope

WsMessageHandler = Callable[[MessageEnvelope], Awaitable[None]]


class WsMessageServer:
    """
    封装 WebSocket 服务端逻辑，负责接收消息并广播响应
    """

    def __init__(self, handler: WsMessageHandler, *, path: str = "/ws") -> None:
        self._handler = handler
        self._app = web.Application()
        self._path = path
        self._app.add_routes([web.get(path, self._handle_ws)])
        self._connections: Set[web.WebSocketResponse] = set()
        self._lock = asyncio.Lock()
        self._logger = logging.getLogger("mofox_bus.ws_server")

    async def _handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._logger.info(f"WebSocket 连接打开: {request.remote}")

        async with self._track_connection(ws):
            async for message in ws:
                if message.type in (WSMsgType.BINARY, WSMsgType.TEXT):
                    envelopes = loads_messages(message.data)
                    for env in envelopes:
                        await self._handler(env)
                elif message.type == WSMsgType.ERROR:
                    self._logger.warning(f"WebSocket 连接错误: {ws.exception()}")
                    break

        self._logger.info(f"WebSocket 连接关闭: {request.remote}")
        return ws

    @asynccontextmanager
    async def _track_connection(self, ws: web.WebSocketResponse):
        async with self._lock:
            self._connections.add(ws)
        try:
            yield
        finally:
            async with self._lock:
                self._connections.discard(ws)

    async def broadcast(self, messages: Iterable[MessageEnvelope]) -> None:
        payload = dumps_messages(list(messages))
        async with self._lock:
            targets = list(self._connections)
        for ws in targets:
            await ws.send_bytes(payload)

    def make_app(self) -> web.Application:
        return self._app
