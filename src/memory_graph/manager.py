"""
记忆管理器 - Phase 3

统一的记忆系统管理接口，整合所有组件：
- 记忆创建、检索、更新、删除
- 记忆生命周期管理（激活、遗忘）
- 记忆整合与维护
- 多策略检索优化
"""

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.config.config import global_config
from src.config.official_configs import MemoryConfig
from src.memory_graph.core.builder import MemoryBuilder
from src.memory_graph.core.extractor import MemoryExtractor
from src.memory_graph.models import EdgeType, Memory, MemoryEdge, NodeType
from src.memory_graph.storage.graph_store import GraphStore
from src.memory_graph.storage.persistence import PersistenceManager
from src.memory_graph.storage.vector_store import VectorStore
from src.memory_graph.tools.memory_tools import MemoryTools
from src.memory_graph.utils.embeddings import EmbeddingGenerator
from src.memory_graph.utils.similarity import cosine_similarity

if TYPE_CHECKING:
    import numpy as np

logger = logging.getLogger(__name__)


class MemoryManager:
    """
    记忆管理器

    核心管理类，提供记忆系统的统一接口：
    - 记忆 CRUD 操作
    - 记忆生命周期管理
    - 智能检索与推荐
    - 记忆维护与优化
    """

    def __init__(
        self,
        data_dir: Path | None = None,
    ):
        """
        初始化记忆管理器

        Args:
            data_dir: 数据目录（可选，默认从global_config读取）
        """
        # 直接使用 global_config.memory
        if not global_config.memory or not getattr(global_config.memory, "enable", False):
            raise ValueError("记忆系统未启用，请在配置文件中启用 [memory] enable = true")

        self.config: MemoryConfig = global_config.memory
        self.data_dir = data_dir or Path(getattr(self.config, "data_dir", "data/memory_graph"))

        # 存储组件
        self.vector_store: VectorStore | None = None
        self.graph_store: GraphStore | None = None
        self.persistence: PersistenceManager | None = None

        # 核心组件
        self.embedding_generator: EmbeddingGenerator | None = None
        self.extractor: MemoryExtractor | None = None
        self.builder: MemoryBuilder | None = None
        self.tools: MemoryTools | None = None

        # 状态
        self._initialized = False
        self._last_maintenance = datetime.now()
        self._maintenance_task: asyncio.Task | None = None
        self._maintenance_interval_hours = getattr(self.config, "consolidation_interval_hours", 1.0)
        self._maintenance_running = False  # 维护任务运行状态

        logger.info(f"记忆管理器已创建 (data_dir={self.data_dir}, enable={getattr(self.config, 'enable', False)})")

    async def initialize(self) -> None:
        """
        初始化所有组件

        按照依赖顺序初始化：
        1. 存储层（向量存储、图存储、持久化）
        2. 工具层（嵌入生成器、提取器）
        3. 管理层（构建器、工具接口）
        """
        if self._initialized:
            logger.warning("记忆管理器已经初始化")
            return

        try:
            logger.info("开始初始化记忆管理器...")

            # 1. 初始化存储层
            self.data_dir.mkdir(parents=True, exist_ok=True)

            # 获取存储配置
            storage_config = getattr(self.config, "storage", None)
            vector_collection_name = getattr(storage_config, "vector_collection_name", "memory_graph") if storage_config else "memory_graph"

            self.vector_store = VectorStore(
                collection_name=vector_collection_name,
                data_dir=self.data_dir,
            )
            await self.vector_store.initialize()

            self.persistence = PersistenceManager(data_dir=self.data_dir)

            # 尝试加载现有图数据
            self.graph_store = await self.persistence.load_graph_store()
            if not self.graph_store:
                logger.info("未找到现有图数据，创建新的图存储")
                self.graph_store = GraphStore()
            else:
                stats = self.graph_store.get_statistics()
                logger.info(
                    f"加载图数据: {stats['total_memories']} 条记忆, "
                    f"{stats['total_nodes']} 个节点, {stats['total_edges']} 条边"
                )

            # 2. 初始化工具层
            self.embedding_generator = EmbeddingGenerator()
            # EmbeddingGenerator 使用延迟初始化，在第一次调用时自动初始化

            self.extractor = MemoryExtractor()

            # 3. 初始化管理层
            self.builder = MemoryBuilder(
                vector_store=self.vector_store,
                graph_store=self.graph_store,
                embedding_generator=self.embedding_generator,
            )

            # 检查配置值
            # 兼容性处理：如果配置项不存在，使用默认值或映射到新配置项
            expand_depth = getattr(self.config, "path_expansion_max_hops", 2)
            expand_semantic_threshold = getattr(self.config, "search_similarity_threshold", 0.5)
            search_top_k = getattr(self.config, "search_top_k", 10)
            
            # 读取权重配置
            search_vector_weight = getattr(self.config, "vector_weight", 0.65)
            # context_weight 近似映射为 importance_weight
            search_importance_weight = getattr(self.config, "context_weight", 0.25)
            search_recency_weight = getattr(self.config, "recency_weight", 0.10)
            
            # 读取阈值过滤配置
            search_min_importance = getattr(self.config, "search_min_importance", 0.3)
            search_similarity_threshold = getattr(self.config, "search_similarity_threshold", 0.5)

            logger.info(
                f"📊 配置检查: expand_depth={expand_depth}, "
                f"expand_semantic_threshold={expand_semantic_threshold}, "
                f"search_top_k={search_top_k}"
            )
            logger.info(
                f"📊 权重配置: vector={search_vector_weight}, "
                f"importance={search_importance_weight}, "
                f"recency={search_recency_weight}"
            )
            logger.info(
                f"📊 阈值过滤: min_importance={search_min_importance}, "
                f"similarity_threshold={search_similarity_threshold}"
            )

            self.tools = MemoryTools(
                vector_store=self.vector_store,
                graph_store=self.graph_store,
                persistence_manager=self.persistence,
                embedding_generator=self.embedding_generator,
                max_expand_depth=expand_depth,  # 从配置读取图扩展深度
                expand_semantic_threshold=expand_semantic_threshold,  # 从配置读取图扩展语义阈值
                search_top_k=search_top_k,  # 从配置读取默认 top_k
                search_vector_weight=search_vector_weight,  # 从配置读取向量权重
                search_importance_weight=search_importance_weight,  # 从配置读取重要性权重
                search_recency_weight=search_recency_weight,  # 从配置读取时效性权重
                search_min_importance=search_min_importance,  # 从配置读取最小重要性阈值
                search_similarity_threshold=search_similarity_threshold,  # 从配置读取相似度阈值
            )

            self._initialized = True
            logger.info("✅ 记忆管理器初始化完成")

            # 启动后台维护任务
            self._start_maintenance_task()

        except Exception as e:
            logger.error(f"记忆管理器初始化失败: {e}", exc_info=True)
            raise

    async def shutdown(self) -> None:
        """
        关闭记忆管理器

        执行清理操作：
        - 停止维护调度任务
        - 保存所有数据
        - 关闭存储组件
        """
        if not self._initialized:
            logger.warning("记忆管理器未初始化，无需关闭")
            return

        try:
            logger.info("正在关闭记忆管理器...")

            # 1. 停止维护任务
            await self._stop_maintenance_task()

            # 2. 执行最后一次维护（保存数据）
            if self.graph_store and self.persistence:
                logger.info("执行最终数据保存...")
                await self.persistence.save_graph_store(self.graph_store)

            # 3. 关闭存储组件
            if self.vector_store:
                # VectorStore 使用 chromadb，无需显式关闭
                pass

            self._initialized = False
            logger.info("✅ 记忆管理器已关闭")

        except Exception as e:
            logger.error(f"关闭记忆管理器失败: {e}", exc_info=True)

    # ==================== 记忆 CRUD 操作 ====================

    async def create_memory(
        self,
        subject: str,
        memory_type: str,
        topic: str,
        object: str | None = None,
        attributes: dict[str, str] | None = None,
        importance: float = 0.5,
        **kwargs,
    ) -> Memory | None:
        """
        创建新记忆

        Args:
            subject: 主体（谁）
            memory_type: 记忆类型（事件/观点/事实/关系）
            topic: 主题（做什么/想什么）
            object: 客体（对谁/对什么）
            attributes: 属性字典（时间、地点、原因等）
            importance: 重要性 (0.0-1.0)
            **kwargs: 其他参数

        Returns:
            创建的记忆对象，失败返回 None
        """
        if not self._initialized:
            await self.initialize()

        try:
            result = await self.tools.create_memory(
                subject=subject,
                memory_type=memory_type,
                topic=topic,
                object=object,
                attributes=attributes,
                importance=importance,
                **kwargs,
            )

            if result["success"]:
                memory_id = result["memory_id"]
                memory = self.graph_store.get_memory_by_id(memory_id)
                logger.info(f"记忆创建成功: {memory_id}")
                return memory
            else:
                logger.error(f"记忆创建失败: {result.get('error', 'Unknown error')}")
                return None

        except Exception as e:
            logger.error(f"创建记忆时发生异常: {e}", exc_info=True)
            return None

    async def get_memory(self, memory_id: str) -> Memory | None:
        """
        根据 ID 获取记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            记忆对象，不存在返回 None
        """
        if not self._initialized:
            await self.initialize()

        return self.graph_store.get_memory_by_id(memory_id)

    async def update_memory(
        self,
        memory_id: str,
        **updates,
    ) -> bool:
        """
        更新记忆

        Args:
            memory_id: 记忆 ID
            **updates: 要更新的字段

        Returns:
            是否更新成功
        """
        if not self._initialized:
            await self.initialize()

        try:
            memory = self.graph_store.get_memory_by_id(memory_id)
            if not memory:
                logger.warning(f"记忆不存在: {memory_id}")
                return False

            # 更新元数据
            if "importance" in updates:
                memory.importance = updates["importance"]

            if "metadata" in updates:
                memory.metadata.update(updates["metadata"])

            memory.updated_at = datetime.now()

            # 异步保存更新（不阻塞当前操作）
            asyncio.create_task(self._async_save_graph_store("更新记忆"))
            logger.info(f"记忆更新成功: {memory_id}")
            return True

        except Exception as e:
            logger.error(f"更新记忆失败: {e}", exc_info=True)
            return False

    async def delete_memory(self, memory_id: str) -> bool:
        """
        删除记忆

        Args:
            memory_id: 记忆 ID

        Returns:
            是否删除成功
        """
        if not self._initialized:
            await self.initialize()

        try:
            memory = self.graph_store.get_memory_by_id(memory_id)
            if not memory:
                logger.warning(f"记忆不存在: {memory_id}")
                return False

            # 从向量存储删除节点
            if self.vector_store:
                for node in memory.nodes:
                    if getattr(node, "has_vector", False):
                        await self.vector_store.delete_node(node.id)
                        node.has_vector = False
                        if self.graph_store.graph.has_node(node.id):
                            self.graph_store.graph.nodes[node.id]["has_vector"] = False

            # 从图存储删除记忆
            self.graph_store.remove_memory(memory_id)

            # 异步保存更新（不阻塞当前操作）
            asyncio.create_task(self._async_save_graph_store("删除记忆"))
            logger.info(f"记忆删除成功: {memory_id}")
            return True

        except Exception as e:
            logger.error(f"删除记忆失败: {e}", exc_info=True)
            return False

    # ==================== 记忆检索操作 ====================
    async def search_memories(
        self,
        query: str,
        top_k: int | None = None,
        memory_types: list[str] | None = None,
        time_range: tuple[datetime, datetime] | None = None,
        min_importance: float = 0.0,
        include_forgotten: bool = False,
        use_multi_query: bool = True,
        expand_depth: int | None = None,
        context: dict[str, Any] | None = None,
        prefer_node_types: list[str] | None = None,  # 🆕 偏好节点类型
    ) -> list[Memory]:
        """
        搜索记忆

        使用多策略检索优化，解决复杂查询问题。
        例如："杰瑞喵如何评价新的记忆系统" 会被分解为多个子查询，
        确保同时匹配"杰瑞喵"和"新的记忆系统"两个关键概念。

        同时支持图扩展：从初始检索结果出发，沿图结构查找语义相关的邻居记忆。

        Args:
            query: 搜索查询
            top_k: 返回结果数
            memory_types: 记忆类型过滤
            time_range: 时间范围过滤 (start, end)
            min_importance: 最小重要性
            include_forgotten: 是否包含已遗忘的记忆
            use_multi_query: 是否使用多查询策略（推荐，默认True）
            expand_depth: 图扩展深度（0=禁用, 1=推荐, 2-3=深度探索）
            context: 查询上下文（用于优化）
            prefer_node_types: 偏好节点类型列表（如 ["ENTITY", "EVENT"]）🆕

        Returns:
            记忆列表
        """
        if not self._initialized:
            await self.initialize()

        try:
            # 使用配置的默认值
            if top_k is None:
                top_k = getattr(self.config, "search_top_k", 10)

            # 准备搜索参数
            params = {
                "query": query,
                "top_k": top_k,
                "use_multi_query": use_multi_query,
                "expand_depth": expand_depth or getattr(global_config.memory, "path_expansion_max_hops", 2),  # 传递图扩展深度
                "context": context,
                "prefer_node_types": prefer_node_types or [],  # 🆕 传递偏好节点类型
            }

            if memory_types:
                params["memory_types"] = memory_types

            # 执行搜索
            result = await self.tools.search_memories(**params)

            if not result["success"]:
                logger.error(f"搜索失败: {result.get('error', 'Unknown error')}")
                return []

            memories = result.get("results", [])

            # 后处理过滤
            filtered_memories = []
            for mem_dict in memories:
                # 从字典重建 Memory 对象
                memory_id = mem_dict.get("memory_id", "")
                if not memory_id:
                    continue

                memory = self.graph_store.get_memory_by_id(memory_id)
                if not memory:
                    continue

                # 重要性过滤
                if min_importance is not None and memory.importance < min_importance:
                    continue

                # 遗忘状态过滤
                if not include_forgotten and memory.metadata.get("forgotten", False):
                    continue

                # 时间范围过滤
                if time_range:
                    mem_time = memory.created_at
                    if not (time_range[0] <= mem_time <= time_range[1]):
                        continue

                filtered_memories.append(memory)

            strategy = result.get("strategy", "unknown")
            logger.info(
                f"搜索完成: 找到 {len(filtered_memories)} 条记忆 (策略={strategy})"
            )

            # 强制激活被检索到的记忆（核心功能）- 使用快速批量激活
            if filtered_memories:
                await self._quick_batch_activate_memories(filtered_memories)

            return filtered_memories[:top_k]

        except Exception as e:
            logger.error(f"搜索记忆失败: {e}", exc_info=True)
            return []

    async def link_memories(
        self,
        source_description: str,
        target_description: str,
        relation_type: str,
        importance: float = 0.5,
    ) -> bool:
        """
        关联两条记忆

        Args:
            source_description: 源记忆描述
            target_description: 目标记忆描述
            relation_type: 关系类型（导致/引用/相似/相反）
            importance: 关系重要性

        Returns:
            是否关联成功
        """
        if not self._initialized:
            await self.initialize()

        try:
            result = await self.tools.link_memories(
                source_memory_description=source_description,
                target_memory_description=target_description,
                relation_type=relation_type,
                importance=importance,
            )

            if result["success"]:
                logger.info(
                    f"记忆关联成功: {result['source_memory_id']} -> "
                    f"{result['target_memory_id']} ({relation_type})"
                )
                return True
            else:
                logger.error(f"记忆关联失败: {result.get('error', 'Unknown error')}")
                return False

        except Exception as e:
            logger.error(f"关联记忆失败: {e}", exc_info=True)
            return False

    # ==================== 记忆生命周期管理 ====================

    async def activate_memory(self, memory_id: str, strength: float = 1.0) -> bool:
        """
        激活记忆

        更新记忆的激活度，并传播到相关记忆

        Args:
            memory_id: 记忆 ID
            strength: 激活强度 (0.0-1.0)

        Returns:
            是否激活成功
        """
        if not self._initialized:
            await self.initialize()

        try:
            memory = self.graph_store.get_memory_by_id(memory_id)
            if not memory:
                logger.warning(f"记忆不存在: {memory_id}")
                return False

            # 更新激活信息
            now = datetime.now()
            activation_info = memory.metadata.get("activation", {})

            # 更新激活度（考虑时间衰减）
            last_access = activation_info.get("last_access")
            if last_access:
                # 计算时间衰减
                last_access_dt = datetime.fromisoformat(last_access)
                hours_passed = (now - last_access_dt).total_seconds() / 3600
                decay_rate = getattr(self.config, "activation_decay_rate", 0.95)
                decay_factor = decay_rate ** (hours_passed / 24)
                current_activation = activation_info.get("level", 0.0) * decay_factor
            else:
                current_activation = 0.0

            # 新的激活度 = 当前激活度 + 激活强度
            new_activation = min(1.0, current_activation + strength)

            activation_info.update({
                "level": new_activation,
                "last_access": now.isoformat(),
                "access_count": activation_info.get("access_count", 0) + 1,
            })

            # 同步更新 memory.activation 字段，确保数据一致性
            memory.activation = new_activation
            memory.metadata["activation"] = activation_info
            memory.last_accessed = now

            # 激活传播：激活相关记忆
            if strength > 0.1:  # 只有足够强的激活才传播
                propagation_depth = getattr(self.config, "activation_propagation_depth", 2)
                related_memories = self._get_related_memories(
                    memory_id,
                    max_depth=propagation_depth
                )
                propagation_strength_factor = getattr(self.config, "activation_propagation_strength", 0.5)
                propagation_strength = strength * propagation_strength_factor

                max_related = getattr(self.config, "max_related_memories", 5)
                for related_id in related_memories[:max_related]:
                    await self.activate_memory(related_id, propagation_strength)

            # 异步保存更新（不阻塞当前操作）
            asyncio.create_task(self._async_save_graph_store("激活记忆"))
            logger.debug(f"记忆已激活: {memory_id} (level={new_activation:.3f})")
            return True

        except Exception as e:
            logger.error(f"激活记忆失败: {e}", exc_info=True)
            return False

    async def _auto_activate_searched_memories(self, memories: list[Memory]) -> None:
        """
        批量激活被搜索到的记忆

        Args:
            memories: 被检索到的记忆列表
        """
        try:
            if not memories:
                return

            # 获取配置参数
            base_strength = getattr(self.config, "auto_activate_base_strength", 0.1)
            max_activate_count = getattr(self.config, "auto_activate_max_count", 5)
            decay_rate = getattr(self.config, "activation_decay_rate", 0.9)
            now = datetime.now()

            # 限制处理的记忆数量
            memories_to_activate = memories[:max_activate_count]

            # 批量更新激活度
            activation_updates = []
            for memory in memories_to_activate:
                # 计算激活强度
                strength = base_strength * (0.5 + memory.importance)

                # 获取当前激活度信息
                activation_info = memory.metadata.get("activation", {})
                last_access = activation_info.get("last_access")

                if last_access:
                    # 计算时间衰减
                    last_access_dt = datetime.fromisoformat(last_access)
                    hours_passed = (now - last_access_dt).total_seconds() / 3600
                    decay_factor = decay_rate ** (hours_passed / 24)
                    current_activation = activation_info.get("level", 0.0) * decay_factor
                else:
                    current_activation = 0.0

                # 计算新的激活度
                new_activation = min(1.0, current_activation + strength)

                # 更新记忆对象
                memory.activation = new_activation
                memory.last_accessed = now
                activation_info.update({
                    "level": new_activation,
                    "last_access": now.isoformat(),
                    "access_count": activation_info.get("access_count", 0) + 1,
                })
                memory.metadata["activation"] = activation_info

                activation_updates.append({
                    "memory_id": memory.id,
                    "old_activation": current_activation,
                    "new_activation": new_activation,
                    "strength": strength
                })

            # 批量保存到数据库（异步执行）
            if activation_updates:
                asyncio.create_task(self._async_save_graph_store("批量激活更新"))

                # 激活传播（异步执行，不阻塞主流程）
                asyncio.create_task(self._batch_propagate_activation(memories_to_activate, base_strength))

                logger.debug(f"批量激活 {len(activation_updates)} 条记忆完成")

        except Exception as e:
            logger.warning(f"批量激活搜索记忆失败: {e}")

    async def _quick_batch_activate_memories(self, memories: list[Memory]) -> None:
        """
        快速批量激活记忆（用于搜索结果，优化性能）

        与 _auto_activate_searched_memories 的区别：
        - 更轻量级，专注于速度
        - 简化激活传播逻辑
        - 减少数据库写入次数

        Args:
            memories: 需要激活的记忆列表
        """
        try:
            if not memories:
                return

            # 获取配置参数
            base_strength = getattr(self.config, "auto_activate_base_strength", 0.1)
            max_activate_count = getattr(self.config, "auto_activate_max_count", 5)
            decay_rate = getattr(self.config, "activation_decay_rate", 0.9)
            now = datetime.now()

            # 限制处理的记忆数量
            memories_to_activate = memories[:max_activate_count]

            # 批量更新激活度（内存操作）
            for memory in memories_to_activate:
                # 计算激活强度
                strength = base_strength * (0.5 + memory.importance)

                # 快速计算新的激活度（简化版）
                activation_info = memory.metadata.get("activation", {})
                last_access = activation_info.get("last_access")

                if last_access:
                    # 简化的时间衰减计算
                    try:
                        last_access_dt = datetime.fromisoformat(last_access)
                        hours_passed = (now - last_access_dt).total_seconds() / 3600
                        decay_factor = decay_rate ** (hours_passed / 24)
                        current_activation = activation_info.get("level", 0.0) * decay_factor
                    except (ValueError, TypeError):
                        current_activation = activation_info.get("level", 0.0) * 0.9  # 默认衰减
                else:
                    current_activation = 0.0

                # 计算新的激活度
                new_activation = min(1.0, current_activation + strength)

                # 直接更新记忆对象（内存中）
                memory.activation = new_activation
                memory.last_accessed = now
                activation_info.update({
                    "level": new_activation,
                    "last_access": now.isoformat(),
                    "access_count": activation_info.get("access_count", 0) + 1,
                })
                memory.metadata["activation"] = activation_info

            # 异步批量保存（不阻塞搜索）
            if memories_to_activate:
                asyncio.create_task(self._background_save_activation(memories_to_activate, base_strength))

            logger.debug(f"快速批量激活 {len(memories_to_activate)} 条记忆")

        except Exception as e:
            logger.warning(f"快速批量激活记忆失败: {e}")

    async def _background_save_activation(self, memories: list[Memory], base_strength: float) -> None:
        """
        后台保存激活更新并执行传播

        Args:
            memories: 已更新的记忆列表
            base_strength: 基础激活强度
        """
        try:
            # 批量保存到数据库（异步执行）
            asyncio.create_task(self._async_save_graph_store("后台激活更新"))

            # 简化的激活传播（仅在强度足够时执行）
            if base_strength > 0.08:  # 提高传播阈值，减少传播频率
                propagation_strength_factor = getattr(self.config, "activation_propagation_strength", 0.3)  # 降低传播强度
                max_related = getattr(self.config, "max_related_memories", 3)  # 减少传播数量

                # 只传播最重要的记忆的激活
                important_memories = [m for m in memories if m.importance > 0.6][:2]  # 最多2个重要记忆

                for memory in important_memories:
                    related_memories = self._get_related_memories(memory.id, max_depth=1)  # 减少传播深度
                    propagation_strength = base_strength * propagation_strength_factor

                    for related_id in related_memories[:max_related]:
                        try:
                            related_memory = self.graph_store.get_memory_by_id(related_id)
                            if related_memory:
                                # 简单的激活度增加（不调用完整激活方法）
                                current_activation = related_memory.metadata.get("activation", {}).get("level", related_memory.activation)
                                new_activation = min(1.0, current_activation + propagation_strength * 0.5)

                                related_memory.activation = new_activation
                                related_memory.metadata["activation"] = {
                                    "level": new_activation,
                                    "last_access": datetime.now().isoformat(),
                                    "access_count": related_memory.metadata.get("activation", {}).get("access_count", 0) + 1,
                                }
                        except Exception as e:
                            logger.debug(f"传播激活到相关记忆 {related_id[:8]} 失败: {e}")

                # 再次保存传播后的更新
                await self.persistence.save_graph_store(self.graph_store)

            logger.debug(f"后台保存激活更新完成，处理了 {len(memories)} 条记忆")

        except Exception as e:
            logger.warning(f"后台保存激活更新失败: {e}")

    async def _batch_propagate_activation(self, memories: list[Memory], base_strength: float) -> None:
        """
        批量传播激活到相关记忆（后台执行）

        Args:
            memories: 已激活的记忆列表
            base_strength: 基础激活强度
        """
        try:
            propagation_strength_factor = getattr(self.config, "activation_propagation_strength", 0.5)
            propagation_depth = getattr(self.config, "activation_propagation_depth", 2)
            max_related = getattr(self.config, "max_related_memories", 5)

            # 收集所有需要传播激活的记忆ID
            propagation_tasks = []
            for memory in memories:
                if base_strength > 0.05:  # 只有足够强的激活才传播
                    related_memories = self._get_related_memories(
                        memory.id,
                        max_depth=propagation_depth
                    )
                    propagation_strength = base_strength * propagation_strength_factor

                    for related_id in related_memories[:max_related]:
                        task = self.activate_memory(related_id, propagation_strength)
                        propagation_tasks.append(task)

            # 批量执行传播任务
            if propagation_tasks:
                import asyncio
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*propagation_tasks, return_exceptions=True),
                        timeout=3.0  # 传播操作超时时间稍长
                    )
                    logger.debug(f"激活传播完成: {len(propagation_tasks)} 个相关记忆")
                except asyncio.TimeoutError:
                    logger.warning("激活传播超时，部分相关记忆未激活")
                except Exception as e:
                    logger.warning(f"激活传播失败: {e}")

        except Exception as e:
            logger.warning(f"批量传播激活失败: {e}")

    def _get_related_memories(self, memory_id: str, max_depth: int = 1) -> list[str]:
        """
        获取相关记忆 ID 列表（旧版本，保留用于激活传播）

        Args:
            memory_id: 记忆 ID
            max_depth: 最大遍历深度

        Returns:
            相关记忆 ID 列表
        """
        memory = self.graph_store.get_memory_by_id(memory_id)
        if not memory:
            return []

        related_ids = set()

        # 遍历记忆的节点
        for node in memory.nodes:
            # 获取节点的邻居
            neighbors = list(self.graph_store.graph.neighbors(node.id))

            for neighbor_id in neighbors:
                # 获取邻居节点所属的记忆
                neighbor_node = self.graph_store.graph.nodes.get(neighbor_id)
                if neighbor_node:
                    neighbor_memory_ids = neighbor_node.get("memory_ids", [])
                    for mem_id in neighbor_memory_ids:
                        if mem_id != memory_id:
                            related_ids.add(mem_id)

        return list(related_ids)

    async def forget_memory(self, memory_id: str, cleanup_orphans: bool = True) -> bool:
        """
        遗忘记忆（直接删除）

        这个方法会：
        1. 从向量存储中删除节点的嵌入向量
        2. 从图存储中删除记忆
        3. 可选：清理孤立节点（建议批量遗忘后统一清理）
        4. 保存更新后的数据

        Args:
            memory_id: 记忆 ID
            cleanup_orphans: 是否立即清理孤立节点（默认True，批量遗忘时设为False）

        Returns:
            是否遗忘成功
        """
        if not self._initialized:
            await self.initialize()

        try:
            memory = self.graph_store.get_memory_by_id(memory_id)
            if not memory:
                logger.warning(f"记忆不存在: {memory_id}")
                return False

            # 1. 从向量存储删除节点的嵌入向量
            deleted_vectors = 0
            if self.vector_store:
                for node in memory.nodes:
                    if getattr(node, "has_vector", False):
                        try:
                            await self.vector_store.delete_node(node.id)
                            deleted_vectors += 1
                            node.has_vector = False
                            if self.graph_store.graph.has_node(node.id):
                                self.graph_store.graph.nodes[node.id]["has_vector"] = False
                        except Exception as e:
                            logger.warning(f"删除节点向量失败 {node.id}: {e}")

            # 2. 从图存储删除记忆
            success = self.graph_store.remove_memory(memory_id, cleanup_orphans=False)

            if success:
                # 3. 可选：清理孤立节点
                if cleanup_orphans:
                    orphan_nodes, orphan_edges = await self._cleanup_orphan_nodes_and_edges()
                    logger.info(
                        f"记忆已遗忘并删除: {memory_id} "
                        f"(删除了 {deleted_vectors} 个向量, 清理了 {orphan_nodes} 个孤立节点, {orphan_edges} 条孤立边)"
                    )
                else:
                    logger.debug(f"记忆已删除: {memory_id} (删除了 {deleted_vectors} 个向量)")

                # 4. 异步保存更新（不阻塞当前操作）
                asyncio.create_task(self._async_save_graph_store("删除相关记忆"))
                return True
            else:
                logger.error(f"从图存储删除记忆失败: {memory_id}")
                return False

        except Exception as e:
            logger.error(f"遗忘记忆失败: {e}", exc_info=True)
            return False

    async def auto_forget_memories(self, threshold: float = 0.1) -> int:
        """
        自动遗忘低激活度的记忆（批量优化版）

        应用时间衰减公式计算当前激活度，低于阈值则遗忘。
        衰减公式：activation = base_activation * (decay_rate ^ days_passed)
        
        优化：批量删除记忆后统一清理孤立节点，减少重复检查

        Args:
            threshold: 激活度阈值

        Returns:
            遗忘的记忆数量
        """
        if not self._initialized:
            await self.initialize()

        try:
            forgotten_count = 0
            all_memories = self.graph_store.get_all_memories()

            # 获取配置参数
            min_importance = getattr(self.config, "forgetting_min_importance", 0.8)
            decay_rate = getattr(self.config, "activation_decay_rate", 0.9)

            # 收集需要遗忘的记忆ID
            memories_to_forget = []

            for memory in all_memories:
                # 跳过已遗忘的记忆
                if memory.metadata.get("forgotten", False):
                    continue

                # 跳过高重要性记忆（保护重要记忆不被遗忘）
                if memory.importance >= min_importance:
                    continue

                # 计算当前激活度（应用时间衰减）
                activation_info = memory.metadata.get("activation", {})
                base_activation = activation_info.get("level", memory.activation)
                last_access = activation_info.get("last_access")

                if last_access:
                    try:
                        last_access_dt = datetime.fromisoformat(last_access)
                        days_passed = (datetime.now() - last_access_dt).days

                        # 应用指数衰减：activation = base * (decay_rate ^ days)
                        current_activation = base_activation * (decay_rate ** days_passed)

                        logger.debug(
                            f"记忆 {memory.id[:8]}: 基础激活度={base_activation:.3f}, "
                            f"经过{days_passed}天衰减后={current_activation:.3f}"
                        )
                    except (ValueError, TypeError) as e:
                        logger.warning(f"解析时间失败: {e}, 使用基础激活度")
                        current_activation = base_activation
                else:
                    # 没有访问记录，使用基础激活度
                    current_activation = base_activation

                # 低于阈值则标记为待遗忘
                if current_activation < threshold:
                    memories_to_forget.append((memory.id, current_activation))
                    logger.debug(
                        f"标记遗忘 {memory.id[:8]}: 激活度={current_activation:.3f} < 阈值={threshold:.3f}"
                    )

            # 批量遗忘记忆（不立即清理孤立节点）
            if memories_to_forget:
                logger.info(f"开始批量遗忘 {len(memories_to_forget)} 条记忆...")

                for memory_id, activation in memories_to_forget:
                    # cleanup_orphans=False：暂不清理孤立节点
                    success = await self.forget_memory(memory_id, cleanup_orphans=False)
                    if success:
                        forgotten_count += 1

                # 统一清理孤立节点和边
                logger.info("批量遗忘完成，开始统一清理孤立节点和边...")
                orphan_nodes, orphan_edges = await self._cleanup_orphan_nodes_and_edges()

                # 保存最终更新
                await self.persistence.save_graph_store(self.graph_store)

                logger.info(
                    f"✅ 自动遗忘完成: 遗忘了 {forgotten_count} 条记忆, "
                    f"清理了 {orphan_nodes} 个孤立节点, {orphan_edges} 条孤立边"
                )
            else:
                logger.info("✅ 自动遗忘完成: 没有需要遗忘的记忆")

            return forgotten_count

        except Exception as e:
            logger.error(f"自动遗忘失败: {e}", exc_info=True)
            return 0

    async def _cleanup_orphan_nodes_and_edges(self) -> tuple[int, int]:
        """
        清理孤立节点和边

        孤立节点：不再属于任何记忆的节点
        孤立边：连接到已删除节点的边

        Returns:
            (清理的孤立节点数, 清理的孤立边数)
        """
        try:
            orphan_nodes_count = 0
            orphan_edges_count = 0

            # 1. 清理孤立节点
            # graph_store.node_to_memories 记录了每个节点属于哪些记忆
            nodes_to_remove = []

            for node_id, memory_ids in list(self.graph_store.node_to_memories.items()):
                # 如果节点不再属于任何记忆，标记为删除
                if not memory_ids:
                    nodes_to_remove.append(node_id)

            # 从图中删除孤立节点
            for node_id in nodes_to_remove:
                if self.graph_store.graph.has_node(node_id):
                    self.graph_store.graph.remove_node(node_id)
                    orphan_nodes_count += 1

                # 从映射中删除
                if node_id in self.graph_store.node_to_memories:
                    del self.graph_store.node_to_memories[node_id]

            # 2. 清理孤立边（指向已删除节点的边）
            edges_to_remove = []

            for source, target, edge_id in self.graph_store.graph.edges(data="edge_id"):
                # 检查边的源节点和目标节点是否还存在于node_to_memories中
                if source not in self.graph_store.node_to_memories or \
                   target not in self.graph_store.node_to_memories:
                    edges_to_remove.append((source, target))

            # 删除孤立边
            for source, target in edges_to_remove:
                try:
                    self.graph_store.graph.remove_edge(source, target)
                    orphan_edges_count += 1
                except Exception as e:
                    logger.debug(f"删除边失败 {source} -> {target}: {e}")

            if orphan_nodes_count > 0 or orphan_edges_count > 0:
                logger.info(
                    f"清理完成: {orphan_nodes_count} 个孤立节点, {orphan_edges_count} 条孤立边"
                )

            return orphan_nodes_count, orphan_edges_count

        except Exception as e:
            logger.error(f"清理孤立节点和边失败: {e}", exc_info=True)
            return 0, 0

    # ==================== 统计与维护 ====================

    def get_statistics(self) -> dict[str, Any]:
        """
        获取记忆系统统计信息

        Returns:
            统计信息字典
        """
        if not self._initialized or not self.graph_store:
            return {}

        stats = self.graph_store.get_statistics()

        # 添加激活度统计
        all_memories = self.graph_store.get_all_memories()
        activation_levels = []
        forgotten_count = 0

        for memory in all_memories:
            if memory.metadata.get("forgotten", False):
                forgotten_count += 1
            else:
                activation_info = memory.metadata.get("activation", {})
                activation_levels.append(activation_info.get("level", 0.0))

        if activation_levels:
            stats["avg_activation"] = sum(activation_levels) / len(activation_levels)
            stats["max_activation"] = max(activation_levels)
        else:
            stats["avg_activation"] = 0.0
            stats["max_activation"] = 0.0

        stats["forgotten_memories"] = forgotten_count
        stats["active_memories"] = stats["total_memories"] - forgotten_count

        return stats

    async def consolidate_memories(
        self,
        similarity_threshold: float = 0.85,
        time_window_hours: float = 24.0,
        max_batch_size: int = 50,
    ) -> dict[str, Any]:
        """
        简化的记忆整理：仅检查需要遗忘的记忆并清理孤立节点和边
        
        功能：
        1. 检查需要遗忘的记忆（低激活度）
        2. 清理孤立节点和边
        
        注意：记忆的创建、合并、关联等操作已由三级记忆系统自动处理

        Args:
            similarity_threshold: （已废弃，保留参数兼容性）
            time_window_hours: （已废弃，保留参数兼容性）
            max_batch_size: （已废弃，保留参数兼容性）

        Returns:
            整理结果
        """
        if not self._initialized:
            await self.initialize()

        try:
            logger.info("🧹 开始记忆整理：检查遗忘 + 清理孤立节点...")

            # 步骤1: 自动遗忘低激活度的记忆
            forgotten_count = await self.auto_forget()

            # 步骤2: 清理孤立节点和边（auto_forget内部已执行，这里再次确保）
            orphan_nodes, orphan_edges = await self._cleanup_orphan_nodes_and_edges()

            result = {
                "forgotten_count": forgotten_count,
                "orphan_nodes_cleaned": orphan_nodes,
                "orphan_edges_cleaned": orphan_edges,
                "message": "记忆整理完成（仅遗忘和清理孤立节点）"
            }

            logger.info(f"✅ 记忆整理完成: {result}")
            return result

        except Exception as e:
            logger.error(f"记忆整理失败: {e}", exc_info=True)
            return {"error": str(e), "forgotten_count": 0}

    async def _consolidate_memories_background(
        self,
        similarity_threshold: float,
        time_window_hours: float,
        max_batch_size: int,
    ) -> None:
        """
        后台整理任务（已简化为调用consolidate_memories）
        
        保留此方法用于向后兼容
        """
        await self.consolidate_memories(
            similarity_threshold=similarity_threshold,
            time_window_hours=time_window_hours,
            max_batch_size=max_batch_size
        )

    # ==================== 以下方法已废弃 ====================
    # 旧的记忆整理逻辑（去重、自动关联等）已由三级记忆系统取代
    # 保留方法签名用于向后兼容，但不再执行复杂操作

    async def auto_link_memories(  # 已废弃
        self,
        time_window_hours: float | None = None,
        max_candidates: int | None = None,
        min_confidence: float | None = None,
    ) -> dict[str, Any]:
        """
        自动关联记忆（已废弃）

        该功能已由三级记忆系统取代。记忆之间的关联现在通过模型自动处理。

        Args:
            time_window_hours: 分析时间窗口（小时）
            max_candidates: 每个记忆最多关联的候选数
            min_confidence: 最低置信度阈值

        Returns:
            空结果（向后兼容）
        """
        logger.warning("auto_link_memories 已废弃，记忆关联由三级记忆系统自动处理")
        return {"checked_count": 0, "linked_count": 0, "deprecated": True}

    async def _find_link_candidates(  # 已废弃
        self,
        memory: Memory,
        exclude_ids: set[str],
        max_results: int = 5,
    ) -> list[Memory]:
        """
        为记忆寻找关联候选（已废弃）

        该功能已由三级记忆系统取代。
        """
        logger.warning("_find_link_candidates 已废弃")
        return []

    async def _analyze_memory_relations(  # 已废弃
        self,
        source_memory: Memory,
        candidate_memories: list[Memory],
        min_confidence: float = 0.7,
    ) -> list[dict[str, Any]]:
        """
        使用LLM分析记忆之间的关系（已废弃）

        该功能已由三级记忆系统取代。

        Args:
            source_memory: 源记忆
            candidate_memories: 候选记忆列表
            min_confidence: 最低置信度

        Returns:
            空列表（向后兼容）
        """
        logger.warning("_analyze_memory_relations 已废弃")
        return []

    def _format_memory_for_llm(self, memory: Memory) -> str:  # 已废弃
        """格式化记忆为LLM可读的文本（已废弃）"""
        logger.warning("_format_memory_for_llm 已废弃")
        return f"记忆ID: {memory.id}"

    async def maintenance(self) -> dict[str, Any]:
        """
        执行维护任务（简化版）

        只包括：
        - 简化的记忆整理（检查遗忘+清理孤立节点）
        - 保存数据

        注意：记忆的创建、合并、关联等操作已由三级记忆系统自动处理

        Returns:
            维护结果
        """
        if not self._initialized:
            await self.initialize()

        try:
            logger.info("🔧 开始执行记忆系统维护...")

            result = {
                "forgotten": 0,
                "orphan_nodes_cleaned": 0,
                "orphan_edges_cleaned": 0,
                "saved": False,
                "total_time": 0,
            }

            start_time = datetime.now()

            # 1. 简化的记忆整理（只检查遗忘和清理孤立节点）
            if getattr(self.config, "consolidation_enabled", False):
                consolidate_result = await self.consolidate_memories()
                result["forgotten"] = consolidate_result.get("forgotten_count", 0)
                result["orphan_nodes_cleaned"] = consolidate_result.get("orphan_nodes_cleaned", 0)
                result["orphan_edges_cleaned"] = consolidate_result.get("orphan_edges_cleaned", 0)

            # 2. 保存数据
            await self.persistence.save_graph_store(self.graph_store)
            result["saved"] = True

            self._last_maintenance = datetime.now()

            # 计算维护耗时
            total_time = (datetime.now() - start_time).total_seconds()
            result["total_time"] = total_time

            logger.info(f"✅ 维护完成 (耗时 {total_time:.2f}s): {result}")
            return result

        except Exception as e:
            logger.error(f"❌ 维护失败: {e}", exc_info=True)
            return {"error": str(e), "total_time": 0}

    async def _lightweight_auto_link_memories(  # 已废弃
        self,
        time_window_hours: float | None = None,
        max_candidates: int | None = None,
        max_memories: int | None = None,
    ) -> dict[str, Any]:
        """
        智能轻量级自动关联记忆（已废弃）

        该功能已由三级记忆系统取代。

        Args:
            time_window_hours: 从配置读取
            max_candidates: 从配置读取
            max_memories: 从配置读取

        Returns:
            空结果（向后兼容）
        """
        logger.warning("_lightweight_auto_link_memories 已废弃")
        return {"checked_count": 0, "linked_count": 0, "deprecated": True}

    async def _batch_analyze_memory_relations(  # 已废弃
        self,
        candidate_pairs: list[tuple[Memory, Memory, float]]
    ) -> list[dict[str, Any]]:
        """
        批量分析记忆关系（已废弃）

        该功能已由三级记忆系统取代。

        Args:
            candidate_pairs: 候选记忆对列表

        Returns:
            空列表（向后兼容）
        """
        logger.warning("_batch_analyze_memory_relations 已废弃")
        return []

    def _start_maintenance_task(self) -> None:
        """
        启动记忆维护后台任务

        直接创建async task，避免使用scheduler阻塞主程序：
        - 记忆整合（合并相似记忆）
        - 自动遗忘低激活度记忆
        - 保存数据

        默认间隔：1小时
        """
        try:
            # 如果已有维护任务，先停止
            if self._maintenance_task and not self._maintenance_task.done():
                self._maintenance_task.cancel()
                logger.info("取消旧的维护任务")

            # 创建新的后台维护任务
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(),
                name="memory_maintenance_loop"
            )

            logger.info(
                f"✅ 记忆维护后台任务已启动 "
                f"(间隔={self._maintenance_interval_hours}小时)"
            )

        except Exception as e:
            logger.error(f"启动维护后台任务失败: {e}", exc_info=True)

    async def _stop_maintenance_task(self) -> None:
        """
        停止记忆维护后台任务
        """
        if not self._maintenance_task or self._maintenance_task.done():
            return

        try:
            self._maintenance_running = False  # 设置停止标志
            self._maintenance_task.cancel()

            try:
                await self._maintenance_task
            except asyncio.CancelledError:
                logger.debug("维护任务已取消")

            logger.info("✅ 记忆维护后台任务已停止")
            self._maintenance_task = None

        except Exception as e:
            logger.error(f"停止维护后台任务失败: {e}", exc_info=True)

    async def _maintenance_loop(self) -> None:
        """
        记忆维护循环

        在后台独立运行，定期执行维护任务，避免阻塞主程序
        """
        self._maintenance_running = True

        try:
            # 首次执行延迟（启动后1小时）
            initial_delay = self._maintenance_interval_hours * 3600
            logger.debug(f"记忆维护任务将在 {initial_delay} 秒后首次执行")

            while self._maintenance_running:
                try:
                    # 使用 asyncio.wait_for 来支持取消
                    await asyncio.wait_for(
                        asyncio.sleep(initial_delay),
                        timeout=float("inf")  # 允许随时取消
                    )

                    # 检查是否仍然需要运行
                    if not self._maintenance_running:
                        break

                    # 执行维护任务（使用try-catch避免崩溃）
                    try:
                        await self.maintenance()
                    except Exception as e:
                        logger.error(f"维护任务执行失败: {e}", exc_info=True)

                    # 后续执行使用相同间隔
                    initial_delay = self._maintenance_interval_hours * 3600

                except asyncio.CancelledError:
                    logger.debug("维护循环被取消")
                    break
                except Exception as e:
                    logger.error(f"维护循环发生异常: {e}", exc_info=True)
                    # 异常后等待较短时间再重试
                    try:
                        await asyncio.sleep(300)  # 5分钟后重试
                    except asyncio.CancelledError:
                        break

        except asyncio.CancelledError:
            logger.debug("维护循环完全退出")
        except Exception as e:
            logger.error(f"维护循环意外结束: {e}", exc_info=True)
        finally:
            self._maintenance_running = False
            logger.debug("维护循环已清理完毕")

    async def _async_save_graph_store(self, operation_name: str = "未知操作") -> None:
        """
        异步保存图存储到磁盘

        此方法设计为在后台任务中执行，包含错误处理

        Args:
            operation_name: 操作名称，用于日志记录
        """
        try:
            # 确保图存储存在且已初始化
            if self.graph_store is None:
                logger.warning(f"图存储未初始化，跳过异步保存: {operation_name}")
                return

            if self.persistence is None:
                logger.warning(f"持久化管理器未初始化，跳过异步保存: {operation_name}")
                return

            await self.persistence.save_graph_store(self.graph_store)
            logger.debug(f"异步保存图数据成功: {operation_name}")
        except Exception as e:
            logger.error(f"异步保存图数据失败 ({operation_name}): {e}", exc_info=True)
            # 可以考虑添加重试机制或者通知机制
