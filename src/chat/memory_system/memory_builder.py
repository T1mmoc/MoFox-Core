# -*- coding: utf-8 -*-
"""
记忆构建模块
从对话流中提取高质量、结构化记忆单元
"""

import re
import time
import orjson
from typing import Dict, List, Optional, Any
from datetime import datetime
from dataclasses import dataclass
from enum import Enum

from src.common.logger import get_logger
from src.llm_models.utils_model import LLMRequest
from src.chat.memory_system.memory_chunk import (
    MemoryChunk, MemoryType, ConfidenceLevel, ImportanceLevel,
    create_memory_chunk
)

logger = get_logger(__name__)


class ExtractionStrategy(Enum):
    """提取策略"""
    LLM_BASED = "llm_based"           # 基于LLM的智能提取
    RULE_BASED = "rule_based"         # 基于规则的提取
    HYBRID = "hybrid"                 # 混合策略


@dataclass
class ExtractionResult:
    """提取结果"""
    memories: List[MemoryChunk]
    confidence_scores: List[float]
    extraction_time: float
    strategy_used: ExtractionStrategy


class MemoryBuilder:
    """记忆构建器"""

    def __init__(self, llm_model: LLMRequest):
        self.llm_model = llm_model
        self.extraction_stats = {
            "total_extractions": 0,
            "successful_extractions": 0,
            "failed_extractions": 0,
            "average_confidence": 0.0
        }

    async def build_memories(
        self,
        conversation_text: str,
        context: Dict[str, Any],
        user_id: str,
        timestamp: float
    ) -> List[MemoryChunk]:
        """从对话中构建记忆"""
        start_time = time.time()

        try:
            logger.debug(f"开始从对话构建记忆，文本长度: {len(conversation_text)}")

            # 预处理文本
            processed_text = self._preprocess_text(conversation_text)

            # 确定提取策略
            strategy = self._determine_extraction_strategy(processed_text, context)

            # 根据策略提取记忆
            if strategy == ExtractionStrategy.LLM_BASED:
                memories = await self._extract_with_llm(processed_text, context, user_id, timestamp)
            elif strategy == ExtractionStrategy.RULE_BASED:
                memories = self._extract_with_rules(processed_text, context, user_id, timestamp)
            else:  # HYBRID
                memories = await self._extract_with_hybrid(processed_text, context, user_id, timestamp)

            # 后处理和验证
            validated_memories = self._validate_and_enhance_memories(memories, context)

            # 更新统计
            extraction_time = time.time() - start_time
            self._update_extraction_stats(len(validated_memories), extraction_time)

            logger.info(f"✅ 成功构建 {len(validated_memories)} 条记忆，耗时 {extraction_time:.2f}秒")
            return validated_memories

        except Exception as e:
            logger.error(f"❌ 记忆构建失败: {e}", exc_info=True)
            self.extraction_stats["failed_extractions"] += 1
            return []

    def _preprocess_text(self, text: str) -> str:
        """预处理文本"""
        # 移除多余的空白字符
        text = re.sub(r'\s+', ' ', text.strip())

        # 移除特殊字符，但保留基本标点
        text = re.sub(r'[^\w\s\u4e00-\u9fff，。！？、；：""''（）【】]', '', text)

        # 截断过长的文本
        if len(text) > 2000:
            text = text[:2000] + "..."

        return text

    def _determine_extraction_strategy(self, text: str, context: Dict[str, Any]) -> ExtractionStrategy:
        """确定提取策略"""
        text_length = len(text)
        has_structured_data = any(key in context for key in ["structured_data", "entities", "keywords"])
        message_type = context.get("message_type", "normal")

        # 短文本使用规则提取
        if text_length < 50:
            return ExtractionStrategy.RULE_BASED

        # 包含结构化数据使用混合策略
        if has_structured_data:
            return ExtractionStrategy.HYBRID

        # 系统消息或命令使用规则提取
        if message_type in ["command", "system"]:
            return ExtractionStrategy.RULE_BASED

        # 默认使用LLM提取
        return ExtractionStrategy.LLM_BASED

    async def _extract_with_llm(
        self,
        text: str,
        context: Dict[str, Any],
        user_id: str,
        timestamp: float
    ) -> List[MemoryChunk]:
        """使用LLM提取记忆"""
        try:
            prompt = self._build_llm_extraction_prompt(text, context)

            response, _ = await self.llm_model.generate_response_async(
                prompt, temperature=0.3
            )

            # 解析LLM响应
            memories = self._parse_llm_response(response, user_id, timestamp, context)

            return memories

        except Exception as e:
            logger.error(f"LLM提取失败: {e}")
            return []

    def _extract_with_rules(
        self,
        text: str,
        context: Dict[str, Any],
        user_id: str,
        timestamp: float
    ) -> List[MemoryChunk]:
        """使用规则提取记忆"""
        memories = []

        # 规则1: 检测个人信息
        personal_info = self._extract_personal_info(text, user_id, timestamp, context)
        memories.extend(personal_info)

        # 规则2: 检测偏好信息
        preferences = self._extract_preferences(text, user_id, timestamp, context)
        memories.extend(preferences)

        # 规则3: 检测事件信息
        events = self._extract_events(text, user_id, timestamp, context)
        memories.extend(events)

        return memories

    async def _extract_with_hybrid(
        self,
        text: str,
        context: Dict[str, Any],
        user_id: str,
        timestamp: float
    ) -> List[MemoryChunk]:
        """混合策略提取记忆"""
        all_memories = []

        # 首先使用规则提取
        rule_memories = self._extract_with_rules(text, context, user_id, timestamp)
        all_memories.extend(rule_memories)

        # 然后使用LLM提取
        llm_memories = await self._extract_with_llm(text, context, user_id, timestamp)

        # 合并和去重
        final_memories = self._merge_hybrid_results(all_memories, llm_memories)

        return final_memories

    def _build_llm_extraction_prompt(self, text: str, context: Dict[str, Any]) -> str:
        """构建LLM提取提示"""
        current_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        chat_id = context.get("chat_id", "unknown")
        message_type = context.get("message_type", "normal")

        prompt = f"""
你是一个专业的记忆提取专家。请从以下对话中主动识别并提取所有可能重要的信息，特别是包含个人事实、事件、偏好、观点等要素的内容。

当前时间: {current_date}
聊天ID: {chat_id}
消息类型: {message_type}

对话内容:
{text}

## 🎯 重点记忆类型识别指南

### 1. **个人事实** (personal_fact) - 高优先级记忆
**包括但不限于：**
- 基本信息：姓名、年龄、职业、学校、专业、工作地点
- 生活状况：住址、电话、邮箱、社交账号
- 身份特征：生日、星座、血型、国籍、语言能力
- 健康信息：身体状况、疾病史、药物过敏、运动习惯
- 家庭情况：家庭成员、婚姻状况、子女信息、宠物信息

**判断标准：** 涉及个人身份和生活的重要信息，都应该记忆

### 2. **事件** (event) - 高优先级记忆
**包括但不限于：**
- 重要时刻：生日聚会、毕业典礼、婚礼、旅行
- 日常活动：上班、上学、约会、看电影、吃饭
- 特殊经历：考试、面试、会议、搬家、购物
- 计划安排：约会、会议、旅行、活动

**判断标准：** 涉及时间地点的具体活动和经历，都应该记忆

### 3. **偏好** (preference) - 高优先级记忆
**包括但不限于：**
- 饮食偏好：喜欢的食物、餐厅、口味、禁忌
- 娱乐喜好：喜欢的电影、音乐、游戏、书籍
- 生活习惯：作息时间、运动方式、购物习惯
- 消费偏好：品牌喜好、价格敏感度、购物场所
- 风格偏好：服装风格、装修风格、颜色喜好

**判断标准：** 任何表达"喜欢"、"不喜欢"、"习惯"、"经常"等偏好的内容，都应该记忆

### 4. **观点** (opinion) - 高优先级记忆
**包括但不限于：**
- 评价看法：对事物的评价、意见、建议
- 价值判断：认为什么重要、什么不重要
- 态度立场：支持、反对、中立的态度
- 感受反馈：对经历的感受、反馈

**判断标准：** 任何表达主观看法和态度的内容，都应该记忆

### 5. **关系** (relationship) - 中等优先级记忆
**包括但不限于：**
- 人际关系：朋友、同事、家人、恋人的关系状态
- 社交互动：与他人的互动、交流、合作
- 群体归属：所属团队、组织、社群

### 6. **情感** (emotion) - 中等优先级记忆
**包括但不限于：**
- 情绪状态：开心、难过、生气、焦虑、兴奋
- 情感变化：情绪的转变、原因和结果

### 7. **目标** (goal) - 中等优先级记忆
**包括但不限于：**
- 计划安排：短期计划、长期目标
- 愿望期待：想要实现的事情、期望的结果

## 📝 记忆提取原则

### ✅ 积极提取原则：
1. **宁可错记，不可遗漏** - 对于可能的个人信息优先记忆
2. **持续追踪** - 相同信息的多次提及要强化记忆
3. **上下文关联** - 结合对话背景理解信息重要性
4. **细节丰富** - 记录具体的细节和描述

### 🕒 时间处理原则（重要）：
1. **绝对时间要求** - 涉及时间的记忆必须使用绝对时间（年月日）
2. **相对时间转换** - 将"明天"、"后天"、"下周"等相对时间转换为具体日期
3. **时间格式规范** - 使用"YYYY-MM-DD"格式记录日期
4. **当前时间参考** - 当前时间：{current_date}，基于此计算相对时间

**相对时间转换示例：**
- "明天" → "2024-09-30"
- "后天" → "2024-10-01"
- "下周" → "2024-10-07"
- "下个月" → "2024-10-01"
- "明年" → "2025-01-01"

### 🎯 重要性等级标准：
- **4分 (关键)**：个人核心信息（姓名、联系方式、重要日期）
- **3分 (高)**：重要偏好、观点、经历事件
- **2分 (一般)**：一般性信息、日常活动、感受表达
- **1分 (低)**：琐碎细节、重复信息、临时状态

### 🔍 置信度标准：
- **4分 (已验证)**：用户明确确认的信息
- **3分 (高)**：用户直接表达的清晰信息
- **2分 (中等)**：需要推理或上下文判断的信息
- **1分 (低)**：模糊或不完整的信息

输出格式要求:
{{
    "memories": [
        {{
            "type": "记忆类型",
            "subject": "主语(通常是用户)",
            "predicate": "谓语(动作/状态)",
            "object": "宾语(对象/属性)",
            "keywords": ["关键词1", "关键词2"],
            "importance": "重要性等级(1-4)",
            "confidence": "置信度(1-4)",
            "reasoning": "提取理由"
        }}
    ]
}}

注意：
1. 只提取确实值得记忆的信息，不要提取琐碎内容
2. 确保提取的信息准确、具体、有价值
3. 使用主谓宾结构确保信息清晰
4. 重要性等级: 1=低, 2=一般, 3=高, 4=关键
5. 置信度: 1=低, 2=中等, 3=高, 4=已验证

## 🚨 时间处理要求（强制）：
- **绝对时间优先**：任何涉及时间的记忆都必须使用绝对日期格式
- **相对时间转换**：遇到"明天"、"后天"、"下周"等相对时间必须转换为具体日期
- **时间格式**：统一使用 "YYYY-MM-DD" 格式
- **计算依据**：基于当前时间 {current_date} 进行转换计算
"""

        return prompt

    def _extract_json_payload(self, response: str) -> Optional[str]:
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

    def _parse_llm_response(
        self,
        response: str,
        user_id: str,
        timestamp: float,
        context: Dict[str, Any]
    ) -> List[MemoryChunk]:
        """解析LLM响应"""
        memories = []

        try:
            # 提取JSON负载
            json_payload = self._extract_json_payload(response)
            if not json_payload:
                logger.error("未在响应中找到有效的JSON负载")
                return memories

            data = orjson.loads(json_payload)
            memory_list = data.get("memories", [])

            for mem_data in memory_list:
                try:
                    # 创建记忆块
                    memory = create_memory_chunk(
                        user_id=user_id,
                        subject=mem_data.get("subject", user_id),
                        predicate=mem_data.get("predicate", ""),
                        obj=mem_data.get("object", ""),
                        memory_type=MemoryType(mem_data.get("type", "contextual")),
                        chat_id=context.get("chat_id"),
                        source_context=mem_data.get("reasoning", ""),
                        importance=ImportanceLevel(mem_data.get("importance", 2)),
                        confidence=ConfidenceLevel(mem_data.get("confidence", 2))
                    )

                    # 添加关键词
                    keywords = mem_data.get("keywords", [])
                    for keyword in keywords:
                        memory.add_keyword(keyword)

                    memories.append(memory)

                except Exception as e:
                    logger.warning(f"解析单个记忆失败: {e}, 数据: {mem_data}")
                    continue

        except Exception as e:
            preview = response[:200] if response else "空响应"
            logger.error(f"解析LLM响应失败: {e}, 响应片段: {preview}")

        return memories

    def _extract_personal_info(
        self,
        text: str,
        user_id: str,
        timestamp: float,
        context: Dict[str, Any]
    ) -> List[MemoryChunk]:
        """提取个人信息"""
        memories = []

        # 常见个人信息模式
        patterns = {
            r"我叫(\w+)": ("is_named", {"name": "$1"}),
            r"我今年(\d+)岁": ("is_age", {"age": "$1"}),
            r"我是(\w+)": ("is_profession", {"profession": "$1"}),
            r"我住在(\w+)": ("lives_in", {"location": "$1"}),
            r"我的电话是(\d+)": ("has_phone", {"phone": "$1"}),
            r"我的邮箱是(\w+@\w+\.\w+)": ("has_email", {"email": "$1"}),
        }

        for pattern, (predicate, obj_template) in patterns.items():
            match = re.search(pattern, text)
            if match:
                obj = obj_template
                for i, group in enumerate(match.groups(), 1):
                    obj = {k: v.replace(f"${i}", group) for k, v in obj.items()}

                memory = create_memory_chunk(
                    user_id=user_id,
                    subject=user_id,
                    predicate=predicate,
                    obj=obj,
                    memory_type=MemoryType.PERSONAL_FACT,
                    chat_id=context.get("chat_id"),
                    importance=ImportanceLevel.HIGH,
                    confidence=ConfidenceLevel.HIGH
                )

                memories.append(memory)

        return memories

    def _extract_preferences(
        self,
        text: str,
        user_id: str,
        timestamp: float,
        context: Dict[str, Any]
    ) -> List[MemoryChunk]:
        """提取偏好信息"""
        memories = []

        # 偏好模式
        preference_patterns = [
            (r"我喜欢(.+)", "likes"),
            (r"我不喜欢(.+)", "dislikes"),
            (r"我爱吃(.+)", "likes_food"),
            (r"我讨厌(.+)", "hates"),
            (r"我最喜欢的(.+)", "favorite_is"),
        ]

        for pattern, predicate in preference_patterns:
            match = re.search(pattern, text)
            if match:
                memory = create_memory_chunk(
                    user_id=user_id,
                    subject=user_id,
                    predicate=predicate,
                    obj=match.group(1),
                    memory_type=MemoryType.PREFERENCE,
                    chat_id=context.get("chat_id"),
                    importance=ImportanceLevel.NORMAL,
                    confidence=ConfidenceLevel.MEDIUM
                )

                memories.append(memory)

        return memories

    def _extract_events(
        self,
        text: str,
        user_id: str,
        timestamp: float,
        context: Dict[str, Any]
    ) -> List[MemoryChunk]:
        """提取事件信息"""
        memories = []

        # 事件关键词
        event_keywords = ["明天", "今天", "昨天", "上周", "下周", "约会", "会议", "活动", "旅行", "生日"]

        if any(keyword in text for keyword in event_keywords):
            memory = create_memory_chunk(
                user_id=user_id,
                subject=user_id,
                predicate="mentioned_event",
                obj={"event_text": text, "timestamp": timestamp},
                memory_type=MemoryType.EVENT,
                chat_id=context.get("chat_id"),
                importance=ImportanceLevel.NORMAL,
                confidence=ConfidenceLevel.MEDIUM
            )

            memories.append(memory)

        return memories

    def _merge_hybrid_results(
        self,
        rule_memories: List[MemoryChunk],
        llm_memories: List[MemoryChunk]
    ) -> List[MemoryChunk]:
        """合并混合策略结果"""
        all_memories = rule_memories.copy()

        # 添加LLM记忆，避免重复
        for llm_memory in llm_memories:
            is_duplicate = False
            for rule_memory in rule_memories:
                if llm_memory.is_similar_to(rule_memory, threshold=0.7):
                    is_duplicate = True
                    # 合并置信度
                    rule_memory.metadata.confidence = ConfidenceLevel(
                        max(rule_memory.metadata.confidence.value, llm_memory.metadata.confidence.value)
                    )
                    break

            if not is_duplicate:
                all_memories.append(llm_memory)

        return all_memories

    def _validate_and_enhance_memories(
        self,
        memories: List[MemoryChunk],
        context: Dict[str, Any]
    ) -> List[MemoryChunk]:
        """验证和增强记忆"""
        validated_memories = []

        for memory in memories:
            # 基本验证
            if not self._validate_memory(memory):
                continue

            # 增强记忆
            enhanced_memory = self._enhance_memory(memory, context)
            validated_memories.append(enhanced_memory)

        return validated_memories

    def _validate_memory(self, memory: MemoryChunk) -> bool:
        """验证记忆块"""
        # 检查基本字段
        if not memory.content.subject or not memory.content.predicate:
            logger.debug(f"记忆块缺少主语或谓语: {memory.memory_id}")
            return False

        # 检查内容长度
        content_length = len(memory.text_content)
        if content_length < 5 or content_length > 500:
            logger.debug(f"记忆块内容长度异常: {content_length}")
            return False

        # 检查置信度
        if memory.metadata.confidence == ConfidenceLevel.LOW:
            logger.debug(f"记忆块置信度过低: {memory.memory_id}")
            return False

        return True

    def _enhance_memory(
        self,
        memory: MemoryChunk,
        context: Dict[str, Any]
    ) -> MemoryChunk:
        """增强记忆块"""
        # 时间规范化处理
        self._normalize_time_in_memory(memory)

        # 添加时间上下文
        if not memory.temporal_context:
            memory.temporal_context = {
                "timestamp": memory.metadata.created_at,
                "timezone": context.get("timezone", "UTC"),
                "day_of_week": datetime.fromtimestamp(memory.metadata.created_at).strftime("%A")
            }

        # 添加情感上下文（如果有）
        if context.get("sentiment"):
            memory.metadata.emotional_context = context["sentiment"]

        # 自动添加标签
        self._auto_tag_memory(memory)

        return memory

    def _normalize_time_in_memory(self, memory: MemoryChunk):
        """规范化记忆中的时间表达"""
        import re
        from datetime import datetime, timedelta

        # 获取当前时间作为参考
        current_time = datetime.fromtimestamp(memory.metadata.created_at)

        # 定义相对时间映射
        relative_time_patterns = {
            r'今天|今日': current_time.strftime('%Y-%m-%d'),
            r'昨天|昨日': (current_time - timedelta(days=1)).strftime('%Y-%m-%d'),
            r'明天|明日': (current_time + timedelta(days=1)).strftime('%Y-%m-%d'),
            r'后天': (current_time + timedelta(days=2)).strftime('%Y-%m-%d'),
            r'大后天': (current_time + timedelta(days=3)).strftime('%Y-%m-%d'),
            r'前天': (current_time - timedelta(days=2)).strftime('%Y-%m-%d'),
            r'大前天': (current_time - timedelta(days=3)).strftime('%Y-%m-%d'),
            r'本周|这周|这星期': current_time.strftime('%Y-%m-%d'),
            r'上周|上星期': (current_time - timedelta(weeks=1)).strftime('%Y-%m-%d'),
            r'下周|下星期': (current_time + timedelta(weeks=1)).strftime('%Y-%m-%d'),
            r'本月|这个月': current_time.strftime('%Y-%m-01'),
            r'上月|上个月': (current_time.replace(day=1) - timedelta(days=1)).strftime('%Y-%m-01'),
            r'下月|下个月': (current_time.replace(day=1) + timedelta(days=32)).replace(day=1).strftime('%Y-%m-01'),
            r'今年|今年': current_time.strftime('%Y'),
            r'去年|上一年': str(current_time.year - 1),
            r'明年|下一年': str(current_time.year + 1),
        }

        # 检查并替换记忆内容中的相对时间
        memory_content = memory.content.description

        # 应用时间规范化
        for pattern, replacement in relative_time_patterns.items():
            memory_content = re.sub(pattern, replacement, memory_content)

        # 更新记忆内容
        memory.content.description = memory_content

        # 如果记忆有对象信息，也进行时间规范化
        if hasattr(memory.content, 'object') and isinstance(memory.content.object, dict):
            obj_str = str(memory.content.object)
            for pattern, replacement in relative_time_patterns.items():
                obj_str = re.sub(pattern, replacement, obj_str)
            try:
                # 尝试解析回字典（如果原来是字典）
                memory.content.object = eval(obj_str) if obj_str.startswith('{') else obj_str
            except Exception:
                memory.content.object = obj_str

        # 记录时间规范化操作
        logger.debug(f"记忆 {memory.memory_id} 已进行时间规范化")

    def _auto_tag_memory(self, memory: MemoryChunk):
        """自动为记忆添加标签"""
        # 基于记忆类型的自动标签
        type_tags = {
            MemoryType.PERSONAL_FACT: ["个人信息", "基本资料"],
            MemoryType.EVENT: ["事件", "日程"],
            MemoryType.PREFERENCE: ["偏好", "喜好"],
            MemoryType.OPINION: ["观点", "态度"],
            MemoryType.RELATIONSHIP: ["关系", "社交"],
            MemoryType.EMOTION: ["情感", "情绪"],
            MemoryType.KNOWLEDGE: ["知识", "信息"],
            MemoryType.SKILL: ["技能", "能力"],
            MemoryType.GOAL: ["目标", "计划"],
            MemoryType.EXPERIENCE: ["经验", "经历"],
        }

        tags = type_tags.get(memory.memory_type, [])
        for tag in tags:
            memory.add_tag(tag)

    def _update_extraction_stats(self, success_count: int, extraction_time: float):
        """更新提取统计"""
        self.extraction_stats["total_extractions"] += 1
        self.extraction_stats["successful_extractions"] += success_count
        self.extraction_stats["failed_extractions"] += max(0, 1 - success_count)

        # 更新平均置信度
        if self.extraction_stats["successful_extractions"] > 0:
            total_confidence = self.extraction_stats["average_confidence"] * (self.extraction_stats["successful_extractions"] - success_count)
            # 假设新记忆的平均置信度为0.8
            total_confidence += 0.8 * success_count
            self.extraction_stats["average_confidence"] = total_confidence / self.extraction_stats["successful_extractions"]

    def get_extraction_stats(self) -> Dict[str, Any]:
        """获取提取统计信息"""
        return self.extraction_stats.copy()

    def reset_stats(self):
        """重置统计信息"""
        self.extraction_stats = {
            "total_extractions": 0,
            "successful_extractions": 0,
            "failed_extractions": 0,
            "average_confidence": 0.0
        }