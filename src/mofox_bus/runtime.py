from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Iterable, List, Protocol

from .types import MessageEnvelope

Hook = Callable[[MessageEnvelope], Awaitable[None] | None]
ErrorHook = Callable[[MessageEnvelope, BaseException], Awaitable[None] | None]
Predicate = Callable[[MessageEnvelope], bool | Awaitable[bool]]
MessageHandler = Callable[[MessageEnvelope], Awaitable[MessageEnvelope | None] | MessageEnvelope | None]
BatchHandler = Callable[[List[MessageEnvelope]], Awaitable[List[MessageEnvelope] | None] | List[MessageEnvelope] | None]
MiddlewareCallable = Callable[[MessageEnvelope], Awaitable[MessageEnvelope | None]]


class Middleware(Protocol):
    async def __call__(self, message: MessageEnvelope, handler: MiddlewareCallable) -> MessageEnvelope | None: ...


class MessageProcessingError(RuntimeError):
    """封装处理链路中发生的异常。"""

    def __init__(self, message: MessageEnvelope, original: BaseException):
        detail = message.get("id", "<unknown>")
        super().__init__(f"处理消息 {detail} 时出错: {original}")
        self.message_envelope = message
        self.original = original


@dataclass
class MessageRoute:
    """消息路由配置，包含匹配条件和处理函数"""
    predicate: Predicate
    handler: MessageHandler
    name: str | None = None
    message_type: str | None = None
    event_types: set[str] | None = None


