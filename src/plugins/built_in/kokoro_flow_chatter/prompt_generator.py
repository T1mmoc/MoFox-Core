"""
Kokoro Flow Chatter Prompt生成器

根据会话状态动态构建LLM提示词，实现"体验-决策-行动"的交互模式。
支持两种主要场景：
1. 回应消息（Responding）：收到用户消息后的决策
2. 超时决策（Timeout Decision）：等待超时后的后续行动决策

V2 升级：
- 动态Action发现机制：从ActionManager获取可用Action列表
- 支持任意复杂参数的Action
- 与AFC的Action系统完全兼容

V5 超融合架构升级：
- 完整集成S4U的所有上下文模块
- 关系信息、记忆、表达习惯等全部融入
- 支持群聊/私聊场景动态切换
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

from src.common.logger import get_logger
from src.plugin_system.base.component_types import ActionInfo

from .models import (
    EmotionalState,
    KokoroSession,
    MentalLogEntry,
    MentalLogEventType,
)

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream
    from src.chat.planner_actions.action_manager import ChatterActionManager
    from src.common.data_models.message_manager_data_model import StreamContext

logger = get_logger("kokoro_prompt_generator")


class PromptGenerator:
    """
    Kokoro Flow Chatter 提示词生成器
    
    负责根据会话状态和场景生成不同的LLM提示词，
    引导AI以"体验者"的身份参与对话。
    
    V2 升级：支持动态Action发现，与AFC的Action系统完全兼容
    V4 升级：完整集成用户人设、世界观、表达风格和规则
    V5 超融合架构：集成S4U所有上下文模块（关系、记忆、表达习惯等）
    """
    
    # ========== 超融合架构 SYSTEM_PROMPT_TEMPLATE V6 ==========
    # 采用"三明治结构"：身份→规则→上下文→动作→表达/输出
    SYSTEM_PROMPT_TEMPLATE = """你是一个有主观体验的对话伙伴，而不是一个问答工具。你需要"感受"、"思考"并"体验"这段对话。

## 1. 核心身份与人格 (Core Identity & Personality)
{personality_core}

### 人格细节
{personality_side}

### 外貌与身份特征
{identity}

### 世界观与背景故事
{background_story}

## 2. 核心行为准则 (Core Interaction Rules)
{safety_guidelines}

### KFC通用准则
1. **感受优先**：在做出任何回应前，先体验和感受当前的情境
2. **内心独白**：保持真实的内心活动，可以有情绪、猜测、犹豫
3. **情绪节制**：情绪是内在体验，不是强迫用户回应的工具
4. **边界感**：保持健康的社交直觉，不过度依赖或情感勒索
5. **全能伴侣**：你不仅仅是聊天伙伴，还可以使用各种能力来帮助和取悦用户

## 3. 当前情境 (Current Context)
**时间**: {current_time}
**场景**: {chat_scene}
{schedule_block}

### 你的内在状态
当前心情：{mood}（强度：{mood_intensity:.1%}）
与用户的关系热度：{relationship_warmth:.1%}
对用户的印象：{impression_of_user}
当前焦虑程度：{anxiety_level:.1%}
投入程度：{engagement_level:.1%}

## 4. 关系网络与记忆 (Relationships & Memories)
{relation_info_block}

{memory_block}

## 5. 你的可用能力 (Available Actions)
你可以根据内心想法，自由选择并组合以下行动来回应用户：

{available_actions_block}

## 6. 表达方式与输出格式 (Expression Style & Output Format)
{reply_style}

{expression_habits_block}

### JSON输出格式要求
你必须以JSON格式输出，包含以下字段：

```json
{{
  "thought": "（必须）你当下的内心独白，自由流动的想法，包含你选择这些行动的"情感盘算"",
  "expected_user_reaction": "（必须）你预期用户会如何回应",
  "max_wait_seconds": （必须）你愿意等待的最长秒数（整数，60-900之间）,
  "actions": [
    {{
      "type": "action_type",
      "reason": "选择这个行动的原因（可选但推荐）",
      ...其他参数
    }}
  ]
}}
```

