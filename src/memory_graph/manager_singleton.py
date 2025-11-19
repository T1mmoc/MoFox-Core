"""
记忆系统管理单例

提供全局访问的 MemoryManager 和 UnifiedMemoryManager 实例
"""

from __future__ import annotations

from pathlib import Path

from src.common.logger import get_logger
from src.memory_graph.manager import MemoryManager

logger = get_logger(__name__)

# 全局 MemoryManager 实例（旧的单层记忆系统，已弃用）
_memory_manager: MemoryManager | None = None
_initialized: bool = False

# 全局 UnifiedMemoryManager 实例（新的三层记忆系统）
_unified_memory_manager = None


# ============================================================================
# 旧的单层记忆系统 API（已弃用，保留用于向后兼容）
# ============================================================================


async def initialize_memory_manager(
    data_dir: Path | str | None = None,
) -> MemoryManager | None:
    """
    初始化全局 MemoryManager

    直接从 global_config.memory 读取配置

    Args:
        data_dir: 数据目录（可选，默认从配置读取）

    Returns:
        MemoryManager 实例，如果禁用则返回 None
    """
    global _memory_manager, _initialized

    if _initialized and _memory_manager:
        logger.info("MemoryManager 已经初始化，返回现有实例")
        return _memory_manager

    try:
        from src.config.config import global_config

        # 检查是否启用
        if not global_config.memory or not getattr(global_config.memory, "enable", False):
            logger.info("记忆图系统已在配置中禁用")
            _initialized = False
            _memory_manager = None
            return None

        # 处理数据目录
        if data_dir is None:
            data_dir = getattr(global_config.memory, "data_dir", "data/memory_graph")
        if isinstance(data_dir, str):
            data_dir = Path(data_dir)

        logger.info(f"正在初始化全局 MemoryManager (data_dir={data_dir})...")

        _memory_manager = MemoryManager(data_dir=data_dir)
        await _memory_manager.initialize()

        _initialized = True
        logger.info("✅ 全局 MemoryManager 初始化成功")

        return _memory_manager

    except Exception as e:
        logger.error(f"初始化 MemoryManager 失败: {e}", exc_info=True)
        _initialized = False
        _memory_manager = None
        raise


def get_memory_manager() -> MemoryManager | None:
    """
    获取全局 MemoryManager 实例

    Returns:
        MemoryManager 实例，如果未初始化则返回 None
    """
    if not _initialized or _memory_manager is None:
        logger.warning("MemoryManager 尚未初始化，请先调用 initialize_memory_manager()")
        return None

    return _memory_manager


async def shutdown_memory_manager():
    """关闭全局 MemoryManager"""
    global _memory_manager, _initialized

    if _memory_manager:
        try:
            logger.info("正在关闭全局 MemoryManager...")
            await _memory_manager.shutdown()
            logger.info("✅ 全局 MemoryManager 已关闭")
        except Exception as e:
            logger.error(f"关闭 MemoryManager 时出错: {e}", exc_info=True)
        finally:
            _memory_manager = None
            _initialized = False


def is_initialized() -> bool:
    """检查 MemoryManager 是否已初始化"""
    return _initialized and _memory_manager is not None


# ============================================================================
# 新的三层记忆系统 API（推荐使用）
# ============================================================================


async def initialize_unified_memory_manager():
    """
    初始化统一记忆管理器（三层记忆系统）

    从全局配置读取参数

    Returns:
        初始化后的管理器实例，未启用返回 None
    """
    global _unified_memory_manager

    if _unified_memory_manager is not None:
        logger.warning("统一记忆管理器已经初始化")
        return _unified_memory_manager

    try:
        from src.config.config import global_config
        from src.memory_graph.unified_manager import UnifiedMemoryManager

        # 检查是否启用三层记忆系统
        if not hasattr(global_config, "memory") or not getattr(
            global_config.memory, "enable", False
        ):
            logger.warning("三层记忆系统未启用，跳过初始化")
            return None

        config = global_config.memory

        # 创建管理器实例
        # 注意：我们将 data_dir 指向 three_tier 子目录，以隔离感知/短期记忆数据
        # 同时传入全局 _memory_manager 以共享长期记忆图存储
        base_data_dir = Path(getattr(config, "data_dir", "data/memory_graph"))
        
        _unified_memory_manager = UnifiedMemoryManager(
            data_dir=base_data_dir,
            memory_manager=_memory_manager,
            # 感知记忆配置
            perceptual_max_blocks=getattr(config, "perceptual_max_blocks", 50),
            perceptual_block_size=getattr(config, "perceptual_block_size", 5),
            perceptual_activation_threshold=getattr(config, "perceptual_activation_threshold", 3),
            perceptual_recall_top_k=getattr(config, "perceptual_topk", 5),
            perceptual_recall_threshold=getattr(config, "perceptual_similarity_threshold", 0.55),
            # 短期记忆配置
            short_term_max_memories=getattr(config, "short_term_max_memories", 30),
            short_term_transfer_threshold=getattr(config, "short_term_transfer_threshold", 0.6),
            # 长期记忆配置
            long_term_batch_size=getattr(config, "long_term_batch_size", 10),
            long_term_search_top_k=getattr(config, "search_top_k", 5),
            long_term_decay_factor=getattr(config, "long_term_decay_factor", 0.95),
            long_term_auto_transfer_interval=getattr(config, "long_term_auto_transfer_interval", 600),
            # 智能检索配置
            judge_confidence_threshold=getattr(config, "judge_confidence_threshold", 0.7),
        )

        # 初始化
        await _unified_memory_manager.initialize()

        logger.info("✅ 统一记忆管理器单例已初始化")
        return _unified_memory_manager

    except Exception as e:
        logger.error(f"初始化统一记忆管理器失败: {e}", exc_info=True)
        raise


def get_unified_memory_manager():
    """
    获取统一记忆管理器实例（三层记忆系统）

    Returns:
        管理器实例，未初始化返回 None
    """
    if _unified_memory_manager is None:
        logger.warning("统一记忆管理器尚未初始化，请先调用 initialize_unified_memory_manager()")
    return _unified_memory_manager


async def shutdown_unified_memory_manager() -> None:
    """关闭统一记忆管理器"""
    global _unified_memory_manager

    if _unified_memory_manager is None:
        logger.warning("统一记忆管理器未初始化，无需关闭")
        return

    try:
        await _unified_memory_manager.shutdown()
        _unified_memory_manager = None
        logger.info("✅ 统一记忆管理器已关闭")

    except Exception as e:
        logger.error(f"关闭统一记忆管理器失败: {e}", exc_info=True)

