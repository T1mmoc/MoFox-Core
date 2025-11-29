"""
Kokoro Flow Chatter 模块化提示词组件

将提示词拆分为独立的模块，每个模块负责特定的内容生成：
1. 核心身份模块 - 人设、人格、世界观
2. 行为准则模块 - 规则、安全边界
3. 情境上下文模块 - 时间、场景、关系、记忆
4. 动作能力模块 - 可用动作的描述
5. 输出格式模块 - JSON格式要求

设计理念：
- 每个模块只负责自己的部分，互不干扰
- 回复相关内容（人设、上下文）与动作定义分离
- 方便独立调试和优化每个部分
"""

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

import orjson

from src.common.logger import get_logger
from src.config.config import global_config
from src.plugin_system.base.component_types import ActionInfo

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream

from .models import EmotionalState, KokoroSession

logger = get_logger("kfc_prompt_modules")


# ============================================================
# 模块1: 核心身份 - 人设/人格/背景
# ============================================================

def build_identity_module() -> str:
    """
    构建核心身份模块
    
    包含：人格核心、人格细节、外貌身份、世界观背景
    这些是回复内容的基础，决定了"你是谁"
    """
    if global_config is None:
        raise RuntimeError("global_config 未初始化")
    
    personality_cfg = global_config.personality
    
    personality_core = personality_cfg.personality_core or "你是一个温暖、真诚的对话伙伴。"
    personality_side = personality_cfg.personality_side or ""
    identity = personality_cfg.identity or ""
    background_story = personality_cfg.background_story or ""
    
    parts = [
        "## 1. 核心身份与人格 (Core Identity & Personality)",
        personality_core,
    ]
    
    if personality_side:
        parts.append(f"\n### 人格细节\n{personality_side}")
    
    if identity:
        parts.append(f"\n### 外貌与身份特征\n{identity}")
    
    if background_story:
        parts.append(f"\n### 世界观与背景故事\n{background_story}")
    
    return "\n".join(parts)


# ============================================================
# 模块2: 行为准则 - 规则/边界/KFC特有准则
# ============================================================

def build_rules_module() -> str:
    """
    构建行为准则模块
    
    包含：安全规则 + KFC 通用准则
    这些是行为的边界，决定了"什么能做什么不能做"
    """
    if global_config is None:
        raise RuntimeError("global_config 未初始化")
    
    safety_guidelines = global_config.personality.safety_guidelines or []
    if isinstance(safety_guidelines, list):
        safety_guidelines_str = "\n".join(f"- {rule}" for rule in safety_guidelines)
    else:
        safety_guidelines_str = str(safety_guidelines)
    
    # KFC 特有准则（极致拟人相关）
    kfc_rules = """### KFC核心准则（极致拟人）
1. **你是真实存在的人**：你不是AI、不是助手、不是工具。你是一个有血有肉、有自己生活和想法的人
2. **感受优先**：在做出任何回应前，先以"人"的方式体验和感受当前的情境
3. **真实的内心活动**：保持真实的内心独白，可以有情绪、猜测、犹豫、甚至不想搭理人的时候
4. **自然的社交距离**：像真人一样有自己的边界感，不会无条件迎合，也不会刻意疏远
5. **有自己的生活**：你有自己的事情要做、自己的兴趣爱好、自己的情绪起伏，不是随叫随到的存在"""
    
    return f"""## 2. 核心行为准则 (Core Interaction Rules)
{safety_guidelines_str}

{kfc_rules}"""


# ============================================================
# 模块3: 情境上下文 - 时间/场景/内在状态/关系/记忆
# ============================================================

def build_context_module(
    session: KokoroSession,
    chat_stream: Optional["ChatStream"] = None,
    context_data: Optional[dict[str, str]] = None,
) -> str:
    """
    构建情境上下文模块
    
    包含：当前时间、聊天场景、内在状态、关系信息、记忆
    这些是回复的上下文，决定了"当前在什么情况下"
    
    Args:
        session: 当前会话
        chat_stream: 聊天流（判断群聊/私聊）
        context_data: S4U 上下文数据
    """
    context_data = context_data or {}
    
    # 时间和场景
    current_time = datetime.now().strftime("%Y年%m月%d日 %H:%M:%S")
    is_group_chat = bool(chat_stream and chat_stream.group_info)
    chat_scene = "群聊" if is_group_chat else "私聊"
    
    # 日程（如果有）
    schedule_block = context_data.get("schedule", "")
    if schedule_block:
        schedule_block = f"\n**当前活动**: {schedule_block}"
    
    # 内在状态
    es = session.emotional_state
    inner_state = f"""### 你的内在状态
当前心情：{es.mood}（强度：{es.mood_intensity:.1%}）
与用户的关系热度：{es.relationship_warmth:.1%}
对用户的印象：{es.impression_of_user or "还没有形成明确的印象"}
当前焦虑程度：{es.anxiety_level:.1%}
投入程度：{es.engagement_level:.1%}"""
    
    # 关系信息
    relation_info = context_data.get("relation_info", "")
    relation_block = relation_info if relation_info else "（暂无关系信息）"
    
    # 记忆
    memory_block = context_data.get("memory_block", "")
    
    parts = [
        "## 3. 当前情境 (Current Context)",
        f"**时间**: {current_time}",
        f"**场景**: {chat_scene}",
    ]
    
    if schedule_block:
        parts.append(schedule_block)
    
    parts.append("")
    parts.append(inner_state)
    parts.append("")
    parts.append("## 4. 关系网络与记忆 (Relationships & Memories)")
    parts.append(relation_block)
    
    if memory_block:
        parts.append("")
        parts.append(memory_block)
    
    return "\n".join(parts)


