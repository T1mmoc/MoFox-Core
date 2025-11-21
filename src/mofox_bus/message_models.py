from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional


@dataclass
class Seg:
    """
    消息段，表示一段文本/图片/结构化内容。
    """

    type: str
    data: Any

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Seg":
        seg_type = data.get("type")
        seg_data = data.get("data")
        if seg_type == "seglist" and isinstance(seg_data, list):
            seg_data = [Seg.from_dict(item) for item in seg_data]
        return cls(type=seg_type, data=seg_data)

    def to_dict(self) -> Dict[str, Any]:
        if self.type == "seglist" and isinstance(self.data, list):
            payload = [seg.to_dict() if isinstance(seg, Seg) else seg for seg in self.data]
        else:
            payload = self.data
        return {"type": self.type, "data": payload}


@dataclass
class GroupInfo:
    platform: Optional[str] = None
    group_id: Optional[str] = None
    group_name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["GroupInfo"]:
        if not data or data.get("group_id") is None:
            return None
        return cls(
            platform=data.get("platform"),
            group_id=data.get("group_id"),
            group_name=data.get("group_name"),
        )


@dataclass
class UserInfo:
    platform: Optional[str] = None
    user_id: Optional[str] = None
    user_nickname: Optional[str] = None
    user_cardname: Optional[str] = None
    user_avatar: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "UserInfo":
        return cls(
            platform=data.get("platform"),
            user_id=data.get("user_id"),
            user_nickname=data.get("user_nickname"),
            user_cardname=data.get("user_cardname"),
            user_avatar=data.get("user_avatar"),
        )


@dataclass
class FormatInfo:
    content_format: Optional[List[str]] = None
    accept_format: Optional[List[str]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["FormatInfo"]:
        if not data:
            return None
        return cls(
            content_format=data.get("content_format"),
            accept_format=data.get("accept_format"),
        )


@dataclass
class TemplateInfo:
    template_items: Optional[Dict[str, str]] = None
    template_name: Optional[Dict[str, str]] = None
    template_default: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> Optional["TemplateInfo"]:
        if not data:
            return None
        return cls(
            template_items=data.get("template_items"),
            template_name=data.get("template_name"),
            template_default=data.get("template_default", True),
        )


@dataclass
class BaseMessageInfo:
    platform: Optional[str] = None
    message_id: Optional[str] = None
    time: Optional[float] = None
    group_info: Optional[GroupInfo] = None
    user_info: Optional[UserInfo] = None
    format_info: Optional[FormatInfo] = None
    template_info: Optional[TemplateInfo] = None
    additional_config: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        if self.platform is not None:
            result["platform"] = self.platform
        if self.message_id is not None:
            result["message_id"] = self.message_id
        if self.time is not None:
            result["time"] = self.time
        if self.additional_config is not None:
            result["additional_config"] = self.additional_config
        if self.group_info is not None:
            result["group_info"] = self.group_info.to_dict()
        if self.user_info is not None:
            result["user_info"] = self.user_info.to_dict()
        if self.format_info is not None:
            result["format_info"] = self.format_info.to_dict()
        if self.template_info is not None:
            result["template_info"] = self.template_info.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseMessageInfo":
        return cls(
            platform=data.get("platform"),
            message_id=data.get("message_id"),
            time=data.get("time"),
            additional_config=data.get("additional_config"),
            group_info=GroupInfo.from_dict(data.get("group_info", {})),
            user_info=UserInfo.from_dict(data.get("user_info", {})),
            format_info=FormatInfo.from_dict(data.get("format_info", {})),
            template_info=TemplateInfo.from_dict(data.get("template_info", {})),
        )


@dataclass
class MessageBase:
    message_info: BaseMessageInfo
    message_segment: Seg
    raw_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "message_info": self.message_info.to_dict(),
            "message_segment": self.message_segment.to_dict(),
        }
        if self.raw_message is not None:
            payload["raw_message"] = self.raw_message
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MessageBase":
        return cls(
            message_info=BaseMessageInfo.from_dict(data.get("message_info", {})),
            message_segment=Seg.from_dict(data.get("message_segment", {})),
            raw_message=data.get("raw_message"),
        )


__all__ = [
    "BaseMessageInfo",
    "FormatInfo",
    "GroupInfo",
    "MessageBase",
    "Seg",
    "TemplateInfo",
    "UserInfo",
]
