# -*- coding: utf-8 -*-
"""
记忆激活器
记忆系统的激活器组件
"""

import difflib
import orjson
import time
from typing import List, Dict, Optional
from datetime import datetime

from json_repair import repair_json
from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.chat.utils.prompt import Prompt, global_prompt_manager
from src.chat.memory_system.memory_manager import memory_manager, MemoryResult

logger = get_logger("memory_activator")


def get_keywords_from_json(json_str) -> List:
    """
    从JSON字符串中提取关键词列表

    Args:
        json_str: JSON格式的字符串

    Returns:
        List[str]: 关键词列表
    """
    try:
        # 使用repair_json修复JSON格式
        fixed_json = repair_json(json_str)

        # 如果repair_json返回的是字符串，需要解析为Python对象
        result = orjson.loads(fixed_json) if isinstance(fixed_json, str) else fixed_json
        return result.get("keywords", [])
    except Exception as e:
        logger.error(f"解析关键词JSON失败: {e}")
        return []


def init_prompt():
    # --- Memory Activator Prompt ---
    memory_activator_prompt = """
    你是一个记忆分析器，你需要根据以下信息来进行记忆检索

    以下是一段聊天记录，请根据这些信息，总结出几个关键词作为记忆检索的触发词

    聊天记录:
    {obs_info_text}

    用户想要回复的消息:
    {target_message}

    历史关键词（请避免重复提取这些关键词）：
    {cached_keywords}

    请输出一个json格式，包含以下字段：
    {{
        "keywords": ["关键词1", "关键词2", "关键词3",......]
    }}

    不要输出其他多余内容，只输出json格式就好
    """

    Prompt(memory_activator_prompt, "memory_activator_prompt")


class MemoryActivator:
    """记忆激活器"""

    def __init__(self):
        self.key_words_model = LLMRequest(
            model_set=model_config.model_task_config.utils_small,
            request_type="memory.activator",
        )

        self.running_memory = []
        self.cached_keywords = set()  # 用于缓存历史关键词
        self.last_memory_query_time = 0  # 上次查询记忆的时间

    async def activate_memory_with_chat_history(self, target_message, chat_history_prompt) -> List[Dict]:
        """
        激活记忆
        """
        # 如果记忆系统被禁用，直接返回空列表
        if not global_config.memory.enable_memory:
            return []

        # 将缓存的关键词转换为字符串，用于prompt
        cached_keywords_str = ", ".join(self.cached_keywords) if self.cached_keywords else "暂无历史关键词"

        prompt = await global_prompt_manager.format_prompt(
            "memory_activator_prompt",
            obs_info_text=chat_history_prompt,
            target_message=target_message,
            cached_keywords=cached_keywords_str,
        )

        # 生成关键词
        response, (reasoning_content, model_name, _) = await self.key_words_model.generate_response_async(
            prompt, temperature=0.5
        )
        keywords = list(get_keywords_from_json(response))

        # 更新关键词缓存
        if keywords:
            # 限制缓存大小，最多保留10个关键词
            if len(self.cached_keywords) > 10:
                # 转换为列表，移除最早的关键词
                cached_list = list(self.cached_keywords)
                self.cached_keywords = set(cached_list[-8:])

            # 添加新的关键词到缓存
            self.cached_keywords.update(keywords)

        logger.debug(f"记忆关键词: {self.cached_keywords}")

        # 使用记忆系统获取相关记忆
        memory_results = await self._query_unified_memory(keywords, target_message)

        # 处理和记忆结果
        if memory_results:
            for result in memory_results:
                # 检查是否已存在相似内容的记忆
                exists = any(
                    m["content"] == result.content or
                    difflib.SequenceMatcher(None, m["content"], result.content).ratio() >= 0.7
                    for m in self.running_memory
                )
                if not exists:
                    memory_entry = {
                        "topic": result.memory_type,
                        "content": result.content,
                        "timestamp": datetime.fromtimestamp(result.timestamp).isoformat(),
                        "duration": 1,
                        "confidence": result.confidence,
                        "importance": result.importance,
                        "source": result.source,
                        "relevance_score": result.relevance_score  # 添加相关度评分
                    }
                    self.running_memory.append(memory_entry)
                    logger.debug(f"添加新记忆: {result.memory_type} - {result.content}")

        # 激活时，所有已有记忆的duration+1，达到3则移除
        for m in self.running_memory[:]:
            m["duration"] = m.get("duration", 1) + 1
        self.running_memory = [m for m in self.running_memory if m["duration"] < 3]

        # 限制同时加载的记忆条数，最多保留最后5条
        if len(self.running_memory) > 5:
            self.running_memory = self.running_memory[-5:]

        return self.running_memory

    async def _query_unified_memory(self, keywords: List[str], query_text: str) -> List[MemoryResult]:
        """查询统一记忆系统"""
        try:
            # 使用记忆系统
            from src.chat.memory_system.memory_system import get_memory_system

            memory_system = get_memory_system()
            if not memory_system or memory_system.status.value != "ready":
                logger.warning("记忆系统未就绪")
                return []

            # 构建查询上下文
            context = {
                "keywords": keywords,
                "query_intent": "conversation_response"
            }

            # 查询记忆
            memories = await memory_system.retrieve_relevant_memories(
                query_text=query_text,
                user_id="global",  # 使用全局作用域
                context=context,
                limit=5
            )

            # 转换为 MemoryResult 格式
            memory_results = []
            for memory in memories:
                result = MemoryResult(
                    content=memory.display,
                    memory_type=memory.memory_type.value,
                    confidence=memory.metadata.confidence.value,
                    importance=memory.metadata.importance.value,
                    timestamp=memory.metadata.created_at,
                    source="unified_memory",
                    relevance_score=memory.metadata.relevance_score
                )
                memory_results.append(result)

            logger.debug(f"统一记忆查询返回 {len(memory_results)} 条结果")
            return memory_results

        except Exception as e:
            logger.error(f"查询统一记忆失败: {e}")
            return []

    async def get_instant_memory(self, target_message: str, chat_id: str) -> Optional[str]:
        """
        获取即时记忆 - 兼容原有接口（使用统一存储）
        """
        try:
            # 使用统一存储系统获取相关记忆
            from src.chat.memory_system.memory_system import get_memory_system

            memory_system = get_memory_system()
            if not memory_system or memory_system.status.value != "ready":
                return None

            context = {
                "query_intent": "instant_response",
                "chat_id": chat_id
            }

            memories = await memory_system.retrieve_relevant_memories(
                query_text=target_message,
                user_id="global",
                context=context,
                limit=1
            )

            if memories:
                return memories[0].display

            return None

        except Exception as e:
            logger.error(f"获取即时记忆失败: {e}")
            return None

    def clear_cache(self):
        """清除缓存"""
        self.cached_keywords.clear()
        self.running_memory.clear()
        logger.debug("记忆激活器缓存已清除")


# 创建全局实例
memory_activator = MemoryActivator()

# 兼容性别名
enhanced_memory_activator = memory_activator

init_prompt()