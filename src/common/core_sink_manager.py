"""
CoreSink 统一管理器

负责管理 InProcessCoreSink 和 ProcessCoreSink 双实例，
提供统一的消息收发接口，自动维护与适配器子进程的通信管道。

核心职责：
1. 创建和管理 InProcessCoreSink（进程内消息）和 ProcessCoreSink（跨进程消息）
2. 自动维护 ProcessCoreSink 与子进程的通信管道
3. 使用 MessageRuntime 进行消息路由和处理
4. 提供统一的消息发送接口

架构说明（2025-11 重构）：
- 集成 mofox_wire.MessageRuntime 作为消息路由中心
- 使用 @runtime.on_message() 装饰器注册消息处理器
- 利用 before_hook/after_hook/error_hook 处理前置/后置/错误逻辑
- 简化消息处理链条，提高可扩展性
"""

from __future__ import annotations

import asyncio
import multiprocessing as mp
from collections.abc import Awaitable, Callable
from typing import Any

from mofox_wire import (
    InProcessCoreSink,
    MessageEnvelope,
    MessageRuntime,
    ProcessCoreSinkServer,
)

from src.common.logger import get_logger

logger = get_logger("core_sink_manager")


# 消息处理器类型
MessageHandlerCallback = Callable[[MessageEnvelope], Awaitable[None]]