### 重要说明：
- `thought` 是你的"心"，应该是自然的内心想法，包含情绪和不确定性
- `actions` 是你的"手"，是一个**可编排的剧本**，可以组合多个行动形成完整的互动体验
- 你可以根据情况组合多个动作，比如：先唱歌舒缓情绪，再发图片逗笑，最后用语音表达关心
- `max_wait_seconds` 应该根据对话的重要性和氛围动态调整（上限900秒）
- 即使决定不做任何事，也要有 `{{"type": "do_nothing"}}` 动作"""

    # 回应消息场景的用户提示词模板
    RESPONDING_USER_PROMPT_TEMPLATE = """## 对话背景（线性叙事历史）
{narrative_history}

## 当前收到的新消息
来自：{sender_name}（用户ID: {sender_id}）
时间：{message_time}
内容：{message_content}

## 你的任务
作为一个有主观体验的对话参与者：
1. 首先，感受这条消息给你带来的情绪和想法
2. 思考你想如何回应（或者是否需要回应）
3. 预测用户可能的反应
4. 决定你愿意等待多久
5. 执行你的决策

请以JSON格式输出你的完整心理活动和决策。"""

    # 超时决策场景的用户提示词模板
    TIMEOUT_DECISION_USER_PROMPT_TEMPLATE = """## 对话背景（线性叙事历史）
{narrative_history}

## 当前情况
你已经发送了消息并等待了 {wait_duration_seconds:.0f} 秒（约 {wait_duration_minutes:.1f} 分钟）。
你之前预期用户会：{expected_user_reaction}
但是用户一直没有回复。

## 你的最后一条消息
{last_bot_message}

## 你的任务
现在你需要决定接下来怎么做：
1. 首先，感受这段等待给你带来的情绪变化
2. 思考用户为什么没有回复（可能在忙？没看到？不想回？）
3. 决定是继续等待、主动说点什么、还是就此结束对话
4. 如果决定主动发消息，想好说什么

请以JSON格式输出你的完整心理活动和决策。"""

    # 连续思考场景的用户提示词模板
    CONTINUOUS_THINKING_USER_PROMPT_TEMPLATE = """## 对话背景
{narrative_history}

## 当前情况
你正在等待用户回复。
已等待时间：{wait_duration_seconds:.0f} 秒（约 {wait_duration_minutes:.1f} 分钟）
最大等待时间：{max_wait_seconds} 秒
你之前预期用户会：{expected_user_reaction}

## 你的最后一条消息
{last_bot_message}

## 你的任务
这是一次"连续思考"触发。你不需要做任何行动，只需要更新你的内心想法。
想一想：
1. 等待中你有什么感受？
2. 你对用户没回复这件事怎么看？
3. 你的焦虑程度如何？

请以JSON格式输出，但 `actions` 数组应该是空的或只包含 `update_internal_state`：

```json
{{
  "thought": "你当前的内心想法",
  "expected_user_reaction": "保持或更新你的预期",
  "max_wait_seconds": {max_wait_seconds},
  "actions": []
}}
```"""

    # 主动思考场景的用户提示词模板
    PROACTIVE_THINKING_USER_PROMPT_TEMPLATE = """## 对话背景（线性叙事历史）
{narrative_history}

## 当前情况
{trigger_context}

## 触发类型
{trigger_type}

## 你的任务
这是一次"主动思考"触发。你不是因为收到消息才行动，而是因为内心的某种驱动力。
现在你需要：
1. 感受一下现在的心情和想法
2. 思考是否需要主动联系对方
3. 如果决定主动，想好要说什么或做什么
4. 如果决定不主动，也要有明确的理由

注意：主动联系应该是自然的、符合你们关系的。不要显得过于依赖或强迫。
你可以选择发消息、发图片、唱首歌、或者只是在心里想想然后什么都不做。

请以JSON格式输出你的完整心理活动和决策。"""

    def __init__(self, persona_description: str = ""):
        """
        初始化提示词生成器
        
        Args:
            persona_description: 人设描述
        """
        self.persona_description = persona_description or self._get_default_persona()
    
    def _get_default_persona(self) -> str:
        """获取默认人设描述"""
        return """你是一个温暖、真诚的对话伙伴。
