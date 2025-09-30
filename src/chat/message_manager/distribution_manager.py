"""
流循环管理器
为每个聊天流创建独立的无限循环任务，主动轮询处理消息
"""

import asyncio
import time
from typing import Dict, Optional, Any

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.energy_system import energy_manager
from src.chat.chatter_manager import ChatterManager
from src.plugin_system.apis.chat_api import get_chat_manager

logger = get_logger("stream_loop_manager")


class StreamLoopManager:
    """流循环管理器 - 每个流一个独立的无限循环任务"""

    def __init__(self, max_concurrent_streams: Optional[int] = None):
        # 流循环任务管理
        self.stream_loops: Dict[str, asyncio.Task] = {}
        self.loop_lock = asyncio.Lock()

        # 统计信息
        self.stats: Dict[str, Any] = {
            "active_streams": 0,
            "total_loops": 0,
            "total_process_cycles": 0,
            "total_failures": 0,
            "start_time": time.time(),
        }

        # 配置参数
        self.max_concurrent_streams = max_concurrent_streams or getattr(
            global_config.chat, "max_concurrent_distributions", 10
        )

        # Chatter管理器
        self.chatter_manager: Optional[ChatterManager] = None

        # 状态控制
        self.is_running = False

        logger.info(f"流循环管理器初始化完成 (最大并发流数: {self.max_concurrent_streams})")

    async def start(self) -> None:
        """启动流循环管理器"""
        if self.is_running:
            logger.warning("流循环管理器已经在运行")
            return

        self.is_running = True
        logger.info("流循环管理器已启动")

    async def stop(self) -> None:
        """停止流循环管理器"""
        if not self.is_running:
            return

        self.is_running = False

        # 取消所有流循环
        async with self.loop_lock:
            for task in list(self.stream_loops.values()):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            self.stream_loops.clear()

        logger.info("流循环管理器已停止")

    async def start_stream_loop(self, stream_id: str) -> bool:
        """启动指定流的循环任务

        Args:
            stream_id: 流ID

        Returns:
            bool: 是否成功启动
        """
        async with self.loop_lock:
            # 检查是否已有循环在运行
            if stream_id in self.stream_loops:
                logger.debug(f"流 {stream_id} 循环已在运行")
                return True

            # 检查是否超过最大并发限制
            if len(self.stream_loops) >= self.max_concurrent_streams:
                logger.warning(f"超过最大并发流数限制，无法启动流 {stream_id}")
                return False

            # 创建流循环任务
            task = asyncio.create_task(self._stream_loop(stream_id))
            self.stream_loops[stream_id] = task
            self.stats["total_loops"] += 1

            logger.info(f"启动流循环: {stream_id}")
            return True

    async def stop_stream_loop(self, stream_id: str) -> bool:
        """停止指定流的循环任务

        Args:
            stream_id: 流ID

        Returns:
            bool: 是否成功停止
        """
        async with self.loop_lock:
            if stream_id in self.stream_loops:
                task = self.stream_loops[stream_id]
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                del self.stream_loops[stream_id]
                logger.info(f"停止流循环: {stream_id}")
                return True
            return False

    async def _stream_loop(self, stream_id: str) -> None:
        """单个流的无限循环

        Args:
            stream_id: 流ID
        """
        logger.info(f"流循环开始: {stream_id}")

        try:
            while self.is_running:
                try:
                    # 1. 获取流上下文
                    context = await self._get_stream_context(stream_id)
                    if not context:
                        logger.warning(f"无法获取流上下文: {stream_id}")
                        await asyncio.sleep(10.0)
                        continue

                    # 2. 检查是否有消息需要处理
                    has_messages = await self._has_messages_to_process(context)

                    if has_messages:
                        # 3. 激活chatter处理
                        success = await self._process_stream_messages(stream_id, context)

                        # 更新统计
                        self.stats["total_process_cycles"] += 1
                        if success:
                            logger.debug(f"流处理成功: {stream_id}")
                        else:
                            self.stats["total_failures"] += 1
                            logger.warning(f"流处理失败: {stream_id}")

                    # 4. 计算下次检查间隔
                    interval = await self._calculate_interval(stream_id, has_messages)

                    # 5. sleep等待下次检查
                    logger.info(f"流 {stream_id} 等待 {interval:.2f}s")
                    await asyncio.sleep(interval)

                except asyncio.CancelledError:
                    logger.info(f"流循环被取消: {stream_id}")
                    break
                except Exception as e:
                    logger.error(f"流循环出错 {stream_id}: {e}", exc_info=True)
                    self.stats["total_failures"] += 1
                    await asyncio.sleep(5.0)  # 错误时等待5秒再重试

        finally:
            # 清理循环标记
            async with self.loop_lock:
                if stream_id in self.stream_loops:
                    del self.stream_loops[stream_id]

            logger.info(f"流循环结束: {stream_id}")

    async def _get_stream_context(self, stream_id: str) -> Optional[Any]:
        """获取流上下文

        Args:
            stream_id: 流ID

        Returns:
            Optional[Any]: 流上下文，如果不存在返回None
        """
        try:
            chat_manager = get_chat_manager()
            chat_stream = chat_manager.get_stream(stream_id)
            if chat_stream:
                return chat_stream.context_manager.context
            return None
        except Exception as e:
            logger.error(f"获取流上下文失败 {stream_id}: {e}")
            return None

    async def _has_messages_to_process(self, context: Any) -> bool:
        """检查是否有消息需要处理

        Args:
            context: 流上下文

        Returns:
            bool: 是否有未读消息
        """
        try:
            # 检查是否有未读消息
            if hasattr(context, "unread_messages") and context.unread_messages:
                return True

            # 检查其他需要处理的条件
            if hasattr(context, "has_pending_messages") and context.has_pending_messages:
                return True

            return False
        except Exception as e:
            logger.error(f"检查消息状态失败: {e}")
            return False

    async def _process_stream_messages(self, stream_id: str, context: Any) -> bool:
        """处理流消息

        Args:
            stream_id: 流ID
            context: 流上下文

        Returns:
            bool: 是否处理成功
        """
        if not self.chatter_manager:
            logger.warning(f"Chatter管理器未设置: {stream_id}")
            return False

        try:
            start_time = time.time()

            # 直接调用chatter_manager处理流上下文
            results = await self.chatter_manager.process_stream_context(stream_id, context)
            success = results.get("success", False)

            if success:
                await self._refresh_focus_energy(stream_id)
                process_time = time.time() - start_time
                logger.debug(f"流处理成功: {stream_id} (耗时: {process_time:.2f}s)")
            else:
                logger.warning(f"流处理失败: {stream_id} - {results.get('error_message', '未知错误')}")

            return success

        except Exception as e:
            logger.error(f"流处理异常: {stream_id} - {e}", exc_info=True)
            return False

    async def _calculate_interval(self, stream_id: str, has_messages: bool) -> float:
        """计算下次检查间隔

        Args:
            stream_id: 流ID
            has_messages: 本次是否有消息处理

        Returns:
            float: 间隔时间（秒）
        """
        # 基础间隔
        base_interval = getattr(global_config.chat, "distribution_interval", 5.0)

        # 如果没有消息，使用更长的间隔
        if not has_messages:
            return base_interval * 2.0  # 无消息时间隔加倍

        # 尝试使用能量管理器计算间隔
        try:
            # 获取当前focus_energy
            focus_energy = energy_manager.energy_cache.get(stream_id, (0.5, 0))[0]

            # 使用能量管理器计算间隔
            interval = energy_manager.get_distribution_interval(focus_energy)

            logger.debug(f"流 {stream_id} 动态间隔: {interval:.2f}s (能量: {focus_energy:.3f})")
            return interval

        except Exception as e:
            logger.debug(f"流 {stream_id} 使用默认间隔: {base_interval:.2f}s ({e})")
            return base_interval

    def get_queue_status(self) -> Dict[str, Any]:
        """获取队列状态

        Returns:
            Dict[str, Any]: 队列状态信息
        """
        current_time = time.time()
        uptime = current_time - self.stats["start_time"] if self.is_running else 0

        return {
            "active_streams": len(self.stream_loops),
            "total_loops": self.stats["total_loops"],
            "max_concurrent": self.max_concurrent_streams,
            "is_running": self.is_running,
            "uptime": uptime,
            "total_process_cycles": self.stats["total_process_cycles"],
            "total_failures": self.stats["total_failures"],
            "stats": self.stats.copy(),
        }

    def set_chatter_manager(self, chatter_manager: ChatterManager) -> None:
        """设置chatter管理器

        Args:
            chatter_manager: chatter管理器实例
        """
        self.chatter_manager = chatter_manager
        logger.info(f"设置chatter管理器: {chatter_manager.__class__.__name__}")

    def get_performance_summary(self) -> Dict[str, Any]:
        """获取性能摘要

        Returns:
            Dict[str, Any]: 性能摘要
        """
        current_time = time.time()
        uptime = current_time - self.stats["start_time"]

        # 计算吞吐量
        throughput = self.stats["total_process_cycles"] / max(1, uptime / 3600)  # 每小时处理次数

        return {
            "uptime_hours": uptime / 3600,
            "active_streams": len(self.stream_loops),
            "total_process_cycles": self.stats["total_process_cycles"],
            "total_failures": self.stats["total_failures"],
            "throughput_per_hour": throughput,
            "max_concurrent_streams": self.max_concurrent_streams,
        }

    async def _refresh_focus_energy(self, stream_id: str) -> None:
        """分发完成后基于历史消息刷新能量值"""
        try:
            chat_manager = get_chat_manager()
            chat_stream = chat_manager.get_stream(stream_id)
            if not chat_stream:
                logger.debug(f"刷新能量时未找到聊天流: {stream_id}")
                return

            await chat_stream.context_manager.refresh_focus_energy_from_history()
            logger.debug(f"已刷新聊天流 {stream_id} 的聚焦能量")
        except Exception as e:
            logger.warning(f"刷新聊天流 {stream_id} 能量失败: {e}")


# 全局流循环管理器实例
stream_loop_manager = StreamLoopManager()