class CoreSinkManager:
    """
    CoreSink 统一管理器

    管理 InProcessCoreSink 和 ProcessCoreSinkServer 双实例，
    集成 MessageRuntime 提供统一的消息路由和收发接口。

    架构说明：
    - InProcessCoreSink: 用于同进程内的适配器（run_in_subprocess=False）
    - ProcessCoreSinkServer: 用于管理与子进程适配器的通信
    - MessageRuntime: 统一消息路由，支持 @on_message 装饰器和钩子机制

    消息流向：
    1. 适配器（同进程）→ InProcessCoreSink → MessageRuntime.handle_message() → 注册的处理器
    2. 适配器（子进程）→ ProcessCoreSinkServer → MessageRuntime.handle_message() → 注册的处理器
    3. 核心回复 → CoreSinkManager.send_outgoing → 适配器

    使用 MessageRuntime 的优势：
    - 支持 @runtime.on_message(message_type="xxx") 按消息类型路由
    - 支持 before_hook/after_hook/error_hook 统一处理流程
    - 支持中间件机制（洋葱模型）
    - 自动处理同步/异步处理器
    """

    def __init__(self):
        # MessageRuntime 实例
        self._runtime: MessageRuntime = MessageRuntime()

        # InProcessCoreSink 实例（用于同进程适配器）
        self._in_process_sink: InProcessCoreSink | None = None

        # 子进程通信管理
        # key: adapter_name, value: (ProcessCoreSinkServer, incoming_queue, outgoing_queue)
        self._process_sinks: dict[str, tuple[ProcessCoreSinkServer, mp.Queue, mp.Queue]] = {}

        # multiprocessing context
        self._mp_ctx = mp.get_context("spawn")

        # 运行状态
        self._running = False
        self._initialized = False

        # 后台任务集合（防止任务被垃圾回收）
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def runtime(self) -> MessageRuntime:
        """
        获取 MessageRuntime 实例

        外部模块可以通过此属性注册消息处理器、钩子等：

        ```python
        manager = get_core_sink_manager()

        # 注册消息处理器
        @manager.runtime.on_message(message_type="text")
        async def handle_text(envelope: MessageEnvelope):
            ...

        # 注册前置钩子
        manager.runtime.register_before_hook(my_before_hook)
        ```

        Returns:
            MessageRuntime 实例
        """
        return self._runtime

    async def initialize(self) -> None:
        """
        初始化 CoreSink 管理器

        创建 InProcessCoreSink，将收到的消息交给 MessageRuntime 处理。
        """
        if self._initialized:
            logger.warning("CoreSinkManager 已经初始化，跳过重复初始化")
            return

        logger.info("正在初始化 CoreSink 管理器...")

        # 创建 InProcessCoreSink，使用 MessageRuntime 作为消息处理入口
        self._in_process_sink = InProcessCoreSink(self._dispatch_to_runtime)

        self._running = True
        self._initialized = True

        logger.info("CoreSink 管理器初始化完成（已集成 MessageRuntime）")

    async def shutdown(self) -> None:
        """关闭 CoreSink 管理器"""
        if not self._running:
            return

        logger.info("正在关闭 CoreSink 管理器...")
        self._running = False

        # 关闭所有 ProcessCoreSinkServer
        for adapter_name, (server, _, _) in list(self._process_sinks.items()):
            try:
                await server.close()
                logger.info(f"已关闭适配器 {adapter_name} 的 ProcessCoreSinkServer")
            except Exception as e:
                logger.error(f"关闭适配器 {adapter_name} 的 ProcessCoreSinkServer 时出错: {e}")

        self._process_sinks.clear()

        # 关闭 InProcessCoreSink
        if self._in_process_sink:
            await self._in_process_sink.close()
            self._in_process_sink = None

        self._initialized = False
        logger.info("CoreSink 管理器已关闭")

    def get_in_process_sink(self) -> InProcessCoreSink:
        """
        获取 InProcessCoreSink 实例

        用于同进程运行的适配器

        Returns:
            InProcessCoreSink 实例

        Raises:
            RuntimeError: 如果管理器未初始化
        """
        if self._in_process_sink is None:
            raise RuntimeError("CoreSinkManager 未初始化，请先调用 initialize()")
        return self._in_process_sink

    def create_process_sink_queues(self, adapter_name: str) -> tuple[mp.Queue, mp.Queue]:
        """
        为子进程适配器创建通信队列

        创建 incoming 和 outgoing 队列对，用于与子进程适配器通信。
        同时创建 ProcessCoreSinkServer 来处理消息转发。

        Args:
            adapter_name: 适配器名称

        Returns:
            (to_core_queue, from_core_queue) 元组
            - to_core_queue: 子进程发送到核心的队列
            - from_core_queue: 核心发送到子进程的队列

        Raises:
            RuntimeError: 如果管理器未初始化
        """
        if not self._initialized:
            raise RuntimeError("CoreSinkManager 未初始化，请先调用 initialize()")

        if adapter_name in self._process_sinks:
            logger.warning(f"适配器 {adapter_name} 的队列已存在，将被覆盖")
            # 先关闭旧的
            old_server, _, _ = self._process_sinks[adapter_name]
            task = asyncio.create_task(old_server.close())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        # 创建通信队列
        incoming_queue = self._mp_ctx.Queue()  # 子进程 → 核心
        outgoing_queue = self._mp_ctx.Queue()  # 核心 → 子进程

        # 创建 ProcessCoreSinkServer，使用 MessageRuntime 处理消息
        server = ProcessCoreSinkServer(
            incoming_queue=incoming_queue,
            outgoing_queue=outgoing_queue,
            core_handler=self._dispatch_to_runtime,
            name=adapter_name,
        )

        # 启动服务器
        server.start()

        # 存储引用
        self._process_sinks[adapter_name] = (server, incoming_queue, outgoing_queue)

        logger.info(f"为适配器 {adapter_name} 创建了 ProcessCoreSink 通信队列")

        return incoming_queue, outgoing_queue

    def remove_process_sink(self, adapter_name: str) -> None:
        """
        移除子进程适配器的通信队列

        Args:
            adapter_name: 适配器名称
        """
        if adapter_name not in self._process_sinks:
            logger.warning(f"适配器 {adapter_name} 的队列不存在")
            return

        server, _, _ = self._process_sinks.pop(adapter_name)
        task = asyncio.create_task(server.close())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        logger.info(f"已移除适配器 {adapter_name} 的 ProcessCoreSink 通信队列")

    async def send_outgoing(
        self,
        envelope: MessageEnvelope,
        platform: str | None = None,
        adapter_name: str | None = None
    ) -> None:
        """
        发送消息到适配器

        根据 platform 或 adapter_name 路由到正确的适配器。

        Args:
            envelope: 消息信封
            platform: 目标平台（可选）
            adapter_name: 目标适配器名称（可选）

        路由规则：
        1. 如果指定了 adapter_name，直接发送到该适配器
        2. 如果指定了 platform，发送到所有匹配平台的适配器
        3. 如果都没指定，从 envelope 中提取 platform 并广播
        """
        # 从 envelope 中获取 platform
        if platform is None:
            platform = envelope.get("platform") or envelope.get("message_info", {}).get("platform")

        # 发送到 InProcessCoreSink（会自动广播到所有注册的 outgoing handler）
        if self._in_process_sink:
            await self._in_process_sink.push_outgoing(envelope)

        # 发送到所有 ProcessCoreSinkServer
        for name, (server, _, _) in self._process_sinks.items():
            if adapter_name and name != adapter_name:
                continue
            try:
                await server.push_outgoing(envelope)
            except Exception as e:
                logger.error(f"发送消息到适配器 {name} 失败: {e}")

    async def _dispatch_to_runtime(self, envelope: MessageEnvelope) -> None:
        """
        将消息分发给 MessageRuntime 处理

        这是内部方法，由 InProcessCoreSink 和 ProcessCoreSinkServer 调用。
        所有从适配器接收到的消息都会经过这里，然后交给 MessageRuntime 路由。

        Args:
            envelope: 消息信封
        """
        if not self._running:
            logger.warning("CoreSinkManager 未运行，忽略接收到的消息")
            return

        try:
            # 使用 MessageRuntime 处理消息
            await self._runtime.handle_message(envelope)
        except Exception as e:
            logger.error(f"MessageRuntime 处理消息时出错: {e}")


