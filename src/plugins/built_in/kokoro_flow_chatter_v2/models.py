"""
Kokoro Flow Chatter V2 - 数据模型

定义核心数据结构：
- EventType: 活动流事件类型
- SessionStatus: 会话状态（仅 IDLE 和 WAITING）
- MentalLogEntry: 心理活动日志条目
- WaitingConfig: 等待配置
- ActionModel: 动作模型
- LLMResponse: LLM 响应结构
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
import time


class EventType(Enum):
    """
    活动流事件类型
    
    用于标记 mental_log 中不同类型的事件，
    每种类型对应一个提示词小模板
    """
    # 用户相关
    USER_MESSAGE = "user_message"              # 用户发送消息
    
    # Bot 行动相关
    BOT_PLANNING = "bot_planning"              # Bot 规划（thought + actions）
    
    # 等待相关
    WAITING_START = "waiting_start"            # 开始等待
    WAITING_UPDATE = "waiting_update"          # 等待期间心理变化
    REPLY_RECEIVED_IN_TIME = "reply_in_time"   # 在预期内收到回复
    REPLY_RECEIVED_LATE = "reply_late"         # 超出预期收到回复
    WAIT_TIMEOUT = "wait_timeout"              # 等待超时
    
    # 主动思考相关
    PROACTIVE_TRIGGER = "proactive_trigger"    # 主动思考触发（长期沉默）
    
    def __str__(self) -> str:
        return self.value


class SessionStatus(Enum):
    """
    会话状态
    
    极简设计，只有两种稳定状态：
    - IDLE: 空闲，没有期待回复
    - WAITING: 等待对方回复中
    """
    IDLE = "idle"
    WAITING = "waiting"
    
    def __str__(self) -> str:
        return self.value


@dataclass
class WaitingConfig:
    """
    等待配置
    
    当 Bot 发送消息后设置的等待参数
    """
    expected_reaction: str = ""      # 期望对方如何回应
    max_wait_seconds: int = 0        # 最长等待时间（秒），0 表示不等待
    started_at: float = 0.0          # 开始等待的时间戳
    last_thinking_at: float = 0.0    # 上次连续思考的时间戳
    thinking_count: int = 0          # 连续思考次数
    
    def is_active(self) -> bool:
        """是否正在等待"""
        return self.max_wait_seconds > 0 and self.started_at > 0
    
    def get_elapsed_seconds(self) -> float:
        """获取已等待时间（秒）"""
        if not self.is_active():
            return 0.0
        return time.time() - self.started_at
    
    def get_elapsed_minutes(self) -> float:
        """获取已等待时间（分钟）"""
        return self.get_elapsed_seconds() / 60
    
    def is_timeout(self) -> bool:
        """是否已超时"""
        if not self.is_active():
            return False
        return self.get_elapsed_seconds() >= self.max_wait_seconds
    
    def get_progress(self) -> float:
        """获取等待进度 (0.0 - 1.0)"""
        if not self.is_active() or self.max_wait_seconds <= 0:
            return 0.0
        return min(self.get_elapsed_seconds() / self.max_wait_seconds, 1.0)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "expected_reaction": self.expected_reaction,
            "max_wait_seconds": self.max_wait_seconds,
            "started_at": self.started_at,
            "last_thinking_at": self.last_thinking_at,
            "thinking_count": self.thinking_count,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WaitingConfig":
        return cls(
            expected_reaction=data.get("expected_reaction", ""),
            max_wait_seconds=data.get("max_wait_seconds", 0),
            started_at=data.get("started_at", 0.0),
            last_thinking_at=data.get("last_thinking_at", 0.0),
            thinking_count=data.get("thinking_count", 0),
        )
    
    def reset(self) -> None:
        """重置等待配置"""
        self.expected_reaction = ""
        self.max_wait_seconds = 0
        self.started_at = 0.0
        self.last_thinking_at = 0.0
        self.thinking_count = 0


@dataclass
class MentalLogEntry:
    """
    心理活动日志条目
    
    记录活动流中的每一个事件节点，
    用于构建线性叙事风格的提示词
    """
    event_type: EventType
    timestamp: float
    
    # 通用字段
    content: str = ""              # 事件内容（消息文本、动作描述等）
    
    # 用户消息相关
    user_name: str = ""            # 发送者名称
    user_id: str = ""              # 发送者 ID
    
    # Bot 规划相关
    thought: str = ""              # 内心想法
    actions: list[dict] = field(default_factory=list)  # 执行的动作列表
    expected_reaction: str = ""    # 期望的回应
    max_wait_seconds: int = 0      # 设定的等待时间
    
    # 等待相关
    elapsed_seconds: float = 0.0   # 已等待时间
    waiting_thought: str = ""      # 等待期间的想法
    mood: str = ""                 # 当前心情
    
    # 元数据
    metadata: dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": str(self.event_type),
            "timestamp": self.timestamp,
            "content": self.content,
            "user_name": self.user_name,
            "user_id": self.user_id,
            "thought": self.thought,
            "actions": self.actions,
            "expected_reaction": self.expected_reaction,
            "max_wait_seconds": self.max_wait_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "waiting_thought": self.waiting_thought,
            "mood": self.mood,
            "metadata": self.metadata,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MentalLogEntry":
        event_type_str = data.get("event_type", "user_message")
        try:
            event_type = EventType(event_type_str)
        except ValueError:
            event_type = EventType.USER_MESSAGE
        
        return cls(
            event_type=event_type,
            timestamp=data.get("timestamp", time.time()),
            content=data.get("content", ""),
            user_name=data.get("user_name", ""),
            user_id=data.get("user_id", ""),
            thought=data.get("thought", ""),
            actions=data.get("actions", []),
            expected_reaction=data.get("expected_reaction", ""),
            max_wait_seconds=data.get("max_wait_seconds", 0),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            waiting_thought=data.get("waiting_thought", ""),
            mood=data.get("mood", ""),
            metadata=data.get("metadata", {}),
        )
    
    def get_time_str(self, format: str = "%H:%M") -> str:
        """获取格式化的时间字符串"""
        return time.strftime(format, time.localtime(self.timestamp))


@dataclass
class ActionModel:
    """
    动作模型
    
    表示 LLM 决策的单个动作
    """
    type: str                                  # 动作类型
    params: dict[str, Any] = field(default_factory=dict)  # 动作参数
    reason: str = ""                           # 选择该动作的理由
    
    def to_dict(self) -> dict[str, Any]:
        result = {"type": self.type}
        if self.reason:
            result["reason"] = self.reason
        result.update(self.params)
        return result
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ActionModel":
        action_type = data.get("type", "do_nothing")
        reason = data.get("reason", "")
        params = {k: v for k, v in data.items() if k not in ("type", "reason")}
        return cls(type=action_type, params=params, reason=reason)
    
    def get_description(self) -> str:
        """获取动作的文字描述"""
        if self.type == "reply":
            content = self.params.get("content", "")
            return f'发送消息："{content[:50]}{"..." if len(content) > 50 else ""}"'
        elif self.type == "poke_user":
            return "戳了戳对方"
        elif self.type == "do_nothing":
            return "什么都没做"
        elif self.type == "send_emoji":
            emoji = self.params.get("emoji", "")
            return f"发送表情：{emoji}"
        else:
            return f"执行动作：{self.type}"


@dataclass
class LLMResponse:
    """
    LLM 响应结构
    
    定义 LLM 输出的 JSON 格式
    """
    thought: str                              # 内心想法
    actions: list[ActionModel]                # 动作列表
    expected_reaction: str = ""               # 期望对方的回应
    max_wait_seconds: int = 0                 # 最长等待时间（0 = 不等待）
    
    # 可选字段
    mood: str = ""                            # 当前心情
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "thought": self.thought,
            "actions": [a.to_dict() for a in self.actions],
            "expected_reaction": self.expected_reaction,
            "max_wait_seconds": self.max_wait_seconds,
            "mood": self.mood,
        }
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "LLMResponse":
        actions_data = data.get("actions", [])
        actions = [ActionModel.from_dict(a) for a in actions_data] if actions_data else []
        
        # 如果没有动作，添加默认的 do_nothing
        if not actions:
            actions = [ActionModel(type="do_nothing")]
        
        # 处理 max_wait_seconds，确保在合理范围内
        max_wait = data.get("max_wait_seconds", 0)
        try:
            max_wait = int(max_wait)
            max_wait = max(0, min(max_wait, 1800))  # 0-30分钟
        except (ValueError, TypeError):
            max_wait = 0
        
        return cls(
            thought=data.get("thought", ""),
            actions=actions,
            expected_reaction=data.get("expected_reaction", ""),
            max_wait_seconds=max_wait,
            mood=data.get("mood", ""),
        )
    
    @classmethod
    def create_error_response(cls, error_message: str) -> "LLMResponse":
        """创建错误响应"""
        return cls(
            thought=f"出现了问题：{error_message}",
            actions=[ActionModel(type="do_nothing")],
            expected_reaction="",
            max_wait_seconds=0,
        )
    
    def has_reply(self) -> bool:
        """是否包含回复动作"""
        return any(a.type in ("reply", "respond") for a in self.actions)
    
    def get_reply_content(self) -> str:
        """获取回复内容"""
        for action in self.actions:
            if action.type in ("reply", "respond"):
                return action.params.get("content", "")
        return ""
    
    def get_actions_description(self) -> str:
        """获取所有动作的文字描述"""
        descriptions = [a.get_description() for a in self.actions]
        return " + ".join(descriptions)
