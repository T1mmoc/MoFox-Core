from __future__ import annotations

import logging
from typing import Iterable, List, Sequence

import aiohttp

from ..codec import dumps_messages, loads_messages
from ..types import MessageEnvelope


class HttpMessageClient:
    """
    面向消息批量传输的 HTTP 客户端封装。
    """

    def __init__(
        self,
        base_url: str,
        *,
        session: aiohttp.ClientSession | None = None,
        timeout: aiohttp.ClientTimeout | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._timeout = timeout
        self._owns_session = session is None
        self._logger = logging.getLogger("mofox_bus.http_client")

    async def send_messages(
        self,
        messages: Sequence[MessageEnvelope],
        *,
        expect_reply: bool = False,
        path: str = "/messages",
    ) -> List[MessageEnvelope] | None:
        if not messages:
            return []
        session = await self._ensure_session()
        url = f"{self._base_url}{path}"
        payload = dumps_messages(messages)
        self._logger.debug("Sending %d message(s) -> %s", len(messages), url)
        async with session.post(url, data=payload, timeout=self._timeout) as resp:
            resp.raise_for_status()
            if not expect_reply:
                return None
            raw = await resp.read()
            replies = loads_messages(raw)
            self._logger.debug("Received %d reply message(s)", len(replies))
            return replies

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self._session

    async def __aenter__(self) -> "HttpMessageClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
