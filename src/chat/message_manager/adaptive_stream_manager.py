"""
自适应流管理器 - 动态并发限制和异步流池管理
根据系统负载和流优先级动态调整并发限制
"""

import asyncio
import psutil
import time
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from enum import Enum

from src.common.logger import get_logger
from src.chat.message_receive.chat_stream import ChatStream

logger = get_logger("adaptive_stream_manager")


class StreamPriority(Enum):
    """流优先级"""
    LOW = 1
    NORMAL = 2
    HIGH = 3
    CRITICAL = 4


@dataclass
class SystemMetrics:
    """系统指标"""
    cpu_usage: float = 0.0
    memory_usage: float = 0.0
    active_coroutines: int = 0
    event_loop_lag: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class StreamMetrics:
    """流指标"""
    stream_id: str
    priority: StreamPriority
    message_rate: float = 0.0  # 消息速率（消息/分钟）
    response_time: float = 0.0  # 平均响应时间
    last_activity: float = field(default_factory=time.time)
    consecutive_failures: int = 0
    is_active: bool = True


class AdaptiveStreamManager:
    """自适应流管理器"""

    def __init__(
        self,
        base_concurrent_limit: int = 50,
        max_concurrent_limit: int = 200,
        min_concurrent_limit: int = 10,
        metrics_window: float = 60.0,  # 指标窗口时间
        adjustment_interval: float = 30.0,  # 调整间隔
        cpu_threshold_high: float = 0.8,  # CPU高负载阈值
        cpu_threshold_low: float = 0.3,   # CPU低负载阈值
        memory_threshold_high: float = 0.85,  # 内存高负载阈值
    ):
        self.base_concurrent_limit = base_concurrent_limit
        self.max_concurrent_limit = max_concurrent_limit
        self.min_concurrent_limit = min_concurrent_limit
        self.metrics_window = metrics_window
        self.adjustment_interval = adjustment_interval
        self.cpu_threshold_high = cpu_threshold_high
        self.cpu_threshold_low = cpu_threshold_low
        self.memory_threshold_high = memory_threshold_high

        # 当前状态
        self.current_limit = base_concurrent_limit
        self.active_streams: Set[str] = set()
        self.pending_streams: Set[str] = set()
        self.stream_metrics: Dict[str, StreamMetrics] = {}

        # 异步信号量
        self.semaphore = asyncio.Semaphore(base_concurrent_limit)
        self.priority_semaphore = asyncio.Semaphore(5)  # 高优先级专用信号量

        # 系统监控
        self.system_metrics: List[SystemMetrics] = []
        self.last_adjustment_time = 0.0

        # 统计信息
        self.stats = {
            "total_requests": 0,
            "accepted_requests": 0,
            "rejected_requests": 0,
            "priority_accepts": 0,
            "limit_adjustments": 0,
            "avg_concurrent_streams": 0,
            "peak_concurrent_streams": 0,
        }

        # 监控任务
        self.monitor_task: Optional[asyncio.Task] = None
        self.adjustment_task: Optional[asyncio.Task] = None
        self.is_running = False

        logger.info(f"自适应流管理器初始化完成 (base_limit={base_concurrent_limit}, max_limit={max_concurrent_limit})")

    async def start(self):
        """启动自适应管理器"""
        if self.is_running:
            logger.warning("自适应流管理器已经在运行")
            return

        self.is_running = True
        self.monitor_task = asyncio.create_task(self._system_monitor_loop(), name="system_monitor")
        self.adjustment_task = asyncio.create_task(self._adjustment_loop(), name="limit_adjustment")

    async def stop(self):
        """停止自适应管理器"""
        if not self.is_running:
            return

        self.is_running = False

        # 停止监控任务
        if self.monitor_task and not self.monitor_task.done():
            self.monitor_task.cancel()
            try:
                await asyncio.wait_for(self.monitor_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("系统监控任务停止超时")
            except Exception as e:
                logger.error(f"停止系统监控任务时出错: {e}")

        if self.adjustment_task and not self.adjustment_task.done():
            self.adjustment_task.cancel()
            try:
                await asyncio.wait_for(self.adjustment_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("限制调整任务停止超时")
            except Exception as e:
                logger.error(f"停止限制调整任务时出错: {e}")

        logger.info("自适应流管理器已停止")

    async def acquire_stream_slot(
        self,
        stream_id: str,
        priority: StreamPriority = StreamPriority.NORMAL,
        force: bool = False
    ) -> bool:
        """
        获取流处理槽位

        Args:
            stream_id: 流ID
            priority: 优先级
            force: 是否强制获取（突破限制）

        Returns:
            bool: 是否成功获取槽位
        """
        # 检查管理器是否已启动
        if not self.is_running:
            logger.warning(f"自适应流管理器未运行，直接允许流 {stream_id}")
            return True

        self.stats["total_requests"] += 1
        current_time = time.time()

        # 更新流指标
        if stream_id not in self.stream_metrics:
            self.stream_metrics[stream_id] = StreamMetrics(
                stream_id=stream_id,
                priority=priority
            )
        self.stream_metrics[stream_id].last_activity = current_time

        # 检查是否已经活跃
        if stream_id in self.active_streams:
            logger.debug(f"流 {stream_id} 已经在活跃列表中")
            return True

        # 优先级处理
        if priority in [StreamPriority.HIGH, StreamPriority.CRITICAL]:
            return await self._acquire_priority_slot(stream_id, priority, force)

        # 检查是否需要强制分发（消息积压）
        if not force and self._should_force_dispatch(stream_id):
            force = True
            logger.info(f"流 {stream_id} 消息积压严重，强制分发")

        # 尝试获取常规信号量
        try:
            # 使用wait_for实现非阻塞获取
            acquired = await asyncio.wait_for(self.semaphore.acquire(), timeout=0.001)
            if acquired:
                self.active_streams.add(stream_id)
                self.stats["accepted_requests"] += 1
                logger.debug(f"流 {stream_id} 获取常规槽位成功 (当前活跃: {len(self.active_streams)})")
                return True
        except asyncio.TimeoutError:
            logger.debug(f"常规信号量已满: {stream_id}")
        except Exception as e:
            logger.warning(f"获取常规槽位时出错: {e}")

        # 如果强制分发，尝试突破限制
        if force:
            return await self._force_acquire_slot(stream_id)

        # 无法获取槽位
        self.stats["rejected_requests"] += 1
        logger.debug(f"流 {stream_id} 获取槽位失败，当前限制: {self.current_limit}, 活跃流: {len(self.active_streams)}")
        return False

    async def _acquire_priority_slot(self, stream_id: str, priority: StreamPriority, force: bool) -> bool:
        """获取优先级槽位"""
        try:
            # 优先级信号量有少量槽位
            acquired = await asyncio.wait_for(self.priority_semaphore.acquire(), timeout=0.001)
            if acquired:
                self.active_streams.add(stream_id)
                self.stats["priority_accepts"] += 1
                self.stats["accepted_requests"] += 1
                logger.debug(f"流 {stream_id} 获取优先级槽位成功 (优先级: {priority.name})")
                return True
        except asyncio.TimeoutError:
            logger.debug(f"优先级信号量已满: {stream_id}")
        except Exception as e:
            logger.warning(f"获取优先级槽位时出错: {e}")

        # 如果优先级槽位也满了，检查是否强制
        if force or priority == StreamPriority.CRITICAL:
            return await self._force_acquire_slot(stream_id)

        return False

    async def _force_acquire_slot(self, stream_id: str) -> bool:
        """强制获取槽位（突破限制）"""
        # 检查是否超过最大限制
        if len(self.active_streams) >= self.max_concurrent_limit:
            logger.warning(f"达到最大并发限制 {self.max_concurrent_limit}，无法为流 {stream_id} 强制分发")
            return False

        # 强制添加到活跃列表
        self.active_streams.add(stream_id)
        self.stats["accepted_requests"] += 1
        logger.warning(f"流 {stream_id} 突破并发限制强制分发 (当前活跃: {len(self.active_streams)})")
        return True

    def release_stream_slot(self, stream_id: str):
        """释放流处理槽位"""
        if stream_id in self.active_streams:
            self.active_streams.remove(stream_id)

            # 释放相应的信号量
            metrics = self.stream_metrics.get(stream_id)
            if metrics and metrics.priority in [StreamPriority.HIGH, StreamPriority.CRITICAL]:
                self.priority_semaphore.release()
            else:
                self.semaphore.release()

            logger.debug(f"流 {stream_id} 释放槽位 (当前活跃: {len(self.active_streams)})")

    def _should_force_dispatch(self, stream_id: str) -> bool:
        """判断是否应该强制分发"""
        # 这里可以实现基于消息积压的判断逻辑
        # 简化版本：基于流的历史活跃度和优先级
        metrics = self.stream_metrics.get(stream_id)
        if not metrics:
            return False

        # 如果是高优先级流，更容易强制分发
        if metrics.priority == StreamPriority.HIGH:
            return True

        # 如果最近有活跃且响应时间较长，可能需要强制分发
        current_time = time.time()
        if (current_time - metrics.last_activity < 300 and  # 5分钟内有活动
            metrics.response_time > 5.0):  # 响应时间超过5秒
            return True

        return False

    async def _system_monitor_loop(self):
        """系统监控循环"""
        logger.info("系统监控循环启动")

        while self.is_running:
            try:
                await asyncio.sleep(5.0)  # 每5秒监控一次
                await self._collect_system_metrics()
            except asyncio.CancelledError:
                logger.info("系统监控循环被取消")
                break
            except Exception as e:
                logger.error(f"系统监控出错: {e}")

        logger.info("系统监控循环结束")

    async def _collect_system_metrics(self):
        """收集系统指标"""
        try:
            # CPU使用率
            cpu_usage = psutil.cpu_percent(interval=None) / 100.0

            # 内存使用率
            memory = psutil.virtual_memory()
            memory_usage = memory.percent / 100.0

            # 活跃协程数量
            try:
                active_coroutines = len(asyncio.all_tasks())
            except:
                active_coroutines = 0

            # 事件循环延迟
            event_loop_lag = 0.0
            try:
                loop = asyncio.get_running_loop()
                start_time = time.time()
                await asyncio.sleep(0)
                event_loop_lag = time.time() - start_time
            except:
                pass

            metrics = SystemMetrics(
                cpu_usage=cpu_usage,
                memory_usage=memory_usage,
                active_coroutines=active_coroutines,
                event_loop_lag=event_loop_lag,
                timestamp=time.time()
            )

            self.system_metrics.append(metrics)

            # 保持指标窗口大小
            cutoff_time = time.time() - self.metrics_window
            self.system_metrics = [
                m for m in self.system_metrics
                if m.timestamp > cutoff_time
            ]

            # 更新统计信息
            self.stats["avg_concurrent_streams"] = (
                self.stats["avg_concurrent_streams"] * 0.9 + len(self.active_streams) * 0.1
            )
            self.stats["peak_concurrent_streams"] = max(
                self.stats["peak_concurrent_streams"],
                len(self.active_streams)
            )

        except Exception as e:
            logger.error(f"收集系统指标失败: {e}")

    async def _adjustment_loop(self):
        """限制调整循环"""
        logger.info("限制调整循环启动")

        while self.is_running:
            try:
                await asyncio.sleep(self.adjustment_interval)
                await self._adjust_concurrent_limit()
            except asyncio.CancelledError:
                logger.info("限制调整循环被取消")
                break
            except Exception as e:
                logger.error(f"限制调整出错: {e}")

        logger.info("限制调整循环结束")

    async def _adjust_concurrent_limit(self):
        """调整并发限制"""
        if not self.system_metrics:
            return

        current_time = time.time()
        if current_time - self.last_adjustment_time < self.adjustment_interval:
            return

        # 计算平均系统指标
        recent_metrics = self.system_metrics[-10:] if len(self.system_metrics) >= 10 else self.system_metrics
        if not recent_metrics:
            return

        avg_cpu = sum(m.cpu_usage for m in recent_metrics) / len(recent_metrics)
        avg_memory = sum(m.memory_usage for m in recent_metrics) / len(recent_metrics)
        avg_coroutines = sum(m.active_coroutines for m in recent_metrics) / len(recent_metrics)

        # 调整策略
        old_limit = self.current_limit
        adjustment_factor = 1.0

        # CPU负载调整
        if avg_cpu > self.cpu_threshold_high:
            adjustment_factor *= 0.8  # 减少20%
        elif avg_cpu < self.cpu_threshold_low:
            adjustment_factor *= 1.2  # 增加20%

        # 内存负载调整
        if avg_memory > self.memory_threshold_high:
            adjustment_factor *= 0.7  # 减少30%

        # 协程数量调整
        if avg_coroutines > 1000:
            adjustment_factor *= 0.9  # 减少10%

        # 应用调整
        new_limit = int(self.current_limit * adjustment_factor)
        new_limit = max(self.min_concurrent_limit, min(self.max_concurrent_limit, new_limit))

        # 检查是否需要调整信号量
        if new_limit != self.current_limit:
            await self._adjust_semaphore(self.current_limit, new_limit)
            self.current_limit = new_limit
            self.stats["limit_adjustments"] += 1
            self.last_adjustment_time = current_time

            logger.info(
                f"并发限制调整: {old_limit} -> {new_limit} "
                f"(CPU: {avg_cpu:.2f}, 内存: {avg_memory:.2f}, 协程: {avg_coroutines:.0f})"
            )

    async def _adjust_semaphore(self, old_limit: int, new_limit: int):
        """调整信号量大小"""
        if new_limit > old_limit:
            # 增加信号量槽位
            for _ in range(new_limit - old_limit):
                self.semaphore.release()
        elif new_limit < old_limit:
            # 减少信号量槽位（通过等待槽位被释放）
            reduction = old_limit - new_limit
            for _ in range(reduction):
                try:
                    await asyncio.wait_for(self.semaphore.acquire(), timeout=0.001)
                except:
                    # 如果无法立即获取，说明当前使用量接近限制
                    break

    def update_stream_metrics(self, stream_id: str, **kwargs):
        """更新流指标"""
        if stream_id not in self.stream_metrics:
            return

        metrics = self.stream_metrics[stream_id]
        for key, value in kwargs.items():
            if hasattr(metrics, key):
                setattr(metrics, key, value)

    def get_stats(self) -> Dict:
        """获取统计信息"""
        stats = self.stats.copy()
        stats.update({
            "current_limit": self.current_limit,
            "active_streams": len(self.active_streams),
            "pending_streams": len(self.pending_streams),
            "is_running": self.is_running,
            "system_cpu": self.system_metrics[-1].cpu_usage if self.system_metrics else 0,
            "system_memory": self.system_metrics[-1].memory_usage if self.system_metrics else 0,
        })

        # 计算接受率
        if stats["total_requests"] > 0:
            stats["acceptance_rate"] = stats["accepted_requests"] / stats["total_requests"]
        else:
            stats["acceptance_rate"] = 0

        return stats


# 全局自适应管理器实例
_adaptive_manager: Optional[AdaptiveStreamManager] = None


def get_adaptive_stream_manager() -> AdaptiveStreamManager:
    """获取自适应流管理器实例"""
    global _adaptive_manager
    if _adaptive_manager is None:
        _adaptive_manager = AdaptiveStreamManager()
    return _adaptive_manager


async def init_adaptive_stream_manager():
    """初始化自适应流管理器"""
    manager = get_adaptive_stream_manager()
    await manager.start()


async def shutdown_adaptive_stream_manager():
    """关闭自适应流管理器"""
    manager = get_adaptive_stream_manager()
    await manager.stop()