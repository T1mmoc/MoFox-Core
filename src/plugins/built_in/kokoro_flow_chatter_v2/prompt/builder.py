"""
Kokoro Flow Chatter V2 - 提示词构建器

使用项目统一的 Prompt 管理系统构建提示词
"""

import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from src.chat.utils.prompt import global_prompt_manager
from src.common.logger import get_logger
from src.config.config import global_config

from ..models import EventType, MentalLogEntry, SessionStatus
from ..session import KokoroSession

# 导入模板注册（确保模板被注册到 global_prompt_manager）
from . import prompts as _  # noqa: F401
from .prompts import PROMPT_NAMES

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream

logger = get_logger("kfc_v2_prompt_builder")


class PromptBuilder:
    """
    提示词构建器
    
    使用统一的 Prompt 管理系统构建提示词：
    1. 构建活动流（从 mental_log 生成线性叙事）
    2. 构建当前情况描述
    3. 使用 global_prompt_manager 格式化最终提示词
    """
    
    def __init__(self):
        self._context_builder = None
    
    async def build_prompt(
        self,
        session: KokoroSession,
        user_name: str,
        situation_type: str = "new_message",
        chat_stream: Optional["ChatStream"] = None,
        available_actions: Optional[dict] = None,
        extra_context: Optional[dict] = None,
    ) -> str:
        """
        构建完整的提示词
        
        Args:
            session: 会话对象
            user_name: 用户名称
            situation_type: 情况类型 (new_message/reply_in_time/reply_late/timeout/proactive)
            chat_stream: 聊天流对象
            available_actions: 可用动作字典
            extra_context: 额外上下文（如 trigger_reason）
            
        Returns:
            完整的提示词
        """
        extra_context = extra_context or {}
        
        # 1. 构建人设块
        persona_block = self._build_persona_block()
        
        # 2. 构建关系块
        relation_block = await self._build_relation_block(user_name, chat_stream)
        
        # 3. 构建活动流
        activity_stream = await self._build_activity_stream(session, user_name)
        
        # 4. 构建当前情况
        current_situation = await self._build_current_situation(
            session, user_name, situation_type, extra_context
        )
        
        # 5. 构建可用动作
        actions_block = self._build_actions_block(available_actions)
        
        # 6. 获取输出格式
        output_format = await self._get_output_format()
        
        # 7. 使用统一的 prompt 管理系统格式化
        prompt = await global_prompt_manager.format_prompt(
            PROMPT_NAMES["main"],
            user_name=user_name,
            persona_block=persona_block,
            relation_block=relation_block,
            activity_stream=activity_stream or "（这是你们第一次聊天）",
            current_situation=current_situation,
            available_actions=actions_block,
            output_format=output_format,
        )
        
        return prompt
    
    def _build_persona_block(self) -> str:
        """构建人设块"""
        if global_config is None:
            return "你是一个温暖、真诚的人。"
        
        personality = global_config.personality
        parts = []
        
        if personality.personality_core:
            parts.append(personality.personality_core)
        
        if personality.personality_side:
            parts.append(personality.personality_side)
        
        if personality.identity:
            parts.append(personality.identity)
        
        if personality.reply_style:
            parts.append(f"\n### 说话风格\n{personality.reply_style}")
        
        return "\n\n".join(parts) if parts else "你是一个温暖、真诚的人。"
    
    async def _build_relation_block(
        self,
        user_name: str,
        chat_stream: Optional["ChatStream"],
    ) -> str:
        """构建关系块"""
        if not chat_stream:
            return f"你与 {user_name} 还不太熟悉，这是早期的交流阶段。"
        
        try:
            # 延迟导入上下文构建器
            if self._context_builder is None:
                from ..context_builder import KFCContextBuilder
                self._context_builder = KFCContextBuilder
            
            builder = self._context_builder(chat_stream)
            context_data = await builder.build_all_context(
                sender_name=user_name,
                target_message="",
                context=None,
            )
            
            relation_info = context_data.get("relation_info", "")
            if relation_info:
                return relation_info
            
        except Exception as e:
            logger.warning(f"构建关系块失败: {e}")
        
        return f"你与 {user_name} 还不太熟悉，这是早期的交流阶段。"
    
    async def _build_activity_stream(
        self,
        session: KokoroSession,
        user_name: str,
    ) -> str:
        """
        构建活动流
        
        将 mental_log 中的事件按时间顺序转换为线性叙事
        使用统一的 prompt 模板
        """
        entries = session.get_recent_entries(limit=30)
        if not entries:
            return ""
        
        parts = []
        
        for entry in entries:
            part = await self._format_entry(entry, user_name)
            if part:
                parts.append(part)
        
        return "\n\n".join(parts)
    
    async def _format_entry(self, entry: MentalLogEntry, user_name: str) -> str:
        """格式化单个活动日志条目"""
        
        if entry.event_type == EventType.USER_MESSAGE:
            # 用户消息
            result = await global_prompt_manager.format_prompt(
                PROMPT_NAMES["entry_user_message"],
                time=entry.get_time_str(),
                user_name=entry.user_name or user_name,
                content=entry.content,
            )
            
            # 如果有回复状态元数据，添加说明
            reply_status = entry.metadata.get("reply_status")
            if reply_status == "in_time":
                elapsed = entry.metadata.get("elapsed_seconds", 0) / 60
                max_wait = entry.metadata.get("max_wait_seconds", 0) / 60
                result += await global_prompt_manager.format_prompt(
                    PROMPT_NAMES["entry_reply_in_time"],
                    elapsed_minutes=elapsed,
                    max_wait_minutes=max_wait,
                )
            elif reply_status == "late":
                elapsed = entry.metadata.get("elapsed_seconds", 0) / 60
                max_wait = entry.metadata.get("max_wait_seconds", 0) / 60
                result += await global_prompt_manager.format_prompt(
                    PROMPT_NAMES["entry_reply_late"],
                    elapsed_minutes=elapsed,
                    max_wait_minutes=max_wait,
                )
            
            return result
        
        elif entry.event_type == EventType.BOT_PLANNING:
            # Bot 规划
            actions_desc = self._format_actions(entry.actions)
            
            if entry.max_wait_seconds > 0:
                return await global_prompt_manager.format_prompt(
                    PROMPT_NAMES["entry_bot_planning"],
                    thought=entry.thought or "（没有特别的想法）",
                    actions_description=actions_desc,
                    expected_reaction=entry.expected_reaction or "随便怎么回应都行",
                    max_wait_minutes=entry.max_wait_seconds / 60,
                )
            else:
                return await global_prompt_manager.format_prompt(
                    PROMPT_NAMES["entry_bot_planning_no_wait"],
                    thought=entry.thought or "（没有特别的想法）",
                    actions_description=actions_desc,
                )
        
        elif entry.event_type == EventType.WAITING_UPDATE:
            # 等待中心理变化
            return await global_prompt_manager.format_prompt(
                PROMPT_NAMES["entry_waiting_update"],
                elapsed_minutes=entry.elapsed_seconds / 60,
                waiting_thought=entry.waiting_thought or "还在等...",
            )
        
        elif entry.event_type == EventType.PROACTIVE_TRIGGER:
            # 主动思考触发
            silence = entry.metadata.get("silence_duration", "一段时间")
            return await global_prompt_manager.format_prompt(
                PROMPT_NAMES["entry_proactive_trigger"],
                silence_duration=silence,
            )
        
        return ""
    
    def _format_actions(self, actions: list[dict]) -> str:
        """格式化动作列表为可读描述"""
        if not actions:
            return "（无动作）"
        
        descriptions = []
        for action in actions:
            action_type = action.get("type", "unknown")
            
            if action_type == "reply":
                content = action.get("content", "")
                if len(content) > 50:
                    content = content[:50] + "..."
                descriptions.append(f"发送消息：「{content}」")
            elif action_type == "poke_user":
                descriptions.append("戳了戳对方")
            elif action_type == "do_nothing":
                descriptions.append("什么都不做")
            elif action_type == "send_emoji":
                emoji = action.get("emoji", "")
                descriptions.append(f"发送表情：{emoji}")
            else:
                descriptions.append(f"执行动作：{action_type}")
        
        return "、".join(descriptions)
    
    async def _build_current_situation(
        self,
        session: KokoroSession,
        user_name: str,
        situation_type: str,
        extra_context: dict,
    ) -> str:
        """构建当前情况描述"""
        current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M")
        
        if situation_type == "new_message":
            return await global_prompt_manager.format_prompt(
                PROMPT_NAMES["situation_new_message"],
                current_time=current_time,
                user_name=user_name,
            )
        
        elif situation_type == "reply_in_time":
            elapsed = session.waiting_config.get_elapsed_seconds()
            max_wait = session.waiting_config.max_wait_seconds
            return await global_prompt_manager.format_prompt(
                PROMPT_NAMES["situation_reply_in_time"],
                current_time=current_time,
                user_name=user_name,
                elapsed_minutes=elapsed / 60,
                max_wait_minutes=max_wait / 60,
            )
        
        elif situation_type == "reply_late":
            elapsed = session.waiting_config.get_elapsed_seconds()
            max_wait = session.waiting_config.max_wait_seconds
            return await global_prompt_manager.format_prompt(
                PROMPT_NAMES["situation_reply_late"],
                current_time=current_time,
                user_name=user_name,
                elapsed_minutes=elapsed / 60,
                max_wait_minutes=max_wait / 60,
            )
        
        elif situation_type == "timeout":
            elapsed = session.waiting_config.get_elapsed_seconds()
            max_wait = session.waiting_config.max_wait_seconds
            expected = session.waiting_config.expected_reaction
            return await global_prompt_manager.format_prompt(
                PROMPT_NAMES["situation_timeout"],
                current_time=current_time,
                user_name=user_name,
                elapsed_minutes=elapsed / 60,
                max_wait_minutes=max_wait / 60,
                expected_reaction=expected or "对方能回复点什么",
            )
        
        elif situation_type == "proactive":
            silence = extra_context.get("silence_duration", "一段时间")
            trigger_reason = extra_context.get("trigger_reason", "")
            return await global_prompt_manager.format_prompt(
                PROMPT_NAMES["situation_proactive"],
                current_time=current_time,
                user_name=user_name,
                silence_duration=silence,
                trigger_reason=trigger_reason,
            )
        
        # 默认使用 new_message
        return await global_prompt_manager.format_prompt(
            PROMPT_NAMES["situation_new_message"],
            current_time=current_time,
            user_name=user_name,
        )
    
    def _build_actions_block(self, available_actions: Optional[dict]) -> str:
        """构建可用动作块"""
        if not available_actions:
            return self._get_default_actions_block()
        
        lines = []
        for name, info in available_actions.items():
            desc = getattr(info, "description", "") or f"执行 {name}"
            lines.append(f"- `{name}`: {desc}")
        
        return "\n".join(lines) if lines else self._get_default_actions_block()
    
    def _get_default_actions_block(self) -> str:
        """获取默认的动作列表"""
        return """- `reply`: 发送文字消息（参数：content）
- `poke_user`: 戳一戳对方
- `do_nothing`: 什么都不做"""
    
    async def _get_output_format(self) -> str:
        """获取输出格式模板"""
        try:
            prompt = await global_prompt_manager.get_prompt_async(
                PROMPT_NAMES["output_format"]
            )
            return prompt.template
        except KeyError:
            # 如果模板未注册，返回默认格式
            return """请用 JSON 格式回复：
{
    "thought": "你的想法",
    "actions": [{"type": "reply", "content": "你的回复"}],
    "expected_reaction": "期待的反应",
    "max_wait_seconds": 300
}"""


# 全局单例
_prompt_builder: Optional[PromptBuilder] = None


def get_prompt_builder() -> PromptBuilder:
    """获取全局提示词构建器"""
    global _prompt_builder
    if _prompt_builder is None:
        _prompt_builder = PromptBuilder()
    return _prompt_builder
