# -*- coding: utf-8 -*-
"""
增强型精准记忆系统核心模块
基于文档设计的高效记忆构建、存储与召回优化系统
"""

import asyncio
import time
import orjson
import re
from typing import Dict, List, Optional, Set, Any, TYPE_CHECKING
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict
from enum import Enum

from src.common.logger import get_logger
from src.llm_models.utils_model import LLMRequest
from src.config.config import model_config, global_config
from src.chat.memory_system.memory_chunk import MemoryChunk
from src.chat.memory_system.memory_builder import MemoryBuilder
from src.chat.memory_system.memory_fusion import MemoryFusionEngine
from src.chat.memory_system.vector_storage import VectorStorageManager, VectorStorageConfig
from src.chat.memory_system.metadata_index import MetadataIndexManager
from src.chat.memory_system.multi_stage_retrieval import MultiStageRetrieval, RetrievalConfig

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger(__name__)


class MemorySystemStatus(Enum):
    """记忆系统状态"""
    INITIALIZING = "initializing"
    READY = "ready"
    BUILDING = "building"
    RETRIEVING = "retrieving"
    ERROR = "error"


@dataclass
class MemorySystemConfig:
    """记忆系统配置"""
    # 记忆构建配置
    min_memory_length: int = 10
    max_memory_length: int = 500
    memory_value_threshold: float = 0.7
    min_build_interval_seconds: float = 300.0

    # 向量存储配置
    vector_dimension: int = 768
    similarity_threshold: float = 0.8

    # 召回配置
    coarse_recall_limit: int = 50
    fine_recall_limit: int = 10
    final_recall_limit: int = 5

    # 融合配置
    fusion_similarity_threshold: float = 0.85
    deduplication_window: timedelta = timedelta(hours=24)

    @classmethod
    def from_global_config(cls):
        """从全局配置创建配置实例"""

        return cls(
            # 记忆构建配置
            min_memory_length=global_config.memory.min_memory_length,
            max_memory_length=global_config.memory.max_memory_length,
            memory_value_threshold=global_config.memory.memory_value_threshold,
            min_build_interval_seconds=getattr(global_config.memory, "memory_build_interval", 300.0),

            # 向量存储配置
            vector_dimension=global_config.memory.vector_dimension,
            similarity_threshold=global_config.memory.vector_similarity_threshold,

            # 召回配置
            coarse_recall_limit=global_config.memory.metadata_filter_limit,
            fine_recall_limit=global_config.memory.final_result_limit,
            final_recall_limit=global_config.memory.final_result_limit,

            # 融合配置
            fusion_similarity_threshold=global_config.memory.fusion_similarity_threshold,
            deduplication_window=timedelta(hours=global_config.memory.deduplication_window_hours)
        )


