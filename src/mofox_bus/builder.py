from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List

from .types import GroupInfoPayload, MessageEnvelope, MessageInfoPayload, SegPayload, UserInfoPayload


class MessageBuilder:
    """
    流式构建 MessageEnvelope 的助手工具，提供类型安全的构建方法。

    使用示例:
        msg = (
            MessageBuilder()
            .text("Hello")
            .image("http://example.com/1.png")
            .to_user("123", platform="qq")
            .build()
        )
    """

    def __init__(self) -> None:
        self._direction: str = "outgoing"
        self._message_info: MessageInfoPayload = {}
        self._segments: List[SegPayload] = []
        self._metadata: Dict[str, Any] | None = None
        self._timestamp_ms: int | None = None
        self._message_id: str | None = None

    def direction(self, value: str) -> "MessageBuilder":
        self._direction = value
        return self

    def message_id(self, value: str) -> "MessageBuilder":
        self._message_id = value
        return self

    def timestamp_ms(self, value: int | None = None) -> "MessageBuilder":
        self._timestamp_ms = value or int(time.time() * 1000)
        return self

    def metadata(self, value: Dict[str, Any]) -> "MessageBuilder":
        self._metadata = value
        return self

    def platform(self, value: str) -> "MessageBuilder":
        self._message_info["platform"] = value
        return self

    def from_user(self, user_id: str, *, platform: str | None = None, nickname: str | None = None) -> "MessageBuilder":
        if platform:
            self.platform(platform)
        user_info: UserInfoPayload = {"user_id": user_id}
        if nickname:
            user_info["user_nickname"] = nickname
        self._message_info["user_info"] = user_info
        return self

    def from_group(self, group_id: str, *, platform: str | None = None, name: str | None = None) -> "MessageBuilder":
        if platform:
            self.platform(platform)
        group_info: GroupInfoPayload = {"group_id": group_id}
        if name:
            group_info["group_name"] = name
        self._message_info["group_info"] = group_info
        return self

    def seg(self, type_: str, data: Any) -> "MessageBuilder":
        self._segments.append({"type": type_, "data": data})
        return self

    def text(self, content: str) -> "MessageBuilder":
        return self.seg("text", content)

    def image(self, url: str) -> "MessageBuilder":
        return self.seg("image", url)

    def reply(self, target_message_id: str) -> "MessageBuilder":
        return self.seg("reply", target_message_id)

    def raw_segment(self, segment: SegPayload) -> "MessageBuilder":
        self._segments.append(segment)
        return self

    def build(self) -> MessageEnvelope:
        """构建最终的消息信封"""
        # 设置 message_info 默认值
        if not self._segments:
            raise ValueError("需要至少添加一个消息段才能构建消息")
        if self._message_id is None:
            self._message_id = str(uuid.uuid4())
        info = dict(self._message_info)
        info.setdefault("message_id", self._message_id)
        info.setdefault("time", time.time())

        segments = [seg.copy() if isinstance(seg, dict) else seg for seg in self._segments]
        envelope: MessageEnvelope = {
            "direction": self._direction,  # type: ignore[assignment]
            "message_info": info,
            "message_segment": segments[0] if len(segments) == 1 else list(segments),
        }
        if self._metadata is not None:
            envelope["metadata"] = self._metadata
        if self._timestamp_ms is not None:
            envelope["timestamp_ms"] = self._timestamp_ms
        return envelope


__all__ = ["MessageBuilder"]