# 全局单例
_core_sink_manager: CoreSinkManager | None = None


def get_core_sink_manager() -> CoreSinkManager:
    """获取 CoreSinkManager 单例"""
    global _core_sink_manager
    if _core_sink_manager is None:
        _core_sink_manager = CoreSinkManager()
    return _core_sink_manager


def get_message_runtime() -> MessageRuntime:
    """
    获取全局 MessageRuntime 实例

    这是获取 MessageRuntime 的推荐方式，用于注册消息处理器、钩子等：

    ```python
    from src.common.core_sink_manager import get_message_runtime

    runtime = get_message_runtime()

    @runtime.on_message(message_type="text")
    async def handle_text(envelope: MessageEnvelope):
        ...
    ```

    Returns:
        MessageRuntime 实例
    """
    return get_core_sink_manager().runtime


async def initialize_core_sink_manager() -> CoreSinkManager:
    """
    初始化 CoreSinkManager 单例

    Returns:
        初始化后的 CoreSinkManager 实例
    """
    manager = get_core_sink_manager()
    await manager.initialize()
    return manager


async def shutdown_core_sink_manager() -> None:
    """关闭 CoreSinkManager 单例"""
    global _core_sink_manager
    if _core_sink_manager:
        await _core_sink_manager.shutdown()
        _core_sink_manager = None


# ============================================================================
# 向后兼容的 API
# ============================================================================

def get_core_sink() -> InProcessCoreSink:
    """
    获取 InProcessCoreSink 实例（向后兼容）

    这是旧版 API，推荐使用 get_core_sink_manager().get_in_process_sink()

    Returns:
        InProcessCoreSink 实例
    """
    return get_core_sink_manager().get_in_process_sink()


def set_core_sink(sink: Any) -> None:
    """
    设置 CoreSink（向后兼容，现已弃用）

    新架构中 CoreSink 由 CoreSinkManager 统一管理，不再支持外部设置。
    此函数保留仅为兼容旧代码，调用会记录警告日志。
    """
    logger.warning(
        "set_core_sink() 已弃用，CoreSink 现由 CoreSinkManager 统一管理。"
        "请使用 initialize_core_sink_manager() 初始化。"
    )


async def push_outgoing(envelope: MessageEnvelope) -> None:
    """
    将消息推送到所有适配器（向后兼容）

    Args:
        envelope: 消息信封
    """
    manager = get_core_sink_manager()
    await manager.send_outgoing(envelope)


__all__ = [
    "CoreSinkManager",
    # 向后兼容
    "get_core_sink",
    "get_core_sink_manager",
    "get_message_runtime",
    "initialize_core_sink_manager",
    "push_outgoing",
    "set_core_sink",
    "shutdown_core_sink_manager",
]