class EnhancedMemorySystem:
    """增强型精准记忆系统核心类"""

    def __init__(
        self,
        llm_model: Optional[LLMRequest] = None,
        config: Optional[MemorySystemConfig] = None
    ):
        self.config = config or MemorySystemConfig.from_global_config()
        self.llm_model = llm_model
        self.status = MemorySystemStatus.INITIALIZING

        # 核心组件
        self.memory_builder: MemoryBuilder = None
        self.fusion_engine: MemoryFusionEngine = None
        self.vector_storage: VectorStorageManager = None
        self.metadata_index: MetadataIndexManager = None
        self.retrieval_system: MultiStageRetrieval = None

        # LLM模型
        self.value_assessment_model: LLMRequest = None
        self.memory_extraction_model: LLMRequest = None

        # 统计信息
        self.total_memories = 0
        self.last_build_time = None
        self.last_retrieval_time = None

        # 构建节流记录
        self._last_memory_build_times: Dict[str, float] = {}

        logger.info("EnhancedMemorySystem 初始化开始")

    async def initialize(self):
        """异步初始化记忆系统"""
        try:
            logger.info("正在初始化增强型记忆系统...")

            # 初始化LLM模型
            task_config = (
                self.llm_model.model_for_task
                if self.llm_model is not None
                else model_config.model_task_config.utils_small
            )

            self.value_assessment_model = LLMRequest(
                model_set=task_config,
                request_type="memory.value_assessment"
            )

            self.memory_extraction_model = LLMRequest(
                model_set=task_config,
                request_type="memory.extraction"
            )

            # 初始化核心组件
            self.memory_builder = MemoryBuilder(self.memory_extraction_model)
            self.fusion_engine = MemoryFusionEngine(self.config.fusion_similarity_threshold)
            # 创建向量存储配置
            vector_config = VectorStorageConfig(
                dimension=self.config.vector_dimension,
                similarity_threshold=self.config.similarity_threshold
            )
            self.vector_storage = VectorStorageManager(vector_config)
            self.metadata_index = MetadataIndexManager()
            # 创建检索配置
            retrieval_config = RetrievalConfig(
                metadata_filter_limit=self.config.coarse_recall_limit,
                vector_search_limit=self.config.fine_recall_limit,
                final_result_limit=self.config.final_recall_limit
            )
            self.retrieval_system = MultiStageRetrieval(retrieval_config)

            # 加载持久化数据
            await self.vector_storage.load_storage()
            await self.metadata_index.load_index()

            self.status = MemorySystemStatus.READY
            logger.info("✅ 增强型记忆系统初始化完成")

        except Exception as e:
            self.status = MemorySystemStatus.ERROR
            logger.error(f"❌ 记忆系统初始化失败: {e}", exc_info=True)
            raise

    async def retrieve_memories_for_building(
        self,
        query_text: str,
        user_id: str,
        context: Optional[Dict[str, Any]] = None,
        limit: int = 5
    ) -> List[MemoryChunk]:
        """在构建记忆时检索相关记忆，允许在BUILDING状态下进行检索

        Args:
            query_text: 查询文本
            user_id: 用户ID
            context: 上下文信息
            limit: 返回结果数量限制

        Returns:
            相关记忆列表
        """
        if self.status not in [MemorySystemStatus.READY, MemorySystemStatus.BUILDING]:
            logger.warning(f"记忆系统状态不允许检索: {self.status.value}")
            return []

        try:
            # 临时切换到检索状态
            original_status = self.status
            self.status = MemorySystemStatus.RETRIEVING

            # 执行检索
            memories = await self.vector_storage.search_similar_memories(
                query_text=query_text,
                user_id=user_id,
                limit=limit
            )

            # 恢复原始状态
            self.status = original_status

            return memories

        except Exception as e:
            logger.error(f"构建过程中检索记忆失败: {e}", exc_info=True)
            return []

    async def build_memory_from_conversation(
        self,
        conversation_text: str,
        context: Dict[str, Any],
        user_id: str,
        timestamp: Optional[float] = None
    ) -> List[MemoryChunk]:
        """从对话中构建记忆

        Args:
            conversation_text: 对话文本
            context: 上下文信息（包括用户信息、群组信息等）
            user_id: 用户ID
            timestamp: 时间戳，默认为当前时间

        Returns:
            构建的记忆块列表
        """
        if self.status not in [MemorySystemStatus.READY, MemorySystemStatus.BUILDING]:
            raise RuntimeError("记忆系统未就绪")

        original_status = self.status
        self.status = MemorySystemStatus.BUILDING
        start_time = time.time()

        build_scope_key: Optional[str] = None
        build_marker_time: Optional[float] = None

        try:
            normalized_context = self._normalize_context(context, user_id, timestamp)

            build_scope_key = self._get_build_scope_key(normalized_context, user_id)
            min_interval = max(0.0, getattr(self.config, "min_build_interval_seconds", 0.0))
            current_time = time.time()

            if build_scope_key and min_interval > 0:
                last_time = self._last_memory_build_times.get(build_scope_key)
                if last_time and (current_time - last_time) < min_interval:
                    remaining = min_interval - (current_time - last_time)
                    logger.info(
                        "距离上次记忆构建间隔不足，跳过此次构建 | key=%s | 剩余%.2f秒",
                        build_scope_key,
                        remaining,
                    )
                    self.status = MemorySystemStatus.READY
                    return []

                build_marker_time = current_time
                self._last_memory_build_times[build_scope_key] = current_time

            conversation_text = await self._resolve_conversation_context(conversation_text, normalized_context)

            logger.debug(f"开始为用户 {user_id} 构建记忆，文本长度: {len(conversation_text)}")

            # 1. 信息价值评估
            value_score = await self._assess_information_value(conversation_text, normalized_context)

            if value_score < self.config.memory_value_threshold:
                logger.info(f"信息价值评分 {value_score:.2f} 低于阈值，跳过记忆构建")
                self.status = original_status
                return []

            # 2. 构建记忆块
            memory_chunks = await self.memory_builder.build_memories(
                conversation_text,
                normalized_context,
                user_id,
                timestamp or time.time()
            )

            if not memory_chunks:
                logger.debug("未提取到有效记忆块")
                self.status = original_status
                return []

            # 3. 记忆融合与去重
            fused_chunks = await self.fusion_engine.fuse_memories(memory_chunks)

            # 4. 存储记忆
            await self._store_memories(fused_chunks)

            # 5. 更新统计
            self.total_memories += len(fused_chunks)
            self.last_build_time = time.time()
            if build_scope_key:
                self._last_memory_build_times[build_scope_key] = self.last_build_time

            build_time = time.time() - start_time
            logger.info(f"✅ 为用户 {user_id} 构建了 {len(fused_chunks)} 条记忆，耗时 {build_time:.2f}秒")

            self.status = original_status
            return fused_chunks

        except Exception as e:
            if build_scope_key and build_marker_time is not None:
                recorded_time = self._last_memory_build_times.get(build_scope_key)
                if recorded_time == build_marker_time:
                    self._last_memory_build_times.pop(build_scope_key, None)
            self.status = MemorySystemStatus.ERROR
            logger.error(f"❌ 记忆构建失败: {e}", exc_info=True)
            raise

    async def process_conversation_memory(
        self,
        conversation_text: str,
        context: Dict[str, Any],
        user_id: str,
        timestamp: Optional[float] = None
    ) -> Dict[str, Any]:
        """对外暴露的对话记忆处理接口，兼容旧调用方式"""
        start_time = time.time()

        try:
            normalized_context = self._normalize_context(context, user_id, timestamp)

            memories = await self.build_memory_from_conversation(
                conversation_text=conversation_text,
                context=normalized_context,
                user_id=user_id,
                timestamp=timestamp
            )

            processing_time = time.time() - start_time
            memory_count = len(memories)

            return {
                "success": True,
                "created_memories": memories,
                "memory_count": memory_count,
                "processing_time": processing_time,
                "status": self.status.value
            }

        except Exception as e:
            processing_time = time.time() - start_time
            logger.error(f"对话记忆处理失败: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e),
                "processing_time": processing_time,
                "status": self.status.value
            }

    async def retrieve_relevant_memories(
        self,
        query_text: Optional[str] = None,
        user_id: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None,
        limit: int = 5,
        **kwargs
    ) -> List[MemoryChunk]:
        """检索相关记忆，兼容 query/query_text 参数形式"""
        if self.status != MemorySystemStatus.READY:
            raise RuntimeError("记忆系统未就绪")

        query_text = query_text or kwargs.get("query")
        if not query_text:
            raise ValueError("query_text 或 query 参数不能为空")

        context = context or {}
        user_id = user_id or kwargs.get("user_id")

        self.status = MemorySystemStatus.RETRIEVING
        start_time = time.time()

        try:
            normalized_context = self._normalize_context(context, user_id, None)

            candidate_memories = list(self.vector_storage.memory_cache.values())
            if user_id:
                candidate_memories = [m for m in candidate_memories if m.user_id == user_id]

            if not candidate_memories:
                self.status = MemorySystemStatus.READY
                self.last_retrieval_time = time.time()
                logger.debug(f"未找到用户 {user_id} 的候选记忆")
                return []

            scored_memories = []
            for memory in candidate_memories:
                score = self._compute_memory_score(query_text, memory, normalized_context)
                if score > 0:
                    scored_memories.append((memory, score))

            if not scored_memories:
                # 如果所有分数为0，返回最近的记忆作为降级策略
                candidate_memories.sort(key=lambda m: m.metadata.last_accessed, reverse=True)
                scored_memories = [(memory, 0.0) for memory in candidate_memories[:limit]]
            else:
                scored_memories.sort(key=lambda item: item[1], reverse=True)

            top_memories = [memory for memory, _ in scored_memories[:limit]]

            # 更新访问信息和缓存
            for memory, score in scored_memories[:limit]:
                memory.update_access()
                memory.update_relevance(score)

                cache_entry = self.metadata_index.memory_metadata_cache.get(memory.memory_id)
                if cache_entry is not None:
                    cache_entry["last_accessed"] = memory.metadata.last_accessed
                    cache_entry["access_count"] = memory.metadata.access_count
                    cache_entry["relevance_score"] = memory.metadata.relevance_score

            retrieval_time = time.time() - start_time
            logger.info(
                f"✅ 为用户 {user_id or 'unknown'} 检索到 {len(top_memories)} 条相关记忆，耗时 {retrieval_time:.3f}秒"
            )

            self.last_retrieval_time = time.time()
            self.status = MemorySystemStatus.READY

            return top_memories

        except Exception as e:
            self.status = MemorySystemStatus.ERROR
            logger.error(f"❌ 记忆检索失败: {e}", exc_info=True)
            raise

    @staticmethod
    def _extract_json_payload(response: str) -> Optional[str]:
        """从模型响应中提取JSON部分，兼容Markdown代码块等格式"""
        if not response:
            return None

        stripped = response.strip()

        # 优先处理Markdown代码块格式 ```json ... ```
        code_block_match = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.IGNORECASE | re.DOTALL)
        if code_block_match:
            candidate = code_block_match.group(1).strip()
            if candidate:
                return candidate

        # 回退到查找第一个 JSON 对象的大括号范围
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            return stripped[start:end + 1].strip()

        return stripped if stripped.startswith("{") and stripped.endswith("}") else None

    def _normalize_context(
        self,
        raw_context: Optional[Dict[str, Any]],
        user_id: Optional[str],
        timestamp: Optional[float]
    ) -> Dict[str, Any]:
        """标准化上下文，确保必备字段存在且格式正确"""
        context: Dict[str, Any] = {}
        if raw_context:
            try:
                context = dict(raw_context)
            except Exception:
                context = dict(raw_context or {})

        # 基础字段
        context["user_id"] = context.get("user_id") or user_id or "unknown"
        context["timestamp"] = context.get("timestamp") or timestamp or time.time()
        context["message_type"] = context.get("message_type") or "normal"
        context["platform"] = context.get("platform") or context.get("source_platform") or "unknown"

        # 标准化关键词类型
        keywords = context.get("keywords")
        if keywords is None:
            context["keywords"] = []
        elif isinstance(keywords, tuple):
            context["keywords"] = list(keywords)
        elif not isinstance(keywords, list):
            context["keywords"] = [str(keywords)] if keywords else []

        # 统一 stream_id
        stream_id = context.get("stream_id") or context.get("stram_id")
        if not stream_id:
            potential = context.get("chat_id") or context.get("session_id")
            if isinstance(potential, str) and potential:
                stream_id = potential
        if stream_id:
            context["stream_id"] = stream_id

        # chat_id 兜底
        context["chat_id"] = context.get("chat_id") or context.get("stream_id") or f"session_{context['user_id']}"

        # 历史窗口配置
        window_candidate = (
            context.get("history_limit")
            or context.get("history_window")
            or context.get("memory_history_limit")
        )
        if window_candidate is not None:
            try:
                context["history_limit"] = int(window_candidate)
            except (TypeError, ValueError):
                context.pop("history_limit", None)

        return context

    async def _resolve_conversation_context(self, fallback_text: str, context: Optional[Dict[str, Any]]) -> str:
        """使用 stream_id 历史消息和相关记忆充实对话文本，默认回退到传入文本"""
        if not context:
            return fallback_text

        user_id = context.get("user_id")
        stream_id = context.get("stream_id") or context.get("stram_id")

        # 优先使用 stream_id 获取历史消息
        if stream_id:
            try:
                from src.chat.message_receive.chat_stream import get_chat_manager

                chat_manager = get_chat_manager()
                chat_stream = chat_manager.get_stream(stream_id)
                if chat_stream and hasattr(chat_stream, "context_manager"):
                    history_limit = self._determine_history_limit(context)
                    messages = chat_stream.context_manager.get_messages(limit=history_limit, include_unread=True)
                    if messages:
                        transcript = self._format_history_messages(messages)
                        if transcript:
                            cleaned_fallback = (fallback_text or "").strip()
                            if cleaned_fallback and cleaned_fallback not in transcript:
                                transcript = f"{transcript}\n[当前消息] {cleaned_fallback}"

                            logger.debug(
                                "使用 stream_id=%s 的历史消息构建记忆上下文，消息数=%d，限制=%d",
                                stream_id,
                                len(messages),
                                history_limit,
                            )
                            return transcript
                        else:
                            logger.debug(f"stream_id={stream_id} 历史消息格式化失败")
                    else:
                        logger.debug(f"stream_id={stream_id} 未获取到历史消息")
                else:
                    logger.debug(f"未找到 stream_id={stream_id} 对应的聊天流或上下文管理器")
            except Exception as exc:
                logger.warning(f"获取 stream_id={stream_id} 的历史消息失败: {exc}", exc_info=True)

        # 如果无法获取历史消息，尝试检索相关记忆作为上下文
        if user_id and fallback_text:
            try:
                relevant_memories = await self.retrieve_memories_for_building(
                    query_text=fallback_text,
                    user_id=user_id,
                    context=context,
                    limit=3
                )

                if relevant_memories:
                    memory_contexts = []
                    for memory in relevant_memories:
                        memory_contexts.append(f"[历史记忆] {memory.text_content}")

                    memory_transcript = "\n".join(memory_contexts)
                    cleaned_fallback = (fallback_text or "").strip()
                    if cleaned_fallback and cleaned_fallback not in memory_transcript:
                        memory_transcript = f"{memory_transcript}\n[当前消息] {cleaned_fallback}"

                    logger.debug(
                        "使用检索到的历史记忆构建记忆上下文，记忆数=%d，用户=%s",
                        len(relevant_memories),
                        user_id
                    )
                    return memory_transcript

            except Exception as exc:
                logger.warning(f"检索历史记忆作为上下文失败: {exc}", exc_info=True)

        # 回退到传入文本
        return fallback_text

    def _get_build_scope_key(self, context: Dict[str, Any], user_id: Optional[str]) -> Optional[str]:
        """确定用于节流控制的记忆构建作用域"""
        stream_id = context.get("stream_id")
        if stream_id:
            return f"stream::{stream_id}"

        chat_id = context.get("chat_id")
        if chat_id:
            return f"chat::{chat_id}"

        if user_id:
            return f"user::{user_id}"

        return None

    def _determine_history_limit(self, context: Dict[str, Any]) -> int:
        """确定历史消息获取数量，限制在30-50之间"""
        default_limit = 40
        candidate = (
            context.get("history_limit")
            or context.get("history_window")
            or context.get("memory_history_limit")
        )

        if isinstance(candidate, str):
            try:
                candidate = int(candidate)
            except ValueError:
                candidate = None

        if isinstance(candidate, int):
            history_limit = max(30, min(50, candidate))
        else:
            history_limit = default_limit

        return history_limit

    def _format_history_messages(self, messages: List["DatabaseMessages"]) -> Optional[str]:
        """将历史消息格式化为可供LLM处理的多轮对话文本"""
        if not messages:
            return None

        lines: List[str] = []
        for msg in messages:
            try:
                content = getattr(msg, "processed_plain_text", None) or getattr(msg, "display_message", None)
                if not content:
                    continue

                content = re.sub(r"\s+", " ", str(content).strip())
                if not content:
                    continue

                speaker = None
                if hasattr(msg, "user_info") and msg.user_info:
                    speaker = (
                        getattr(msg.user_info, "user_nickname", None)
                        or getattr(msg.user_info, "user_cardname", None)
                        or getattr(msg.user_info, "user_id", None)
                    )
                speaker = speaker or getattr(msg, "user_nickname", None) or getattr(msg, "user_id", None) or "用户"

                timestamp_value = getattr(msg, "time", None) or 0.0
                try:
                    timestamp_dt = datetime.fromtimestamp(float(timestamp_value)) if timestamp_value else datetime.now()
                except (TypeError, ValueError, OSError):
                    timestamp_dt = datetime.now()

                timestamp_str = timestamp_dt.strftime("%Y-%m-%d %H:%M:%S")
                lines.append(f"[{timestamp_str}] {speaker}: {content}")

            except Exception as message_exc:
                logger.debug(f"格式化历史消息失败: {message_exc}")
                continue

        return "\n".join(lines) if lines else None

    async def _assess_information_value(self, text: str, context: Dict[str, Any]) -> float:
        """评估信息价值

        Args:
            text: 文本内容
            context: 上下文信息

        Returns:
            价值评分 (0.0-1.0)
        """
        try:
            # 构建评估提示
            prompt = f"""
请评估以下对话内容的信息价值，重点识别包含个人事实、事件、偏好、观点等重要信息的内容。

## 🎯 价值评估重点标准：

### 高价值信息 (0.7-1.0分)：
1. **个人事实** (personal_fact)：包含姓名、年龄、职业、联系方式、住址、健康状况、家庭情况等个人信息
2. **重要事件** (event)：约会、会议、旅行、考试、面试、搬家等重要活动或经历
3. **明确偏好** (preference)：表达喜欢/不喜欢的食物、电影、音乐、品牌、生活习惯等偏好信息
4. **观点态度** (opinion)：对事物的评价、看法、建议、态度等主观观点
5. **核心关系** (relationship)：重要的朋友、家人、同事等人际关系信息

### 中等价值信息 (0.4-0.7分)：
1. **情感表达**：当前情绪状态、心情变化
2. **日常活动**：常规的工作、学习、生活安排
3. **一般兴趣**：兴趣爱好、休闲活动
4. **短期计划**：即将进行的安排和计划

### 低价值信息 (0.0-0.4分)：
1. **寒暄问候**：简单的打招呼、礼貌用语
2. **重复信息**：已经多次提到的相同内容
3. **临时状态**：短暂的情绪波动、临时想法
4. **无关内容**：与用户画像建立无关的信息

对话内容：
{text}

上下文信息：
- 用户ID: {context.get('user_id', 'unknown')}
- 消息类型: {context.get('message_type', 'unknown')}
- 时间: {datetime.fromtimestamp(context.get('timestamp', time.time()))}

## 📋 评估要求：

### 积极识别原则：
- **宁可高估，不可低估** - 对于可能的个人信息给予较高评估
- **重点关注** - 特别注意包含 personal_fact、event、preference、opinion 的内容
- **细节丰富** - 具体的细节信息比笼统的描述更有价值
- **建立画像** - 有助于建立完整用户画像的信息更有价值

### 评分指导：
- **0.9-1.0**：核心个人信息（姓名、联系方式、重要偏好）
- **0.7-0.8**：重要的个人事实、观点、事件经历
- **0.5-0.6**：一般性偏好、日常活动、情感表达
- **0.3-0.4**：简单的兴趣表达、临时状态
- **0.0-0.2**：寒暄问候、重复内容、无关信息

请以JSON格式输出评估结果：
{{
    "value_score": 0.0到1.0之间的数值,
    "reasoning": "评估理由，包含具体识别到的信息类型",
    "key_factors": ["关键因素1", "关键因素2"],
    "detected_types": ["personal_fact", "preference", "opinion", "event", "relationship", "emotion", "goal"]
}}
"""

            response, _ = await self.value_assessment_model.generate_response_async(
                prompt, temperature=0.3
            )

            # 解析响应
            try:
                payload = self._extract_json_payload(response)
                if not payload:
                    raise ValueError("未在响应中找到有效的JSON负载")

                result = orjson.loads(payload)
                value_score = float(result.get("value_score", 0.0))
                reasoning = result.get("reasoning", "")
                key_factors = result.get("key_factors", [])

                logger.info(f"信息价值评估: {value_score:.2f}, 理由: {reasoning}")
                if key_factors:
                    logger.info(f"关键因素: {', '.join(key_factors)}")

                return max(0.0, min(1.0, value_score))

            except (orjson.JSONDecodeError, ValueError) as e:
                preview = response[:200].replace('\n', ' ')
                logger.warning(f"解析价值评估响应失败: {e}, 响应片段: {preview}")
                return 0.5  # 默认中等价值

        except Exception as e:
            logger.error(f"信息价值评估失败: {e}", exc_info=True)
            return 0.5  # 默认中等价值

    async def _store_memories(self, memory_chunks: List[MemoryChunk]):
        """存储记忆块到各个存储系统"""
        if not memory_chunks:
            return

        # 并行存储到向量数据库和元数据索引
        storage_tasks = []

        # 向量存储
        storage_tasks.append(self.vector_storage.store_memories(memory_chunks))

        # 元数据索引
        storage_tasks.append(self.metadata_index.index_memories(memory_chunks))

        # 等待所有存储任务完成
        await asyncio.gather(*storage_tasks, return_exceptions=True)

        logger.debug(f"成功存储 {len(memory_chunks)} 条记忆到各个存储系统")

    def get_system_stats(self) -> Dict[str, Any]:
        """获取系统统计信息"""
        return {
            "status": self.status.value,
            "total_memories": self.total_memories,
            "last_build_time": self.last_build_time,
            "last_retrieval_time": self.last_retrieval_time,
            "config": asdict(self.config)
        }

    def _compute_memory_score(self, query_text: str, memory: MemoryChunk, context: Dict[str, Any]) -> float:
        """根据查询和上下文为记忆计算匹配分数"""
        tokens_query = self._tokenize_text(query_text)
        tokens_memory = self._tokenize_text(memory.text_content)

        if tokens_query and tokens_memory:
            base_score = len(tokens_query & tokens_memory) / len(tokens_query | tokens_memory)
        else:
            base_score = 0.0

        context_keywords = context.get("keywords") or []
        keyword_overlap = 0.0
        if context_keywords:
            memory_keywords = set(k.lower() for k in memory.keywords)
            keyword_overlap = len(memory_keywords & set(k.lower() for k in context_keywords)) / max(len(context_keywords), 1)

        importance_boost = (memory.metadata.importance.value - 1) / 3 * 0.1
        confidence_boost = (memory.metadata.confidence.value - 1) / 3 * 0.05

        final_score = base_score * 0.7 + keyword_overlap * 0.15 + importance_boost + confidence_boost
        return max(0.0, min(1.0, final_score))

    def _tokenize_text(self, text: str) -> Set[str]:
        """简单分词，兼容中英文"""
        if not text:
            return set()

        tokens = re.findall(r"[\w\u4e00-\u9fa5]+", text.lower())
        return {token for token in tokens if len(token) > 1}

    async def maintenance(self):
        """系统维护操作"""
        try:
            logger.info("开始记忆系统维护...")

            # 向量存储优化
            await self.vector_storage.optimize_storage()

            # 元数据索引优化
            await self.metadata_index.optimize_index()

            # 记忆融合引擎维护
            await self.fusion_engine.maintenance()

            logger.info("✅ 记忆系统维护完成")

        except Exception as e:
            logger.error(f"❌ 记忆系统维护失败: {e}", exc_info=True)

    async def shutdown(self):
        """关闭系统"""
        try:
            logger.info("正在关闭增强型记忆系统...")

            # 保存持久化数据
            await self.vector_storage.save_storage()
            await self.metadata_index.save_index()

            logger.info("✅ 增强型记忆系统已关闭")

        except Exception as e:
            logger.error(f"❌ 记忆系统关闭失败: {e}", exc_info=True)


# 全局记忆系统实例
enhanced_memory_system: EnhancedMemorySystem = None


def get_enhanced_memory_system() -> EnhancedMemorySystem:
    """获取全局记忆系统实例"""
    global enhanced_memory_system
    if enhanced_memory_system is None:
        enhanced_memory_system = EnhancedMemorySystem()
    return enhanced_memory_system


async def initialize_enhanced_memory_system():
    """初始化全局记忆系统"""
    global enhanced_memory_system
    if enhanced_memory_system is None:
        enhanced_memory_system = EnhancedMemorySystem()
    await enhanced_memory_system.initialize()
    return enhanced_memory_system