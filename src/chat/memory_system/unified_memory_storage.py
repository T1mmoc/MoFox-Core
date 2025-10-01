# -*- coding: utf-8 -*-
"""
统一记忆存储系统
简化后的记忆存储，整合向量存储和元数据索引
"""

import os
import time
import orjson
import asyncio
import threading
from typing import Dict, List, Optional, Tuple, Set, Any
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from src.common.logger import get_logger
from src.llm_models.utils_model import LLMRequest
from src.config.config import model_config, global_config
from src.chat.memory_system.memory_chunk import MemoryChunk
from src.chat.memory_system.memory_forgetting_engine import MemoryForgettingEngine

logger = get_logger(__name__)

# 尝试导入FAISS
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    logger.warning("FAISS not available, using simple vector storage")


@dataclass
class UnifiedStorageConfig:
    """统一存储配置"""
    # 向量存储配置
    dimension: int = 1024
    similarity_threshold: float = 0.8
    storage_path: str = "data/unified_memory"

    # 性能配置
    cache_size_limit: int = 10000
    auto_save_interval: int = 50
    search_limit: int = 20
    enable_compression: bool = True

    # 遗忘配置
    enable_forgetting: bool = True
    forgetting_check_interval: int = 24  # 小时


class UnifiedMemoryStorage:
    """统一记忆存储系统"""

    def __init__(self, config: Optional[UnifiedStorageConfig] = None):
        self.config = config or UnifiedStorageConfig()

        # 存储路径
        self.storage_path = Path(self.config.storage_path)
        self.storage_path.mkdir(parents=True, exist_ok=True)

        # 向量索引
        self.vector_index = None
        self.memory_id_to_index: Dict[str, int] = {}
        self.index_to_memory_id: Dict[int, str] = {}

        # 内存缓存
        self.memory_cache: Dict[str, MemoryChunk] = {}
        self.vector_cache: Dict[str, np.ndarray] = {}

        # 元数据索引（简化版）
        self.keyword_index: Dict[str, Set[str]] = {}  # keyword -> memory_id set
        self.type_index: Dict[str, Set[str]] = {}      # type -> memory_id set
        self.user_index: Dict[str, Set[str]] = {}      # user_id -> memory_id set

        # 遗忘引擎
        self.forgetting_engine: Optional[MemoryForgettingEngine] = None
        if self.config.enable_forgetting:
            self.forgetting_engine = MemoryForgettingEngine()

        # 统计信息
        self.stats = {
            "total_memories": 0,
            "total_vectors": 0,
            "cache_size": 0,
            "last_save_time": 0.0,
            "total_searches": 0,
            "total_stores": 0,
            "forgetting_stats": {}
        }

        # 线程锁
        self._lock = threading.RLock()
        self._operation_count = 0

        # 嵌入模型
        self.embedding_model: Optional[LLMRequest] = None

        # 初始化
        self._initialize_storage()

    def _initialize_storage(self):
        """初始化存储系统"""
        try:
            # 初始化向量索引
            if FAISS_AVAILABLE:
                self.vector_index = faiss.IndexFlatIP(self.config.dimension)
                logger.info(f"FAISS向量索引初始化完成，维度: {self.config.dimension}")
            else:
                # 简单向量存储
                self.vector_index = {}
                logger.info("使用简单向量存储（FAISS不可用）")

            # 尝试加载现有数据
            self._load_storage()

            logger.info(f"统一记忆存储初始化完成，当前记忆数: {len(self.memory_cache)}")

        except Exception as e:
            logger.error(f"存储系统初始化失败: {e}", exc_info=True)

    def set_embedding_model(self, model: LLMRequest):
        """设置嵌入模型"""
        self.embedding_model = model

    async def _generate_embedding(self, text: str) -> Optional[np.ndarray]:
        """生成文本的向量表示"""
        if not self.embedding_model:
            logger.warning("未设置嵌入模型，无法生成向量")
            return None

        try:
            # 使用嵌入模型生成向量
            embedding, _ = await self.embedding_model.get_embedding(text)
            
            if embedding is None:
                logger.warning(f"嵌入模型返回空向量，文本: {text[:50]}...")
                return None
            
            # 转换为numpy数组
            embedding_array = np.array(embedding, dtype=np.float32)
            
            # 归一化向量
            norm = np.linalg.norm(embedding_array)
            if norm > 0:
                embedding_array = embedding_array / norm

            return embedding_array

        except Exception as e:
            logger.error(f"生成向量失败: {e}")
            return None

    def _add_to_keyword_index(self, memory: MemoryChunk):
        """添加到关键词索引"""
        for keyword in memory.keywords:
            if keyword not in self.keyword_index:
                self.keyword_index[keyword] = set()
            self.keyword_index[keyword].add(memory.memory_id)

    def _add_to_type_index(self, memory: MemoryChunk):
        """添加到类型索引"""
        memory_type = memory.memory_type.value
        if memory_type not in self.type_index:
            self.type_index[memory_type] = set()
        self.type_index[memory_type].add(memory.memory_id)

    def _add_to_user_index(self, memory: MemoryChunk):
        """添加到用户索引"""
        user_id = memory.user_id
        if user_id not in self.user_index:
            self.user_index[user_id] = set()
        self.user_index[user_id].add(memory.memory_id)

    def _remove_from_indexes(self, memory: MemoryChunk):
        """从所有索引中移除记忆"""
        memory_id = memory.memory_id

        # 从关键词索引移除
        for keyword, memory_ids in self.keyword_index.items():
            memory_ids.discard(memory_id)
            if not memory_ids:
                del self.keyword_index[keyword]

        # 从类型索引移除
        memory_type = memory.memory_type.value
        if memory_type in self.type_index:
            self.type_index[memory_type].discard(memory_id)
            if not self.type_index[memory_type]:
                del self.type_index[memory_type]

        # 从用户索引移除
        if memory.user_id in self.user_index:
            self.user_index[memory.user_id].discard(memory_id)
            if not self.user_index[memory.user_id]:
                del self.user_index[memory.user_id]

    async def store_memories(self, memories: List[MemoryChunk]) -> int:
        """存储记忆列表"""
        if not memories:
            return 0

        stored_count = 0

        with self._lock:
            for memory in memories:
                try:
                    # 生成向量
                    vector = None
                    if memory.display and memory.display.strip():
                        vector = await self._generate_embedding(memory.display)
                    elif memory.text_content and memory.text_content.strip():
                        vector = await self._generate_embedding(memory.text_content)

                    # 存储到缓存
                    self.memory_cache[memory.memory_id] = memory
                    if vector is not None:
                        self.vector_cache[memory.memory_id] = vector

                        # 添加到向量索引
                        if FAISS_AVAILABLE:
                            index_id = self.vector_index.ntotal
                            self.vector_index.add(vector.reshape(1, -1))
                            self.memory_id_to_index[memory.memory_id] = index_id
                            self.index_to_memory_id[index_id] = memory.memory_id
                        else:
                            # 简单存储
                            self.vector_index[memory.memory_id] = vector

                    # 更新元数据索引
                    self._add_to_keyword_index(memory)
                    self._add_to_type_index(memory)
                    self._add_to_user_index(memory)

                    stored_count += 1

                except Exception as e:
                    logger.error(f"存储记忆 {memory.memory_id[:8]} 失败: {e}")
                    continue

        # 更新统计
        self.stats["total_memories"] = len(self.memory_cache)
        self.stats["total_vectors"] = len(self.vector_cache)
        self.stats["total_stores"] += stored_count

        # 自动保存
        self._operation_count += stored_count
        if self._operation_count >= self.config.auto_save_interval:
            await self._save_storage()
            self._operation_count = 0

        logger.debug(f"成功存储 {stored_count}/{len(memories)} 条记忆")
        return stored_count

    async def search_similar_memories(
        self,
        query_text: str,
        limit: int = 10,
        scope_id: Optional[str] = None,
        filters: Optional[Dict[str, Any]] = None
    ) -> List[Tuple[str, float]]:
        """搜索相似记忆"""
        if not query_text or not self.vector_cache:
            return []

        # 生成查询向量
        query_vector = await self._generate_embedding(query_text)
        if query_vector is None:
            return []

        try:
            results = []

            if FAISS_AVAILABLE and self.vector_index.ntotal > 0:
                # 使用FAISS搜索
                query_vector = query_vector.reshape(1, -1)
                scores, indices = self.vector_index.search(
                    query_vector,
                    min(limit, self.vector_index.ntotal)
                )

                for score, idx in zip(scores[0], indices[0]):
                    if idx >= 0 and score >= self.config.similarity_threshold:
                        memory_id = self.index_to_memory_id.get(idx)
                        if memory_id and memory_id in self.memory_cache:
                            # 应用过滤器
                            if self._apply_filters(self.memory_cache[memory_id], filters):
                                results.append((memory_id, float(score)))

            else:
                # 简单余弦相似度搜索
                for memory_id, vector in self.vector_cache.items():
                    if memory_id not in self.memory_cache:
                        continue

                    # 计算余弦相似度
                    similarity = np.dot(query_vector, vector)
                    if similarity >= self.config.similarity_threshold:
                        # 应用过滤器
                        if self._apply_filters(self.memory_cache[memory_id], filters):
                            results.append((memory_id, float(similarity)))

            # 排序并限制结果
            results.sort(key=lambda x: x[1], reverse=True)
            results = results[:limit]

            self.stats["total_searches"] += 1
            return results

        except Exception as e:
            logger.error(f"搜索相似记忆失败: {e}")
            return []

    def _apply_filters(self, memory: MemoryChunk, filters: Optional[Dict[str, Any]]) -> bool:
        """应用搜索过滤器"""
        if not filters:
            return True

        # 用户过滤器
        if "user_id" in filters and memory.user_id != filters["user_id"]:
            return False

        # 类型过滤器
        if "memory_types" in filters and memory.memory_type.value not in filters["memory_types"]:
            return False

        # 关键词过滤器
        if "keywords" in filters:
            memory_keywords = set(k.lower() for k in memory.keywords)
            filter_keywords = set(k.lower() for k in filters["keywords"])
            if not memory_keywords.intersection(filter_keywords):
                return False

        # 重要性过滤器
        if "min_importance" in filters and memory.metadata.importance.value < filters["min_importance"]:
            return False

        return True

    def get_memory_by_id(self, memory_id: str) -> Optional[MemoryChunk]:
        """根据ID获取记忆"""
        return self.memory_cache.get(memory_id)

    def get_memories_by_filters(self, filters: Dict[str, Any], limit: int = 50) -> List[MemoryChunk]:
        """根据过滤器获取记忆"""
        results = []

        for memory in self.memory_cache.values():
            if self._apply_filters(memory, filters):
                results.append(memory)
                if len(results) >= limit:
                    break

        return results

    async def forget_memories(self, memory_ids: List[str]) -> int:
        """遗忘指定的记忆"""
        if not memory_ids:
            return 0

        forgotten_count = 0

        with self._lock:
            for memory_id in memory_ids:
                try:
                    memory = self.memory_cache.get(memory_id)
                    if not memory:
                        continue

                    # 从向量索引移除
                    if FAISS_AVAILABLE and memory_id in self.memory_id_to_index:
                        # FAISS不支持直接删除，这里简化处理
                        # 在实际使用中，可能需要重建索引
                        logger.debug(f"FAISS索引删除 {memory_id} (需要重建索引)")
                    elif memory_id in self.vector_index:
                        del self.vector_index[memory_id]

                    # 从缓存移除
                    self.memory_cache.pop(memory_id, None)
                    self.vector_cache.pop(memory_id, None)

                    # 从索引移除
                    self._remove_from_indexes(memory)

                    forgotten_count += 1

                except Exception as e:
                    logger.error(f"遗忘记忆 {memory_id[:8]} 失败: {e}")
                    continue

        # 更新统计
        self.stats["total_memories"] = len(self.memory_cache)
        self.stats["total_vectors"] = len(self.vector_cache)

        logger.info(f"成功遗忘 {forgotten_count}/{len(memory_ids)} 条记忆")
        return forgotten_count

    async def perform_forgetting_check(self) -> Dict[str, Any]:
        """执行遗忘检查"""
        if not self.forgetting_engine:
            return {"error": "遗忘引擎未启用"}

        try:
            # 执行遗忘检查
            result = await self.forgetting_engine.perform_forgetting_check(list(self.memory_cache.values()))

            # 遗忘标记的记忆
            forgetting_ids = result["normal_forgetting"] + result["force_forgetting"]
            if forgetting_ids:
                forgotten_count = await self.forget_memories(forgetting_ids)
                result["forgotten_count"] = forgotten_count

            # 更新统计
            self.stats["forgetting_stats"] = self.forgetting_engine.get_forgetting_stats()

            return result

        except Exception as e:
            logger.error(f"执行遗忘检查失败: {e}")
            return {"error": str(e)}

    def _load_storage(self):
        """加载存储数据"""
        try:
            # 加载记忆缓存
            memory_file = self.storage_path / "memory_cache.json"
            if memory_file.exists():
                with open(memory_file, 'rb') as f:
                    memory_data = orjson.loads(f.read())
                    for memory_id, memory_dict in memory_data.items():
                        self.memory_cache[memory_id] = MemoryChunk.from_dict(memory_dict)

            # 加载向量缓存（如果启用压缩）
            if not self.config.enable_compression:
                vector_file = self.storage_path / "vectors.npz"
                if vector_file.exists():
                    vectors = np.load(vector_file)
                    self.vector_cache = {
                        memory_id: vectors[memory_id]
                        for memory_id in vectors.files
                        if memory_id in self.memory_cache
                    }

            # 重建向量索引
            if FAISS_AVAILABLE and self.vector_cache:
                logger.info("重建FAISS向量索引...")
                vectors = []
                memory_ids = []

                for memory_id, vector in self.vector_cache.items():
                    vectors.append(vector)
                    memory_ids.append(memory_id)

                if vectors:
                    vectors_array = np.vstack(vectors)
                    self.vector_index.reset()
                    self.vector_index.add(vectors_array)

                    # 重建映射
                    for idx, memory_id in enumerate(memory_ids):
                        self.memory_id_to_index[memory_id] = idx
                        self.index_to_memory_id[idx] = memory_id

            logger.info(f"存储数据加载完成，记忆数: {len(self.memory_cache)}")

        except Exception as e:
            logger.warning(f"加载存储数据失败: {e}")

    async def _save_storage(self):
        """保存存储数据"""
        try:
            start_time = time.time()

            # 保存记忆缓存
            memory_data = {
                memory_id: memory.to_dict()
                for memory_id, memory in self.memory_cache.items()
            }

            memory_file = self.storage_path / "memory_cache.json"
            with open(memory_file, 'wb') as f:
                f.write(orjson.dumps(memory_data, option=orjson.OPT_INDENT_2))

            # 保存向量缓存（如果启用压缩）
            if not self.config.enable_compression and self.vector_cache:
                vector_file = self.storage_path / "vectors.npz"
                np.savez_compressed(vector_file, **self.vector_cache)

            save_time = time.time() - start_time
            self.stats["last_save_time"] = time.time()

            logger.debug(f"存储数据保存完成，耗时: {save_time:.3f}s")

        except Exception as e:
            logger.error(f"保存存储数据失败: {e}")

    async def save_storage(self):
        """手动保存存储数据"""
        await self._save_storage()

    def get_storage_stats(self) -> Dict[str, Any]:
        """获取存储统计信息"""
        stats = self.stats.copy()
        stats.update({
            "cache_size": len(self.memory_cache),
            "vector_count": len(self.vector_cache),
            "keyword_index_size": len(self.keyword_index),
            "type_index_size": len(self.type_index),
            "user_index_size": len(self.user_index),
            "config": {
                "dimension": self.config.dimension,
                "similarity_threshold": self.config.similarity_threshold,
                "enable_forgetting": self.config.enable_forgetting
            }
        })
        return stats

    async def cleanup(self):
        """清理存储系统"""
        try:
            logger.info("开始清理统一记忆存储...")

            # 保存数据
            await self._save_storage()

            # 清空缓存
            self.memory_cache.clear()
            self.vector_cache.clear()
            self.keyword_index.clear()
            self.type_index.clear()
            self.user_index.clear()

            # 重置索引
            if FAISS_AVAILABLE:
                self.vector_index.reset()

            self.memory_id_to_index.clear()
            self.index_to_memory_id.clear()

            logger.info("统一记忆存储清理完成")

        except Exception as e:
            logger.error(f"清理存储系统失败: {e}")


# 创建全局存储实例
unified_memory_storage: Optional[UnifiedMemoryStorage] = None


def get_unified_memory_storage() -> Optional[UnifiedMemoryStorage]:
    """获取统一存储实例"""
    return unified_memory_storage


async def initialize_unified_memory_storage(config: Optional[UnifiedStorageConfig] = None) -> UnifiedMemoryStorage:
    """初始化统一记忆存储"""
    global unified_memory_storage

    if unified_memory_storage is None:
        unified_memory_storage = UnifiedMemoryStorage(config)

        # 设置嵌入模型
        from src.llm_models.utils_model import LLMRequest
        from src.config.config import model_config

        try:
            embedding_task = getattr(model_config.model_task_config, "embedding", None)
            if embedding_task:
                unified_memory_storage.set_embedding_model(
                    LLMRequest(model_set=embedding_task, request_type="memory.embedding")
                )
        except Exception as e:
            logger.warning(f"设置嵌入模型失败: {e}")

    return unified_memory_storage