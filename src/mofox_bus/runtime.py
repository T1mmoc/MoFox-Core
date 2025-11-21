from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable, Iterable, List

from .types import MessageEnvelope

Hook = Callable[[MessageEnvelope], Awaitable[None] | None]
ErrorHook = Callable[[MessageEnvelope, BaseException], Awaitable[None] | None]
Predicate = Callable[[MessageEnvelope], bool | Awaitable[bool]]
MessageHandler = Callable[[MessageEnvelope], Awaitable[MessageEnvelope | None] | MessageEnvelope | None]
BatchHandler = Callable[[List[MessageEnvelope]], Awaitable[List[MessageEnvelope] | None] | List[MessageEnvelope] | None]


class MessageProcessingError(RuntimeError):
    """封装处理链路中发生的异常。"""

    def __init__(self, message: MessageEnvelope, original: BaseException):
        detail = message.get("id", "<unknown>")
        super().__init__(f"Failed to handle message {detail}: {original}")  # pragma: no cover - str repr only
        self.message_envelope = message
        self.original = original


@dataclass
class MessageRoute:
    predicate: Predicate
    handler: MessageHandler
    name: str | None = None


class MessageRuntime:
    """
    负责调度消息路由、执行前后 hook 以及批量处理。
    """

    def __init__(self) -> None:
        self._routes: list[MessageRoute] = []
        self._before_hooks: list[Hook] = []
        self._after_hooks: list[Hook] = []
        self._error_hooks: list[ErrorHook] = []
        self._batch_handler: BatchHandler | None = None
        self._lock = threading.RLock()

    def add_route(self, predicate: Predicate, handler: MessageHandler, name: str | None = None) -> None:
        with self._lock:
            self._routes.append(MessageRoute(predicate=predicate, handler=handler, name=name))

    def route(self, predicate: Predicate, name: str | None = None) -> Callable[[MessageHandler], MessageHandler]:
        """
        装饰器写法，便于在核心逻辑中声明式注册。
        """

        def decorator(func: MessageHandler) -> MessageHandler:
            self.add_route(predicate, func, name=name)
            return func

        return decorator

    def set_batch_handler(self, handler: BatchHandler) -> None:
        self._batch_handler = handler

    def register_before_hook(self, hook: Hook) -> None:
        self._before_hooks.append(hook)

    def register_after_hook(self, hook: Hook) -> None:
        self._after_hooks.append(hook)

    def register_error_hook(self, hook: ErrorHook) -> None:
        self._error_hooks.append(hook)

    async def handle_message(self, message: MessageEnvelope) -> MessageEnvelope | None:
        await self._run_hooks(self._before_hooks, message)
        try:
            route = await self._match_route(message)
            if route is None:
                return None
            result = await _maybe_await(route.handler(message))
        except Exception as exc:  # pragma: no cover - tested indirectly
            await self._run_error_hooks(message, exc)
            raise MessageProcessingError(message, exc) from exc
        await self._run_hooks(self._after_hooks, message)
        return result

    async def handle_batch(self, messages: Iterable[MessageEnvelope]) -> List[MessageEnvelope]:
        batch = list(messages)
        if not batch:
            return []
        if self._batch_handler is not None:
            result = await _maybe_await(self._batch_handler(batch))
            return result or []
        responses: list[MessageEnvelope] = []
        for message in batch:
            response = await self.handle_message(message)
            if response is not None:
                responses.append(response)
        return responses

    async def _match_route(self, message: MessageEnvelope) -> MessageRoute | None:
        with self._lock:
            routes = list(self._routes)
        for route in routes:
            should_handle = await _maybe_await(route.predicate(message))
            if should_handle:
                return route
        return None

    async def _run_hooks(self, hooks: Iterable[Hook], message: MessageEnvelope) -> None:
        for hook in hooks:
            await _maybe_await(hook(message))

    async def _run_error_hooks(self, message: MessageEnvelope, exc: BaseException) -> None:
        for hook in self._error_hooks:
            await _maybe_await(hook(message, exc))


async def _maybe_await(result):
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result


__all__ = [
    "BatchHandler",
    "Hook",
    "MessageHandler",
    "MessageProcessingError",
    "MessageRoute",
    "MessageRuntime",
    "Predicate",
]
