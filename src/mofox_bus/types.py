from __future__ import annotations

from typing import Any, Dict, List, Literal, NotRequired, TypedDict

MessageDirection = Literal["incoming", "outgoing"]
Role = Literal["user", "assistant", "system", "tool", "platform"]
ContentType = Literal[
    "text",
    "image",
    "audio",
    "file",
    "video",
    "event",
    "command",
    "system",
]

EventType = Literal[
    "message_created",
    "message_updated",
    "message_deleted",
    "member_join",
    "member_leave",
    "typing",
    "reaction_add",
    "reaction_remove",
]


class TextContent(TypedDict, total=False):
    type: Literal["text"]
    text: str
    markdown: NotRequired[bool]
    entities: NotRequired[List[Dict[str, Any]]]


class ImageContent(TypedDict, total=False):
    type: Literal["image"]
    url: str
    mime_type: NotRequired[str]
    width: NotRequired[int]
    height: NotRequired[int]
    file_id: NotRequired[str]


class FileContent(TypedDict, total=False):
    type: Literal["file"]
    url: str
    mime_type: NotRequired[str]
    file_name: NotRequired[str]
    file_size: NotRequired[int]
    file_id: NotRequired[str]


class AudioContent(TypedDict, total=False):
    type: Literal["audio"]
    url: str
    mime_type: NotRequired[str]
    duration_ms: NotRequired[int]
    file_id: NotRequired[str]


class VideoContent(TypedDict, total=False):
    type: Literal["video"]
    url: str
    mime_type: NotRequired[str]
    duration_ms: NotRequired[int]
    width: NotRequired[int]
    height: NotRequired[int]
    file_id: NotRequired[str]


class EventContent(TypedDict):
    type: Literal["event"]
    event_type: EventType
    raw: Dict[str, Any]


class CommandContent(TypedDict, total=False):
    type: Literal["command"]
    name: str
    args: Dict[str, Any]


class SystemContent(TypedDict):
    type: Literal["system"]
    text: str


Content = (
    TextContent
    | ImageContent
    | FileContent
    | AudioContent
    | VideoContent
    | EventContent
    | CommandContent
    | SystemContent
)


class SenderInfo(TypedDict, total=False):
    user_id: str
    role: Role
    display_name: NotRequired[str]
    avatar_url: NotRequired[str]
    raw: NotRequired[Dict[str, Any]]


class ChannelInfo(TypedDict, total=False):
    channel_id: str
    channel_type: Literal[
        "private",
        "group",
        "supergroup",
        "channel",
        "dm",
        "room",
        "thread",
    ]
    title: NotRequired[str]
    workspace_id: NotRequired[str]
    raw: NotRequired[Dict[str, Any]]


class MessageEnvelope(TypedDict, total=False):
    id: str
    direction: MessageDirection
    platform: str
    timestamp_ms: int
    channel: ChannelInfo
    sender: SenderInfo
    content: Content
    conversation_id: str
    thread_id: NotRequired[str]
    reply_to_message_id: NotRequired[str]
    correlation_id: NotRequired[str]
    is_edited: NotRequired[bool]
    is_ephemeral: NotRequired[bool]
    raw_platform_message: NotRequired[Dict[str, Any]]
    metadata: NotRequired[Dict[str, Any]]
    schema_version: NotRequired[int]


__all__ = [
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
]