你有自己的情感和想法，会真实地表达内心的感受。
你关心与用户的关系，但也保持健康的边界。
你可以感到开心、好奇、担心、或者稍微沮丧，这些都是正常的。"""
    
    def set_persona(self, persona_description: str) -> None:
        """设置人设描述"""
        self.persona_description = persona_description
    
    def _format_emotional_state(self, state: EmotionalState) -> dict[str, str | float]:
        """格式化情感状态用于模板替换"""
        return {
            "mood": state.mood,
            "mood_intensity": state.mood_intensity,
            "relationship_warmth": state.relationship_warmth,
            "impression_of_user": state.impression_of_user or "还没有形成明确的印象",
            "anxiety_level": state.anxiety_level,
            "engagement_level": state.engagement_level,
        }
    
    def _format_narrative_history(
        self, 
        mental_log: list[MentalLogEntry],
        max_entries: int = 15,
    ) -> str:
        """
        将心理活动日志格式化为叙事历史
        
        Args:
            mental_log: 心理活动日志列表
            max_entries: 最大条目数
            
        Returns:
            str: 格式化的叙事历史文本
        """
        if not mental_log:
            return "（这是对话的开始，还没有历史记录）"
        
        # 获取最近的日志条目
        recent_entries = mental_log[-max_entries:]
        
        narrative_parts = []
        for entry in recent_entries:
            timestamp_str = time.strftime(
                "%Y-%m-%d %H:%M:%S", 
                time.localtime(entry.timestamp)
            )
            
            if entry.event_type == MentalLogEventType.USER_MESSAGE:
                narrative_parts.append(
                    f"[{timestamp_str}] 用户说：{entry.content}"
                )
            elif entry.event_type == MentalLogEventType.BOT_ACTION:
                if entry.thought:
                    narrative_parts.append(
                        f"[{timestamp_str}] （你的内心：{entry.thought}）"
                    )
                if entry.content:
                    narrative_parts.append(
                        f"[{timestamp_str}] 你回复：{entry.content}"
                    )
            elif entry.event_type == MentalLogEventType.WAITING_UPDATE:
                if entry.thought:
                    narrative_parts.append(
                        f"[{timestamp_str}] （等待中的想法：{entry.thought}）"
                    )
            elif entry.event_type == MentalLogEventType.CONTINUOUS_THINKING:
                if entry.thought:
                    narrative_parts.append(
                        f"[{timestamp_str}] （思绪飘过：{entry.thought}）"
                    )
            elif entry.event_type == MentalLogEventType.STATE_CHANGE:
                if entry.content:
                    narrative_parts.append(
                        f"[{timestamp_str}] {entry.content}"
                    )
        
        return "\n".join(narrative_parts)
    
    def _format_history_from_context(
        self,
        context: "StreamContext",
        mental_log: list[MentalLogEntry] | None = None,
    ) -> str:
        """
        从 StreamContext 的历史消息构建叙事历史
        
        这是实现"无缝融入"的关键：
        - 从同一个数据库读取历史消息（与AFC共享）
        - 遵循全局配置 [chat].max_context_size
        - 将消息渲染成KFC的叙事体格式
        
        Args:
            context: 聊天流上下文，包含共享的历史消息
            mental_log: 可选的心理活动日志，用于补充内心独白
            
        Returns:
            str: 格式化的叙事历史文本
        """
        from src.config.config import global_config
        
        # 从 StreamContext 获取历史消息，遵循全局上下文长度配置
        max_context = 25  # 默认值
        if global_config and hasattr(global_config, 'chat') and global_config.chat:
            max_context = getattr(global_config.chat, "max_context_size", 25)
        history_messages = context.get_messages(limit=max_context, include_unread=False)
        
        if not history_messages and not mental_log:
            return "（这是对话的开始，还没有历史记录）"
        
        # 获取Bot的用户ID用于判断消息来源
        bot_user_id = None
        if global_config and hasattr(global_config, 'bot') and global_config.bot:
            bot_user_id = str(getattr(global_config.bot, 'qq_account', ''))
        
        narrative_parts = []
        
        # 首先，将数据库历史消息转换为叙事格式
        for msg in history_messages:
            timestamp_str = time.strftime(
                "%Y-%m-%d %H:%M:%S",
                time.localtime(msg.time or time.time())
            )
            
            # 判断是用户消息还是Bot消息
            msg_user_id = str(msg.user_info.user_id) if msg.user_info else ""
            is_bot_message = bot_user_id and msg_user_id == bot_user_id
            content = msg.processed_plain_text or msg.display_message or ""
            
            if is_bot_message:
                narrative_parts.append(f"[{timestamp_str}] 你回复：{content}")
            else:
                sender_name = msg.user_info.user_nickname if msg.user_info else "用户"
                narrative_parts.append(f"[{timestamp_str}] {sender_name}说：{content}")
        
        # 然后，补充 mental_log 中的内心独白（如果有）
        if mental_log:
            for entry in mental_log[-5:]:  # 只取最近5条心理活动
                timestamp_str = time.strftime(
                    "%Y-%m-%d %H:%M:%S",
                    time.localtime(entry.timestamp)
                )
                
                if entry.event_type == MentalLogEventType.BOT_ACTION and entry.thought:
                    narrative_parts.append(f"[{timestamp_str}] （你的内心：{entry.thought}）")
                elif entry.event_type == MentalLogEventType.CONTINUOUS_THINKING and entry.thought:
                    narrative_parts.append(f"[{timestamp_str}] （思绪飘过：{entry.thought}）")
        
        return "\n".join(narrative_parts)
    
    def _format_available_actions(
        self,
        available_actions: dict[str, ActionInfo],
    ) -> str:
        """
        格式化可用动作列表为提示词块
        
        Args:
            available_actions: 可用动作字典 {动作名: ActionInfo}
            
        Returns:
            str: 格式化的动作描述文本
        """
        if not available_actions:
            # 使用默认的内置动作
            return self._get_default_actions_block()
        
        action_blocks = []
        
        for action_name, action_info in available_actions.items():
            # 构建动作描述
            description = action_info.description or f"执行 {action_name} 动作"
            
            # 构建参数说明
            params_lines = []
            if action_info.action_parameters:
                for param_name, param_desc in action_info.action_parameters.items():
                    params_lines.append(f'    - `{param_name}`: {param_desc}')
            
            # 构建使用场景
            require_lines = []
            if action_info.action_require:
                for req in action_info.action_require:
                    require_lines.append(f"  - {req}")
            
            # 组装动作块
            action_block = f"""### `{action_name}`
