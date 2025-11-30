"""
Kokoro Flow Chatter V2 - 提示词模板注册

使用项目统一的 Prompt 管理系统注册所有 KFC V2 使用的提示词模板
"""

from src.chat.utils.prompt import Prompt

# =================================================================================================
# KFC V2 主提示词模板
# =================================================================================================

KFC_V2_MAIN_PROMPT = Prompt(
    name="kfc_v2_main",
    template="""# 你与 {user_name} 的私聊

## 1. 你是谁
{persona_block}

## 2. 你与 {user_name} 的关系
{relation_block}

## 3. 你们之间发生的事（活动流）
以下是你和 {user_name} 最近的互动历史，按时间顺序记录了你们的对话和你的心理活动：

{activity_stream}

## 4. 当前情况
{current_situation}

## 5. 你可以做的事情
{available_actions}

## 6. 你的回复格式
{output_format}
""",
)

# =================================================================================================
# 输出格式模板
# =================================================================================================

KFC_V2_OUTPUT_FORMAT = Prompt(
    name="kfc_v2_output_format",
    template="""请用以下 JSON 格式回复：
```json
{{
    "thought": "你脑子里在想什么，越自然越好",
    "actions": [
        {{"type": "reply", "content": "你要说的话"}},
        {{"type": "其他动作", "参数": "值"}}
    ],
    "expected_reaction": "你期待对方的反应是什么",
    "max_wait_seconds": 300
}}
```

说明：
- `thought`：你的内心独白，记录你此刻的想法和感受
- `actions`：你要执行的动作列表，可以组合多个
- `expected_reaction`：你期待对方如何回应（用于判断是否需要等待）
- `max_wait_seconds`：设定等待时间（秒），0 表示不等待，超时后你会考虑是否要主动说点什么
- 即使什么都不想做，也放一个 `{{"type": "do_nothing"}}`""",
)

# =================================================================================================
# 情景模板 - 根据不同情境使用不同的当前情况描述
# =================================================================================================

KFC_V2_SITUATION_NEW_MESSAGE = Prompt(
    name="kfc_v2_situation_new_message",
    template="""现在是 {current_time}。

{user_name} 刚刚给你发了消息。这是一次新的对话发起（不是对你之前消息的回复）。

请决定你要怎么回应。你可以：
- 发送文字消息回复
- 发表情包
- 戳一戳对方
- 什么都不做（如果觉得没必要回复）
- 或者组合多个动作""",
)

KFC_V2_SITUATION_REPLY_IN_TIME = Prompt(
    name="kfc_v2_situation_reply_in_time",
    template="""现在是 {current_time}。

你之前发了消息后一直在等 {user_name} 的回复。
等了大约 {elapsed_minutes:.1f} 分钟（你原本打算最多等 {max_wait_minutes:.1f} 分钟）。
现在 {user_name} 回复了！

请决定你接下来要怎么回应。""",
)

KFC_V2_SITUATION_REPLY_LATE = Prompt(
    name="kfc_v2_situation_reply_late",
    template="""现在是 {current_time}。

你之前发了消息后在等 {user_name} 的回复。
你原本打算最多等 {max_wait_minutes:.1f} 分钟，但实际等了 {elapsed_minutes:.1f} 分钟才收到回复。
虽然有点迟，但 {user_name} 终于回复了。

请决定你接下来要怎么回应。（可以选择轻轻抱怨一下迟到，也可以装作没在意）""",
)

KFC_V2_SITUATION_TIMEOUT = Prompt(
    name="kfc_v2_situation_timeout",
    template="""现在是 {current_time}。

你之前发了消息后一直在等 {user_name} 的回复。
你原本打算最多等 {max_wait_minutes:.1f} 分钟，现在已经等了 {elapsed_minutes:.1f} 分钟了，对方还是没回。
你期待的反应是："{expected_reaction}"

你需要决定：
1. 继续等待（设置新的 max_wait_seconds）
2. 主动说点什么打破沉默
3. 做点别的事情（戳一戳、发表情等）
4. 算了不等了（max_wait_seconds = 0）""",
)

