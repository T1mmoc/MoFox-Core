import asyncio
from typing import Any

from src.common.logger import get_logger
from src.plugin_system.base.base_events_handler import BaseEventHandler

logger = get_logger("base_event")


class HandlerResult:
    """事件处理器执行结果

    所有事件处理器必须返回此类的实例
    """

    def __init__(self, success: bool, continue_process: bool, message: Any = None, handler_name: str = ""):
        self.success = success
        self.continue_process = continue_process
        self.message = message
        self.handler_name = handler_name

    def __repr__(self):
        return f"HandlerResult(success={self.success}, continue_process={self.continue_process}, message='{self.message}', handler_name='{self.handler_name}')"


class HandlerResultsCollection:
    """HandlerResult集合，提供便捷的查询方法"""

    def __init__(self, results: list[HandlerResult]):
        self.results = results

    def all_continue_process(self) -> bool:
        """检查是否所有handler的continue_process都为True"""
        return all(result.continue_process for result in self.results)

    def get_all_results(self) -> list[HandlerResult]:
        """获取所有HandlerResult"""
        return self.results

    def get_failed_handlers(self) -> list[HandlerResult]:
        """获取执行失败的handler结果"""
        return [result for result in self.results if not result.success]

    def get_stopped_handlers(self) -> list[HandlerResult]:
        """获取continue_process为False的handler结果"""
        return [result for result in self.results if not result.continue_process]

    def get_message_result(self) -> Any:
        """获取handler的message

        当只有一个handler的结果时，直接返回那个handler结果中的message字段
        否则用字典的形式{handler_name:message}返回
        """
        if len(self.results) == 0:
            return {}
        elif len(self.results) == 1:
            return self.results[0].message
        else:
            return {result.handler_name: result.message for result in self.results}

    def get_handler_result(self, handler_name: str) -> HandlerResult | None:
        """获取指定handler的结果"""
        for result in self.results:
            if result.handler_name == handler_name:
                return result
        return None

    def get_success_count(self) -> int:
        """获取成功执行的handler数量"""
        return sum(1 for result in self.results if result.success)

    def get_failure_count(self) -> int:
        """获取执行失败的handler数量"""
        return sum(1 for result in self.results if not result.success)

    def get_summary(self) -> dict[str, Any]:
        """获取执行摘要"""
        return {
            "total_handlers": len(self.results),
            "success_count": self.get_success_count(),
            "failure_count": self.get_failure_count(),
            "continue_process": self.all_continue_process(),
            "failed_handlers": [r.handler_name for r in self.get_failed_handlers()],
            "stopped_handlers": [r.handler_name for r in self.get_stopped_handlers()],
        }


class BaseEvent:
    def __init__(
        self, name: str, allowed_subscribers: list[str] | None = None, allowed_triggers: list[str] | None = None
    ):
        self.name = name
        self.enabled = True
        self.allowed_subscribers = allowed_subscribers  # 记录事件处理器名
        self.allowed_triggers = allowed_triggers  # 记录插件名

        self.subscribers: list["BaseEventHandler"] = []  # 订阅该事件的事件处理器列表

        self.event_handle_lock = asyncio.Lock()

    def __name__(self):
        return self.name

    async def activate(
        self, params: dict, handler_timeout: float | None = None, max_concurrency: int | None = None
    ) -> HandlerResultsCollection:
        """激活事件，执行所有订阅的处理器

        Args:
            params: 传递给处理器的参数

        Returns:
            HandlerResultsCollection: 所有处理器的执行结果集合
        """
        if not self.enabled:
            return HandlerResultsCollection([])

        # 移除全局锁，允许同一事件并发触发
        # async with self.event_handle_lock:
        sorted_subscribers = sorted(
            self.subscribers, key=lambda h: h.weight if hasattr(h, "weight") and h.weight != -1 else 0, reverse=True
        )

        if not sorted_subscribers:
            return HandlerResultsCollection([])

        concurrency_limit = None
        if max_concurrency is not None:
            concurrency_limit = max_concurrency if max_concurrency > 0 else None
            if concurrency_limit:
                concurrency_limit = min(concurrency_limit, len(sorted_subscribers))

        semaphore = (
            asyncio.Semaphore(concurrency_limit)
            if concurrency_limit and concurrency_limit < len(sorted_subscribers)
            else None
        )

        async def _run_handler(subscriber):
            handler_name = (
                subscriber.handler_name if hasattr(subscriber, "handler_name") else subscriber.__class__.__name__
            )

            async def _invoke():
                return await self._execute_subscriber(subscriber, params)

            try:
                if handler_timeout and handler_timeout > 0:
                    result = await asyncio.wait_for(_invoke(), timeout=handler_timeout)
                else:
                    result = await _invoke()
            except asyncio.TimeoutError:
                logger.warning(f"事件处理器 {handler_name} 执行超时 ({handler_timeout}s)")
                return HandlerResult(False, True, f"timeout after {handler_timeout}s", handler_name)
            except Exception as exc:
                logger.error(f"事件处理器 {handler_name} 执行失败: {exc}")
                return HandlerResult(False, True, str(exc), handler_name)

            if not isinstance(result, HandlerResult):
                return HandlerResult(True, True, result, handler_name)

            if not result.handler_name:
                result.handler_name = handler_name
            return result

        async def _guarded_run(subscriber):
            if semaphore:
                async with semaphore:
                    return await _run_handler(subscriber)
            return await _run_handler(subscriber)

        tasks = [asyncio.create_task(_guarded_run(subscriber)) for subscriber in sorted_subscribers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        processed_results: list[HandlerResult] = []
        for subscriber, result in zip(sorted_subscribers, results):
            handler_name = (
                subscriber.handler_name if hasattr(subscriber, "handler_name") else subscriber.__class__.__name__
            )
            if isinstance(result, Exception):
                logger.error(f"事件处理器 {handler_name} 执行失败: {result}")
                processed_results.append(HandlerResult(False, True, str(result), handler_name))
            else:
                processed_results.append(result)

        return HandlerResultsCollection(processed_results)

    @staticmethod
    async def _execute_subscriber(subscriber, params: dict) -> HandlerResult:
        """执行单个订阅者处理器"""
        try:
            return await subscriber.execute(params)
        except Exception as e:
            # 异常会在 gather 中捕获，这里直接抛出让 gather 处理
            raise e