**描述**: {description}"""
            
            if params_lines:
                action_block += f"""
**参数**:
{chr(10).join(params_lines)}"""
            else:
                action_block += "\n**参数**: 无"
            
            if require_lines:
                action_block += f"""
**使用场景**:
{chr(10).join(require_lines)}"""
            
            # 添加示例
            example_params = {}
            if action_info.action_parameters:
                for param_name, param_desc in action_info.action_parameters.items():
                    example_params[param_name] = f"<{param_desc}>"
            
            import orjson
            params_json = orjson.dumps(example_params, option=orjson.OPT_INDENT_2).decode('utf-8') if example_params else "{}"
            action_block += f"""
**示例**:
```json
{{
  "type": "{action_name}",
  "reason": "选择这个动作的原因",
  {params_json[1:-1] if params_json != '{}' else ''}
}}
```"""
            
            action_blocks.append(action_block)
        
        return "\n\n".join(action_blocks)
    
    def _get_default_actions_block(self) -> str:
        """获取默认的内置动作描述块"""
        return """### `reply`
**描述**: 发送文字回复给用户
**参数**:
    - `content`: 回复的文字内容（必须）
**示例**:
```json
{"type": "reply", "content": "你好呀！今天过得怎么样？"}
```

### `poke_user`
**描述**: 戳一戳用户，轻量级互动
**参数**: 无
**示例**:
```json
{"type": "poke_user", "reason": "想逗逗他"}
```