KFC_V2_SITUATION_PROACTIVE = Prompt(
    name="kfc_v2_situation_proactive",
    template="""现在是 {current_time}。

你和 {user_name} 已经有一段时间没聊天了（沉默了 {silence_duration}）。
{trigger_reason}

你在想要不要主动找 {user_name} 聊点什么。

请决定：
1. 主动发起对话（想个话题开场）
2. 发个表情或戳一戳试探一下
3. 算了，现在不是好时机（do_nothing）

如果决定发起对话，想想用什么自然的方式开场，不要太突兀。""",
)

# =================================================================================================
# 活动流条目模板 - 用于构建 activity_stream
# =================================================================================================

# 用户消息条目
KFC_V2_ENTRY_USER_MESSAGE = Prompt(
    name="kfc_v2_entry_user_message",
    template="""【{time}】{user_name} 说：
"{content}"
""",
)

# Bot 规划条目（有等待）
KFC_V2_ENTRY_BOT_PLANNING = Prompt(
    name="kfc_v2_entry_bot_planning",
    template="""【你的想法】
内心：{thought}
行动：{actions_description}
期待：{expected_reaction}
决定等待：最多 {max_wait_minutes:.1f} 分钟
""",
)

# Bot 规划条目（无等待）
KFC_V2_ENTRY_BOT_PLANNING_NO_WAIT = Prompt(
    name="kfc_v2_entry_bot_planning_no_wait",
    template="""【你的想法】
内心：{thought}
行动：{actions_description}
（不打算等对方回复）
""",
)

# 等待期间心理变化
KFC_V2_ENTRY_WAITING_UPDATE = Prompt(
    name="kfc_v2_entry_waiting_update",
    template="""【等待中... {elapsed_minutes:.1f} 分钟过去了】
你想：{waiting_thought}
""",
)

# 收到及时回复时的标注
KFC_V2_ENTRY_REPLY_IN_TIME = Prompt(
    name="kfc_v2_entry_reply_in_time",
    template="""→ （对方在你预期时间内回复了，等了 {elapsed_minutes:.1f} 分钟）
""",
)

# 收到迟到回复时的标注
KFC_V2_ENTRY_REPLY_LATE = Prompt(
    name="kfc_v2_entry_reply_late",
    template="""→ （对方回复迟了，你原本只打算等 {max_wait_minutes:.1f} 分钟，实际等了 {elapsed_minutes:.1f} 分钟）
""",
)

# 主动思考触发
KFC_V2_ENTRY_PROACTIVE_TRIGGER = Prompt(
    name="kfc_v2_entry_proactive_trigger",
    template="""【沉默了 {silence_duration}】
你开始考虑要不要主动找对方聊点什么...
""",
)

# 导出所有模板名称，方便外部引用
PROMPT_NAMES = {
    "main": "kfc_v2_main",
    "output_format": "kfc_v2_output_format",
    "situation_new_message": "kfc_v2_situation_new_message",
    "situation_reply_in_time": "kfc_v2_situation_reply_in_time",
    "situation_reply_late": "kfc_v2_situation_reply_late",
    "situation_timeout": "kfc_v2_situation_timeout",
    "situation_proactive": "kfc_v2_situation_proactive",
    "entry_user_message": "kfc_v2_entry_user_message",
    "entry_bot_planning": "kfc_v2_entry_bot_planning",
    "entry_bot_planning_no_wait": "kfc_v2_entry_bot_planning_no_wait",
    "entry_waiting_update": "kfc_v2_entry_waiting_update",
    "entry_reply_in_time": "kfc_v2_entry_reply_in_time",
    "entry_reply_late": "kfc_v2_entry_reply_late",
    "entry_proactive_trigger": "kfc_v2_entry_proactive_trigger",
}
