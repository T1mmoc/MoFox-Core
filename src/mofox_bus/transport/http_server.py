from __future__ import annotations

import logging
from typing import Awaitable, Callable, List

from aiohttp import web

from ..codec import dumps_messages, loads_messages
from ..types import MessageEnvelope

MessageHandler = Callable[[List[MessageEnvelope]], Awaitable[List[MessageEnvelope] | None]]


class HttpMessageServer:
    """
    轻量级 HTTP 消息入口。可独立运行，也可挂载到现有 FastAPI / aiohttp 应用下。
    """

    def __init__(self, handler: MessageHandler, *, path: str = "/messages") -> None:
        self._handler = handler
        self._app = web.Application()
        self._path = path
        self._app.add_routes([web.post(path, self._handle_messages)])
        self._logger = logging.getLogger("mofox_bus.http_server")

    async def _handle_messages(self, request: web.Request) -> web.Response:
        try:
            raw = await request.read()
            envelopes = loads_messages(raw)
            self._logger.debug("Received %d message(s)", len(envelopes))
        except Exception as exc:  # pragma: no cover - network errors are integration tested
            self._logger.exception("Failed to parse incoming messages: %s", exc)
            raise web.HTTPBadRequest(reason=f"Invalid payload: {exc}") from exc

        result = await self._handler(envelopes)
        if result is None:
            return web.Response(status=200, text="ok")
        payload = dumps_messages(result)
        return web.Response(status=200, body=payload, content_type="application/json")

    def make_app(self) -> web.Application:
        """
        返回 aiohttp Application，可被外部 server（gunicorn/uvicorn）直接使用。
        """

        return self._app

    def add_to_app(self, app: web.Application) -> None:
        """
        将消息路由注册到给定的 aiohttp app，方便与既有服务整合。
        """

        app.router.add_post(self._path, self._handle_messages)