# ============================================================
# 模块4: 动作能力 - 可用动作的描述
# ============================================================

def build_actions_module(available_actions: Optional[dict[str, ActionInfo]] = None) -> str:
    """
    构建动作能力模块
    
    包含：所有可用动作的描述、参数、示例
    这部分与回复内容分离，只描述"能做什么"
    
    Args:
        available_actions: 可用动作字典
    """
    if not available_actions:
        actions_block = _get_default_actions_block()
    else:
        actions_block = _format_available_actions(available_actions)
    
    return f"""## 5. 你的可用能力 (Available Actions)
你可以根据内心想法，自由选择并组合以下行动来回应用户：

{actions_block}"""


def _format_available_actions(available_actions: dict[str, ActionInfo]) -> str:
    """格式化可用动作列表"""
    action_blocks = []
    
    for action_name, action_info in available_actions.items():
        description = action_info.description or f"执行 {action_name} 动作"
        
        # 参数说明
        params_lines = []
        if action_info.action_parameters:
            for param_name, param_desc in action_info.action_parameters.items():
                params_lines.append(f'    - `{param_name}`: {param_desc}')
        
        # 使用场景
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
        
        # 示例
        example_params = {}
        if action_info.action_parameters:
            for param_name, param_desc in action_info.action_parameters.items():
                example_params[param_name] = f"<{param_desc}>"
        
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


def _get_default_actions_block() -> str:
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


# ============================================================
# 模块5: 表达与输出格式 - 回复风格 + JSON格式
# ============================================================

def build_output_module(
    context_data: Optional[dict[str, str]] = None,
) -> str:
    """
    构建输出格式模块
    
    包含：表达风格、表达习惯、JSON 输出格式要求
    这部分定义了"怎么说"和"输出什么格式"
    
    Args:
        context_data: S4U 上下文数据（包含 expression_habits）
    """
    if global_config is None:
        raise RuntimeError("global_config 未初始化")
    
    context_data = context_data or {}
    
    reply_style = global_config.personality.reply_style or ""
    expression_habits = context_data.get("expression_habits", "")
    
    # JSON 输出格式说明 - 强调 max_wait_seconds 可以为 0
    json_format = """### JSON输出格式要求
你必须以JSON格式输出，包含以下字段：

```json
{
  "thought": "（必须）你当下的内心独白，自由流动的想法，包含你选择这些行动的"情感盘算"",
  "expected_user_reaction": "（必须）你预期用户会如何回应",
  "max_wait_seconds": （必须）你愿意等待的最长秒数（整数，0-900之间，0表示不等待）,
  "actions": [
    {
      "type": "action_type",
      "reason": "选择这个行动的原因（可选但推荐）",
      ...其他参数
    }
  ]
}
```

### 重要说明：
- `thought` 是你的"心"，应该是自然的内心想法，包含情绪和不确定性
- `actions` 是你的"手"，是一个**可编排的剧本**，可以组合多个行动形成完整的互动体验
- 你可以根据情况组合多个动作，比如：先唱歌舒缓情绪，再发图片逗笑，最后用语音表达关心
- `max_wait_seconds` 应该根据对话的重要性和氛围动态调整（上限900秒）
  - **重要**: 当话题已经自然结束、用户说"拜拜/晚安/再见"、或者你认为不需要继续等待用户回复时，设为 **0**
- 即使决定不做任何事，也要有 `{"type": "do_nothing"}` 动作"""
    
    parts = ["## 6. 表达方式与输出格式 (Expression Style & Output Format)"]
    
    if reply_style:
        parts.append(reply_style)
    
    if expression_habits:
        parts.append("")
        parts.append(expression_habits)
    
    parts.append("")
    parts.append(json_format)
    
    return "\n".join(parts)


# ============================================================
# 组装完整的 System Prompt
# ============================================================

def build_system_prompt(
    session: KokoroSession,
    available_actions: Optional[dict[str, ActionInfo]] = None,
    context_data: Optional[dict[str, str]] = None,
    chat_stream: Optional["ChatStream"] = None,
) -> str:
    """
    组装完整的系统提示词
    
    按模块顺序组装：
    1. 角色框架（你是对话体验者）
    2. 核心身份（人设）
    3. 行为准则（规则）
    4. 情境上下文（时间/状态/关系）
    5. 动作能力（可用动作）
    6. 输出格式（表达风格 + JSON）
    
    Args:
        session: 当前会话
        available_actions: 可用动作字典
        context_data: S4U 上下文数据
        chat_stream: 聊天流
        
    Returns:
        完整的系统提示词
    """
    # 角色框架
    role_frame = '你是一个有主观体验的对话伙伴，而不是一个问答工具。你需要"感受"、"思考"并"体验"这段对话。'
    
    # 组装各模块
    modules = [
        role_frame,
        "",
        build_identity_module(),
        "",
        build_rules_module(),
        "",
        build_context_module(session, chat_stream, context_data),
        "",
        build_actions_module(available_actions),
        "",
        build_output_module(context_data),
    ]
    
    return "\n".join(modules)