class MessageRuntime:
    """
    消息运行时环境，负责调度消息路由、执行前后处理钩子以及批量处理消息
    """

    def __init__(self) -> None:
        self._routes: list[MessageRoute] = []
        self._before_hooks: list[Hook] = []
        self._after_hooks: list[Hook] = []
        self._error_hooks: list[ErrorHook] = []
        self._batch_handler: BatchHandler | None = None
        self._lock = threading.RLock()
        self._middlewares: list[Middleware] = []
        self._type_routes: Dict[str, list[MessageRoute]] = {}
        self._event_routes: Dict[str, list[MessageRoute]] = {}

    def add_route(
        self,
        predicate: Predicate,
        handler: MessageHandler,
        name: str | None = None,
        *,
        message_type: str | None = None,
        event_types: Iterable[str] | None = None,
    ) -> None:
        """
        添加消息路由

        Args:
            predicate: 路由匹配条件
            handler: 消息处理函数
            name: 路由名称（可选）
            message_type: 消息类型（可选）
            event_types: 事件类型列表（可选）
        """
        with self._lock:
            route = MessageRoute(
                predicate=predicate,
                handler=handler,
                name=name,
                message_type=message_type,
                event_types=set(event_types) if event_types is not None else None,
            )
            self._routes.append(route)
            if message_type:
                self._type_routes.setdefault(message_type, []).append(route)
            if route.event_types:
                for et in route.event_types:
                    self._event_routes.setdefault(et, []).append(route)

    def route(self, predicate: Predicate, name: str | None = None) -> Callable[[MessageHandler], MessageHandler]:
        """装饰器写法，便于在核心逻辑中声明式注册。"""

        def decorator(func: MessageHandler) -> MessageHandler:
            self.add_route(predicate, func, name=name)
            return func

        return decorator

    def on_message(
        self,
        *,
        message_type: str | None = None,
        platform: str | None = None,
        predicate: Predicate | None = None,
        name: str | None = None,
    ) -> Callable[[MessageHandler], MessageHandler]:
        """Sugar 装饰器，基于 Seg.type/platform 及可选额外谓词匹配。"""

        async def combined_predicate(message: MessageEnvelope) -> bool:
            if message_type is not None and _extract_segment_type(message) != message_type:
                return False
            if platform is not None:
                info_platform = message.get("message_info", {}).get("platform")
                if message.get("platform") not in (None, platform) and info_platform is None:
                    return False
                if info_platform not in (None, platform):
                    return False
            if predicate is None:
                return True
            return await _invoke_callable(predicate, message, prefer_thread=False)

        def decorator(func: MessageHandler) -> MessageHandler:
            self.add_route(combined_predicate, func, name=name, message_type=message_type)
            return func

        return decorator

    def on_event(
        self,
        event_type: str | Iterable[str],
        *,
        name: str | None = None,
    ) -> Callable[[MessageHandler], MessageHandler]:
        """装饰器，基于 message 或 message_info.additional_config 中的 event_type 匹配。"""

        allowed = {event_type} if isinstance(event_type, str) else set(event_type)

        async def predicate(message: MessageEnvelope) -> bool:
            current = (
                message.get("event_type")
                or message.get("message_info", {})
                .get("additional_config", {})
                .get("event_type")
            )
            return current in allowed

        def decorator(func: MessageHandler) -> MessageHandler:
            self.add_route(predicate, func, name=name, event_types=allowed)
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

    def register_middleware(self, middleware: Middleware) -> None:
        """注册洋葱模型中间件，围绕处理器执行。"""

        self._middlewares.append(middleware)

    async def handle_message(self, message: MessageEnvelope) -> MessageEnvelope | None:
        await self._run_hooks(self._before_hooks, message)
        try:
            route = await self._match_route(message)
            if route is None:
                return None
            handler = self._wrap_with_middlewares(route.handler)
            result = await handler(message)
        except Exception as exc:
            await self._run_error_hooks(message, exc)
            raise MessageProcessingError(message, exc) from exc
        await self._run_hooks(self._after_hooks, message)
        return result

    async def handle_batch(self, messages: Iterable[MessageEnvelope]) -> List[MessageEnvelope]:
        batch = list(messages)
        if not batch:
            return []
        if self._batch_handler is not None:
            result = await _invoke_callable(self._batch_handler, batch, prefer_thread=True)
            return result or []
        responses: list[MessageEnvelope] = []
        for message in batch:
            response = await self.handle_message(message)
            if response is not None:
                responses.append(response)
        return responses

    async def _match_route(self, message: MessageEnvelope) -> MessageRoute | None:
        candidates: list[MessageRoute] = []
        message_type = _extract_segment_type(message)
        event_type = (
            message.get("event_type")
            or message.get("message_info", {})
            .get("additional_config", {})
            .get("event_type")
        )
        with self._lock:
            if event_type and event_type in self._event_routes:
                candidates.extend(self._event_routes[event_type])
            if message_type and message_type in self._type_routes:
                candidates.extend(self._type_routes[message_type])
            candidates.extend(self._routes)

        seen: set[int] = set()
        for route in candidates:
            rid = id(route)
            if rid in seen:
                continue
            seen.add(rid)
            should_handle = await _invoke_callable(route.predicate, message, prefer_thread=False)
            if should_handle:
                return route
        return None

    async def _run_hooks(self, hooks: Iterable[Hook], message: MessageEnvelope) -> None:
        coro_list = [self._call_hook(hook, message) for hook in hooks]
        if coro_list:
            await asyncio.gather(*coro_list)

    async def _call_hook(self, hook: Hook, message: MessageEnvelope) -> None:
        await _invoke_callable(hook, message, prefer_thread=True)

    async def _run_error_hooks(self, message: MessageEnvelope, exc: BaseException) -> None:
        coros = [self._call_error_hook(hook, message, exc) for hook in self._error_hooks]
        if coros:
            await asyncio.gather(*coros)

    async def _call_error_hook(self, hook: ErrorHook, message: MessageEnvelope, exc: BaseException) -> None:
        await _invoke_callable(hook, message, exc, prefer_thread=True)

    def _wrap_with_middlewares(self, handler: MessageHandler) -> MiddlewareCallable:
        async def base_handler(message: MessageEnvelope) -> MessageEnvelope | None:
            return await _invoke_callable(handler, message, prefer_thread=True)

        wrapped: MiddlewareCallable = base_handler
        for middleware in reversed(self._middlewares):
            current = wrapped

            async def wrapper(msg: MessageEnvelope, mw=middleware, nxt=current) -> MessageEnvelope | None:
                return await _invoke_callable(mw, msg, nxt, prefer_thread=False)

            wrapped = wrapper
        return wrapped


async def _invoke_callable(func: Callable[..., object], *args, prefer_thread: bool = False):
    """支持 sync/async 调用，并可选择在线程中执行。"""
    if inspect.iscoroutinefunction(func):
        return await func(*args)
    if prefer_thread:
        result = await asyncio.to_thread(func, *args)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            return await result
        return result
    result = func(*args)
    if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
        return await result
    return result


def _extract_segment_type(message: MessageEnvelope) -> str | None:
    seg = message.get("message_segment") or message.get("message_chain")
    if isinstance(seg, dict):
        return seg.get("type")
    if isinstance(seg, list) and seg:
        first = seg[0]
        if isinstance(first, dict):
            return first.get("type")
    return None


__all__ = [
    "BatchHandler",
    "Hook",
    "MessageHandler",
    "MessageProcessingError",
    "MessageRoute",
    "MessageRuntime",
    "Middleware",
    "Predicate",
]