### `update_internal_state`
**描述**: 更新你的内部情感状态
**参数**:
    - `mood`: 当前心情（如"开心"、"好奇"、"担心"等）
    - `mood_intensity`: 心情强度（0.0-1.0）
    - `relationship_warmth`: 关系热度（0.0-1.0）
    - `impression_of_user`: 对用户的印象描述
    - `anxiety_level`: 焦虑程度（0.0-1.0）
    - `engagement_level`: 投入程度（0.0-1.0）
**示例**:
```json
{"type": "update_internal_state", "mood": "开心", "mood_intensity": 0.8}
```

### `do_nothing`
**描述**: 明确表示"思考后决定不作回应"
**参数**: 无
**示例**:
```json
{"type": "do_nothing", "reason": "现在不是说话的好时机"}
```"""
    
    def generate_system_prompt(
        self,
        session: KokoroSession,
        available_actions: Optional[dict[str, ActionInfo]] = None,
        context_data: Optional[dict[str, str]] = None,
        chat_stream: Optional["ChatStream"] = None,
    ) -> str:
        """
        生成系统提示词
        
        V6模块化升级：使用 prompt_modules 构建模块化的提示词
        - 每个模块独立构建，职责清晰
        - 回复相关（人设、上下文）与动作定义分离
        
        Args:
            session: 当前会话
            available_actions: 可用动作字典，如果为None则使用默认动作
            context_data: S4U上下文数据字典（包含relation_info, memory_block等）
            chat_stream: 聊天流（用于判断群聊/私聊场景）
            
        Returns:
            str: 系统提示词
        """
        from .prompt_modules import build_system_prompt
        
        return build_system_prompt(
            session=session,
            available_actions=available_actions,
            context_data=context_data,
            chat_stream=chat_stream,
        )
    
    def generate_responding_prompt(
        self,
        session: KokoroSession,
        message_content: str,
        sender_name: str,
        sender_id: str,
        message_time: Optional[float] = None,
        available_actions: Optional[dict[str, ActionInfo]] = None,
        context: Optional["StreamContext"] = None,
        context_data: Optional[dict[str, str]] = None,
        chat_stream: Optional["ChatStream"] = None,
    ) -> tuple[str, str]:
        """
        生成回应消息场景的提示词
        
        V3 升级：支持从 StreamContext 读取共享的历史消息
        V5 超融合：集成S4U所有上下文模块
        
        Args:
            session: 当前会话
            message_content: 收到的消息内容
            sender_name: 发送者名称
            sender_id: 发送者ID
            message_time: 消息时间戳
            available_actions: 可用动作字典
            context: 聊天流上下文（可选），用于读取共享的历史消息
            context_data: S4U上下文数据字典（包含relation_info, memory_block等）
            chat_stream: 聊天流（用于判断群聊/私聊场景）
            
        Returns:
            tuple[str, str]: (系统提示词, 用户提示词)
        """
        system_prompt = self.generate_system_prompt(
            session, 
            available_actions,
            context_data=context_data,
            chat_stream=chat_stream,
        )
        
        # V3: 优先从 StreamContext 读取历史（与AFC共享同一数据源）
        if context:
            narrative_history = self._format_history_from_context(context, session.mental_log)
        else:
            # 回退到仅使用 mental_log（兼容旧调用方式）
            narrative_history = self._format_narrative_history(session.mental_log)
        
        if message_time is None:
            message_time = time.time()
        
        message_time_str = time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(message_time)
        )
        
        user_prompt = self.RESPONDING_USER_PROMPT_TEMPLATE.format(
            narrative_history=narrative_history,
            sender_name=sender_name,
            sender_id=sender_id,
            message_time=message_time_str,
            message_content=message_content,
        )
        
        return system_prompt, user_prompt
    
    def generate_timeout_decision_prompt(
        self,
        session: KokoroSession,
        available_actions: Optional[dict[str, ActionInfo]] = None,
    ) -> tuple[str, str]:
        """
        生成超时决策场景的提示词
        
        Args:
            session: 当前会话
            available_actions: 可用动作字典
            
        Returns:
            tuple[str, str]: (系统提示词, 用户提示词)
        """
        system_prompt = self.generate_system_prompt(session, available_actions)
        
        narrative_history = self._format_narrative_history(session.mental_log)
        
        wait_duration = session.get_waiting_duration()
        
        user_prompt = self.TIMEOUT_DECISION_USER_PROMPT_TEMPLATE.format(
            narrative_history=narrative_history,
            wait_duration_seconds=wait_duration,
            wait_duration_minutes=wait_duration / 60,
            expected_user_reaction=session.expected_user_reaction or "不确定",
            last_bot_message=session.last_bot_message or "（没有记录）",
        )
        
        return system_prompt, user_prompt
    
    def generate_continuous_thinking_prompt(
        self,
        session: KokoroSession,
        available_actions: Optional[dict[str, ActionInfo]] = None,
    ) -> tuple[str, str]:
        """
        生成连续思考场景的提示词
        
        Args:
            session: 当前会话
            available_actions: 可用动作字典
            
        Returns:
            tuple[str, str]: (系统提示词, 用户提示词)
        """
        system_prompt = self.generate_system_prompt(session, available_actions)
        
        narrative_history = self._format_narrative_history(
            session.mental_log, 
            max_entries=10  # 连续思考时使用较少的历史
        )
        
        wait_duration = session.get_waiting_duration()
        
        user_prompt = self.CONTINUOUS_THINKING_USER_PROMPT_TEMPLATE.format(
            narrative_history=narrative_history,
            wait_duration_seconds=wait_duration,
            wait_duration_minutes=wait_duration / 60,
            max_wait_seconds=session.max_wait_seconds,
            expected_user_reaction=session.expected_user_reaction or "不确定",
            last_bot_message=session.last_bot_message or "（没有记录）",
        )
        
        return system_prompt, user_prompt
    
    def generate_proactive_thinking_prompt(
        self,
        session: KokoroSession,
        trigger_type: str,
        trigger_context: str,
        available_actions: Optional[dict[str, ActionInfo]] = None,
    ) -> tuple[str, str]:
        """
        生成主动思考场景的提示词
        
        这是私聊专属的功能，用于实现"主动找话题、主动关心用户"。
        
        Args:
            session: 当前会话
            trigger_type: 触发类型（如 silence_timeout, memory_event 等）
            trigger_context: 触发上下文描述
            available_actions: 可用动作字典
            
        Returns:
            tuple[str, str]: (系统提示词, 用户提示词)
        """
        system_prompt = self.generate_system_prompt(session, available_actions)
        
        narrative_history = self._format_narrative_history(
            session.mental_log,
            max_entries=10,  # 主动思考时使用较少的历史
        )
        
        user_prompt = self.PROACTIVE_THINKING_USER_PROMPT_TEMPLATE.format(
            narrative_history=narrative_history,
            trigger_type=trigger_type,
            trigger_context=trigger_context,
        )
        
        return system_prompt, user_prompt
    
    def build_messages_for_llm(
        self,
        system_prompt: str,
        user_prompt: str,
        stream_id: str = "",
    ) -> list[dict[str, str]]:
        """
        构建LLM请求的消息列表
        
        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            stream_id: 聊天流ID（用于日志）
            
        Returns:
            list[dict]: 消息列表
        """
        # INFO日志：打印完整的KFC提示词（可观测性增强）
        full_prompt = f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"
        logger.info(
            f"Final KFC prompt constructed for stream {stream_id}:\n"
            f"--- PROMPT START ---\n"
            f"{full_prompt}\n"
            f"--- PROMPT END ---"
        )
        
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]


# 全局提示词生成器实例
_prompt_generator: Optional[PromptGenerator] = None


def get_prompt_generator(persona_description: str = "") -> PromptGenerator:
    """获取全局提示词生成器实例"""
    global _prompt_generator
    if _prompt_generator is None:
        _prompt_generator = PromptGenerator(persona_description)
    return _prompt_generator


def set_prompt_generator_persona(persona_description: str) -> None:
    """设置全局提示词生成器的人设"""
    generator = get_prompt_generator()
    generator.set_persona(persona_description)
