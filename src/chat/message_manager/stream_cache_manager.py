"""
流缓存管理器 - 使用优化版聊天流和智能缓存策略
提供分层缓存和自动清理功能
"""

import asyncio
import time
from typing import Dict, List, Optional, Set
from dataclasses import dataclass
from collections import OrderedDict

from maim_message import GroupInfo, UserInfo
from src.common.logger import get_logger
from src.chat.message_receive.optimized_chat_stream import OptimizedChatStream, create_optimized_chat_stream

logger = get_logger("stream_cache_manager")


@dataclass
class StreamCacheStats:
    """缓存统计信息"""
    hot_cache_size: int = 0
    warm_storage_size: int = 0
    cold_storage_size: int = 0
    total_memory_usage: int = 0  # 估算的内存使用（字节）
    cache_hits: int = 0
    cache_misses: int = 0
    evictions: int = 0
    last_cleanup_time: float = 0


class TieredStreamCache:
    """分层流缓存管理器"""

    def __init__(
        self,
        max_hot_size: int = 100,
        max_warm_size: int = 500,
        max_cold_size: int = 2000,
        cleanup_interval: float = 300.0,  # 5分钟清理一次
        hot_timeout: float = 1800.0,      # 30分钟未访问降级到warm
        warm_timeout: float = 7200.0,     # 2小时未访问降级到cold
        cold_timeout: float = 86400.0,    # 24小时未访问删除
    ):
        self.max_hot_size = max_hot_size
        self.max_warm_size = max_warm_size
        self.max_cold_size = max_cold_size
        self.cleanup_interval = cleanup_interval
        self.hot_timeout = hot_timeout
        self.warm_timeout = warm_timeout
        self.cold_timeout = cold_timeout

        # 三层缓存存储
        self.hot_cache: OrderedDict[str, OptimizedChatStream] = OrderedDict()  # 热数据（LRU）
        self.warm_storage: Dict[str, tuple[OptimizedChatStream, float]] = {}   # 温数据（最后访问时间）
        self.cold_storage: Dict[str, tuple[OptimizedChatStream, float]] = {}   # 冷数据（最后访问时间）

        # 统计信息
        self.stats = StreamCacheStats()

        # 清理任务
        self.cleanup_task: Optional[asyncio.Task] = None
        self.is_running = False

        logger.info(f"分层流缓存管理器初始化完成 (hot:{max_hot_size}, warm:{max_warm_size}, cold:{max_cold_size})")

    async def start(self):
        """启动缓存管理器"""
        if self.is_running:
            logger.warning("缓存管理器已经在运行")
            return

        self.is_running = True
        self.cleanup_task = asyncio.create_task(self._cleanup_loop(), name="stream_cache_cleanup")

    async def stop(self):
        """停止缓存管理器"""
        if not self.is_running:
            return

        self.is_running = False

        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await asyncio.wait_for(self.cleanup_task, timeout=10.0)
            except asyncio.TimeoutError:
                logger.warning("缓存清理任务停止超时")
            except Exception as e:
                logger.error(f"停止缓存清理任务时出错: {e}")

        logger.info("分层流缓存管理器已停止")

    async def get_or_create_stream(
        self,
        stream_id: str,
        platform: str,
        user_info: UserInfo,
        group_info: Optional[GroupInfo] = None,
        data: Optional[Dict] = None,
    ) -> OptimizedChatStream:
        """获取或创建流 - 优化版本"""
        current_time = time.time()

        # 1. 检查热缓存
        if stream_id in self.hot_cache:
            stream = self.hot_cache[stream_id]
            # 移动到末尾（LRU更新）
            self.hot_cache.move_to_end(stream_id)
            self.stats.cache_hits += 1
            logger.debug(f"热缓存命中: {stream_id}")
            return stream.create_snapshot()

        # 2. 检查温存储
        if stream_id in self.warm_storage:
            stream, last_access = self.warm_storage[stream_id]
            self.warm_storage[stream_id] = (stream, current_time)
            self.stats.cache_hits += 1
            logger.debug(f"温缓存命中: {stream_id}")
            # 提升到热缓存
            await self._promote_to_hot(stream_id, stream)
            return stream.create_snapshot()

        # 3. 检查冷存储
        if stream_id in self.cold_storage:
            stream, last_access = self.cold_storage[stream_id]
            self.cold_storage[stream_id] = (stream, current_time)
            self.stats.cache_hits += 1
            logger.debug(f"冷缓存命中: {stream_id}")
            # 提升到温缓存
            await self._promote_to_warm(stream_id, stream)
            return stream.create_snapshot()

        # 4. 缓存未命中，创建新流
        self.stats.cache_misses += 1
        stream = create_optimized_chat_stream(
            stream_id=stream_id,
            platform=platform,
            user_info=user_info,
            group_info=group_info,
            data=data
        )
        logger.debug(f"缓存未命中，创建新流: {stream_id}")

        # 添加到热缓存
        await self._add_to_hot(stream_id, stream)

        return stream

    async def _add_to_hot(self, stream_id: str, stream: OptimizedChatStream):
        """添加到热缓存"""
        # 检查是否需要驱逐
        if len(self.hot_cache) >= self.max_hot_size:
            await self._evict_from_hot()

        self.hot_cache[stream_id] = stream
        self.stats.hot_cache_size = len(self.hot_cache)

    async def _promote_to_hot(self, stream_id: str, stream: OptimizedChatStream):
        """提升到热缓存"""
        # 从温存储中移除
        if stream_id in self.warm_storage:
            del self.warm_storage[stream_id]
            self.stats.warm_storage_size = len(self.warm_storage)

        # 添加到热缓存
        await self._add_to_hot(stream_id, stream)
        logger.debug(f"流 {stream_id} 提升到热缓存")

    async def _promote_to_warm(self, stream_id: str, stream: OptimizedChatStream):
        """提升到温缓存"""
        # 从冷存储中移除
        if stream_id in self.cold_storage:
            del self.cold_storage[stream_id]
            self.stats.cold_storage_size = len(self.cold_storage)

        # 添加到温存储
        if len(self.warm_storage) >= self.max_warm_size:
            await self._evict_from_warm()

        current_time = time.time()
        self.warm_storage[stream_id] = (stream, current_time)
        self.stats.warm_storage_size = len(self.warm_storage)
        logger.debug(f"流 {stream_id} 提升到温缓存")

    async def _evict_from_hot(self):
        """从热缓存驱逐最久未使用的流"""
        if not self.hot_cache:
            return

        # LRU驱逐
        stream_id, stream = self.hot_cache.popitem(last=False)
        self.stats.evictions += 1
        logger.debug(f"从热缓存驱逐: {stream_id}")

        # 移动到温存储
        if len(self.warm_storage) < self.max_warm_size:
            current_time = time.time()
            self.warm_storage[stream_id] = (stream, current_time)
            self.stats.warm_storage_size = len(self.warm_storage)
        else:
            # 温存储也满了，直接删除
            logger.debug(f"温存储已满，删除流: {stream_id}")

        self.stats.hot_cache_size = len(self.hot_cache)

    async def _evict_from_warm(self):
        """从温存储驱逐最久未使用的流"""
        if not self.warm_storage:
            return

        # 找到最久未访问的流
        oldest_stream_id = min(self.warm_storage.keys(), key=lambda k: self.warm_storage[k][1])
        stream, last_access = self.warm_storage.pop(oldest_stream_id)
        self.stats.evictions += 1
        logger.debug(f"从温存储驱逐: {oldest_stream_id}")

        # 移动到冷存储
        if len(self.cold_storage) < self.max_cold_size:
            current_time = time.time()
            self.cold_storage[oldest_stream_id] = (stream, current_time)
            self.stats.cold_storage_size = len(self.cold_storage)
        else:
            # 冷存储也满了，直接删除
            logger.debug(f"冷存储已满，删除流: {oldest_stream_id}")

        self.stats.warm_storage_size = len(self.warm_storage)

    async def _cleanup_loop(self):
        """清理循环"""
        logger.info("流缓存清理循环启动")

        while self.is_running:
            try:
                await asyncio.sleep(self.cleanup_interval)
                await self._perform_cleanup()
            except asyncio.CancelledError:
                logger.info("流缓存清理循环被取消")
                break
            except Exception as e:
                logger.error(f"流缓存清理出错: {e}")

        logger.info("流缓存清理循环结束")

    async def _perform_cleanup(self):
        """执行清理操作"""
        current_time = time.time()
        cleanup_stats = {
            "hot_to_warm": 0,
            "warm_to_cold": 0,
            "cold_removed": 0,
        }

        # 1. 检查热缓存超时
        hot_to_demote = []
        for stream_id, stream in self.hot_cache.items():
            # 获取最后访问时间（简化：使用创建时间作为近似）
            last_access = getattr(stream, 'last_active_time', stream.create_time)
            if current_time - last_access > self.hot_timeout:
                hot_to_demote.append(stream_id)

        for stream_id in hot_to_demote:
            stream = self.hot_cache.pop(stream_id)
            current_time_local = time.time()
            self.warm_storage[stream_id] = (stream, current_time_local)
            cleanup_stats["hot_to_warm"] += 1

        # 2. 检查温存储超时
        warm_to_demote = []
        for stream_id, (stream, last_access) in self.warm_storage.items():
            if current_time - last_access > self.warm_timeout:
                warm_to_demote.append(stream_id)

        for stream_id in warm_to_demote:
            stream, last_access = self.warm_storage.pop(stream_id)
            self.cold_storage[stream_id] = (stream, last_access)
            cleanup_stats["warm_to_cold"] += 1

        # 3. 检查冷存储超时
        cold_to_remove = []
        for stream_id, (stream, last_access) in self.cold_storage.items():
            if current_time - last_access > self.cold_timeout:
                cold_to_remove.append(stream_id)

        for stream_id in cold_to_remove:
            self.cold_storage.pop(stream_id)
            cleanup_stats["cold_removed"] += 1

        # 更新统计信息
        self.stats.hot_cache_size = len(self.hot_cache)
        self.stats.warm_storage_size = len(self.warm_storage)
        self.stats.cold_storage_size = len(self.cold_storage)
        self.stats.last_cleanup_time = current_time

        # 估算内存使用（粗略估计）
        self.stats.total_memory_usage = (
            len(self.hot_cache) * 1024 +      # 每个热流约1KB
            len(self.warm_storage) * 512 +    # 每个温流约512B
            len(self.cold_storage) * 256      # 每个冷流约256B
        )

        if sum(cleanup_stats.values()) > 0:
            logger.info(
                f"缓存清理完成: {cleanup_stats['hot_to_warm']}热→温, "
                f"{cleanup_stats['warm_to_cold']}温→冷, "
                f"{cleanup_stats['cold_removed']}冷删除"
            )

    def get_stats(self) -> StreamCacheStats:
        """获取缓存统计信息"""
        # 计算命中率
        total_requests = self.stats.cache_hits + self.stats.cache_misses
        hit_rate = self.stats.cache_hits / total_requests if total_requests > 0 else 0

        stats_copy = StreamCacheStats(
            hot_cache_size=self.stats.hot_cache_size,
            warm_storage_size=self.stats.warm_storage_size,
            cold_storage_size=self.stats.cold_storage_size,
            total_memory_usage=self.stats.total_memory_usage,
            cache_hits=self.stats.cache_hits,
            cache_misses=self.stats.cache_misses,
            evictions=self.stats.evictions,
            last_cleanup_time=self.stats.last_cleanup_time,
        )

        # 添加命中率信息
        stats_copy.hit_rate = hit_rate

        return stats_copy

    def clear_cache(self):
        """清空所有缓存"""
        self.hot_cache.clear()
        self.warm_storage.clear()
        self.cold_storage.clear()

        self.stats.hot_cache_size = 0
        self.stats.warm_storage_size = 0
        self.stats.cold_storage_size = 0
        self.stats.total_memory_usage = 0

        logger.info("所有缓存已清空")

    async def get_stream_snapshot(self, stream_id: str) -> Optional[OptimizedChatStream]:
        """获取流的快照（不修改缓存状态）"""
        if stream_id in self.hot_cache:
            return self.hot_cache[stream_id].create_snapshot()
        elif stream_id in self.warm_storage:
            return self.warm_storage[stream_id][0].create_snapshot()
        elif stream_id in self.cold_storage:
            return self.cold_storage[stream_id][0].create_snapshot()
        return None

    def get_cached_stream_ids(self) -> Set[str]:
        """获取所有缓存的流ID"""
        return set(self.hot_cache.keys()) | set(self.warm_storage.keys()) | set(self.cold_storage.keys())


# 全局缓存管理器实例
_cache_manager: Optional[TieredStreamCache] = None


def get_stream_cache_manager() -> TieredStreamCache:
    """获取流缓存管理器实例"""
    global _cache_manager
    if _cache_manager is None:
        _cache_manager = TieredStreamCache()
    return _cache_manager


async def init_stream_cache_manager():
    """初始化流缓存管理器"""
    manager = get_stream_cache_manager()
    await manager.start()


async def shutdown_stream_cache_manager():
    """关闭流缓存管理器"""
    manager = get_stream_cache_manager()
    await manager.stop()