"""
消息管理模块数据模型
定义消息管理器使用的数据结构
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, TYPE_CHECKING

from . import BaseDataModel
from src.plugin_system.base.component_types import ChatMode, ChatType

if TYPE_CHECKING:
    from .database_data_model import DatabaseMessages


class MessageStatus(Enum):
    """消息状态枚举"""

    UNREAD = "unread"  # 未读消息
    READ = "read"  # 已读消息
    PROCESSING = "processing"  # 处理中


@dataclass
class StreamContext(BaseDataModel):
    """聊天流上下文信息"""

    stream_id: str
    chat_type: ChatType = ChatType.PRIVATE  # 聊天类型，默认为私聊
    chat_mode: ChatMode = ChatMode.NORMAL  # 聊天模式，默认为普通模式
    unread_messages: List["DatabaseMessages"] = field(default_factory=list)
    history_messages: List["DatabaseMessages"] = field(default_factory=list)
    last_check_time: float = field(default_factory=time.time)
    is_active: bool = True
    processing_task: Optional[asyncio.Task] = None

    def add_message(self, message: "DatabaseMessages"):
        """添加消息到上下文"""
        message.is_read = False
        self.unread_messages.append(message)

        # 自动检测和更新chat type
        self._detect_chat_type(message)

    def _detect_chat_type(self, message: "DatabaseMessages"):
        """根据消息内容自动检测聊天类型"""
        # 只有在第一次添加消息时才检测聊天类型，避免后续消息改变类型
        if len(self.unread_messages) == 1:  # 只有这条消息
            # 如果消息包含群组信息，则为群聊
            if hasattr(message, 'chat_info_group_id') and message.chat_info_group_id:
                self.chat_type = ChatType.GROUP
            elif hasattr(message, 'chat_info_group_name') and message.chat_info_group_name:
                self.chat_type = ChatType.GROUP
            else:
                self.chat_type = ChatType.PRIVATE

    def update_chat_type(self, chat_type: ChatType):
        """手动更新聊天类型"""
        self.chat_type = chat_type

    def set_chat_mode(self, chat_mode: ChatMode):
        """设置聊天模式"""
        self.chat_mode = chat_mode
        
    def is_group_chat(self) -> bool:
        """检查是否为群聊"""
        return self.chat_type == ChatType.GROUP

    def is_private_chat(self) -> bool:
        """检查是否为私聊"""
        return self.chat_type == ChatType.PRIVATE

    def get_chat_type_display(self) -> str:
        """获取聊天类型的显示名称"""
        if self.chat_type == ChatType.GROUP:
            return "群聊"
        elif self.chat_type == ChatType.PRIVATE:
            return "私聊"
        else:
            return "未知类型"

    def get_unread_messages(self) -> List["DatabaseMessages"]:
        """获取未读消息"""
        return [msg for msg in self.unread_messages if not msg.is_read]

    def mark_message_as_read(self, message_id: str):
        """标记消息为已读"""
        for msg in self.unread_messages:
            if msg.message_id == message_id:
                msg.is_read = True
                self.history_messages.append(msg)
                self.unread_messages.remove(msg)
                break
     
    def get_history_messages(self, limit: int = 20) -> List["DatabaseMessages"]:
        """获取历史消息"""
        # 优先返回最近的历史消息和所有未读消息
        recent_history = self.history_messages[-limit:] if len(self.history_messages) > limit else self.history_messages
        return recent_history


@dataclass
class MessageManagerStats(BaseDataModel):
    """消息管理器统计信息"""

    total_streams: int = 0
    active_streams: int = 0
    total_unread_messages: int = 0
    total_processed_messages: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def uptime(self) -> float:
        """运行时间"""
        return time.time() - self.start_time


@dataclass
class StreamStats(BaseDataModel):
    """聊天流统计信息"""

    stream_id: str
    is_active: bool
    unread_count: int
    history_count: int
    last_check_time: float
    has_active_task: bool
