"""
MoFox 内部通用消息总线实现。

该模块导出 TypedDict 消息模型、序列化工具、传输层封装以及适配器辅助工具，
供核心进程与各类平台适配器共享。
"""

from . import codec, types
from .adapter_utils import (
    AdapterTransportOptions,
    BaseAdapter,
    BatchDispatcher,
    CoreMessageSink,
    HttpAdapterOptions,
    InProcessCoreSink,
    WebSocketLike,
    WebSocketAdapterOptions,
)
from .api import MessageClient, MessageServer
from .codec import dumps_message, dumps_messages, loads_message, loads_messages
from .message_models import BaseMessageInfo, FormatInfo, GroupInfo, MessageBase, Seg, TemplateInfo, UserInfo
from .router import RouteConfig, Router, TargetConfig
from .runtime import MessageProcessingError, MessageRoute, MessageRuntime
from .types import (
    AudioContent,
    ChannelInfo,
    CommandContent,
    Content,
    ContentType,
    EventContent,
    EventType,
    FileContent,
    ImageContent,
    MessageDirection,
    MessageEnvelope,
    Role,
    SenderInfo,
    SystemContent,
    TextContent,
    VideoContent,
)

__all__ = [
    # TypedDict model
    "AudioContent",
    "ChannelInfo",
    "CommandContent",
    "Content",
    "ContentType",
    "EventContent",
    "EventType",
    "FileContent",
    "ImageContent",
    "MessageDirection",
    "MessageEnvelope",
    "Role",
    "SenderInfo",
    "SystemContent",
    "TextContent",
    "VideoContent",
    # Codec helpers
    "codec",
    "dumps_message",
    "dumps_messages",
    "loads_message",
    "loads_messages",
    # Runtime / routing
    "MessageRoute",
    "MessageRuntime",
    "MessageProcessingError",
    # Message dataclasses
    "Seg",
    "GroupInfo",
    "UserInfo",
    "FormatInfo",
    "TemplateInfo",
    "BaseMessageInfo",
    "MessageBase",
    # Server/client/router
    "MessageServer",
    "MessageClient",
    "Router",
    "RouteConfig",
    "TargetConfig",
    # Adapter helpers
    "AdapterTransportOptions",
    "BaseAdapter",
    "BatchDispatcher",
    "CoreMessageSink",
    "InProcessCoreSink",
    "WebSocketLike",
    "WebSocketAdapterOptions",
    "HttpAdapterOptions",
]
