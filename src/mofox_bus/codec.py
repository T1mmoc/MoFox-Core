from __future__ import annotations

import json as _stdlib_json
from typing import Any, Dict, Iterable, List

try:
    import orjson as _json_impl
except Exception:  # pragma: no cover - fallback when orjson is unavailable
    _json_impl = None

from .types import MessageEnvelope

DEFAULT_SCHEMA_VERSION = 1


def _dumps(obj: Any) -> bytes:
    if _json_impl is not None:
        return _json_impl.dumps(obj)
    return _stdlib_json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _loads(data: bytes) -> Dict[str, Any]:
    if _json_impl is not None:
        return _json_impl.loads(data)
    return _stdlib_json.loads(data.decode("utf-8"))


def dumps_message(msg: MessageEnvelope) -> bytes:
    """
    将单条消息序列化为 JSON bytes。
    """
    sanitized = _strip_raw_bytes(msg)
    if "schema_version" not in sanitized:
        sanitized["schema_version"] = DEFAULT_SCHEMA_VERSION
    return _dumps(sanitized)

def dumps_messages(messages: Iterable[MessageEnvelope]) -> bytes:
    """
    将批量消息序列化为 JSON bytes。
    """
    payload = {
        "schema_version": DEFAULT_SCHEMA_VERSION,
        "items": [_strip_raw_bytes(msg) for msg in messages],
    }
    return _dumps(payload)

def loads_message(data: bytes | str) -> MessageEnvelope:
    """
    反序列化单条消息。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    obj = _loads(data)
    return _upgrade_schema_if_needed(obj)


def loads_messages(data: bytes | str) -> List[MessageEnvelope]:
    """
    反序列化批量消息。
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    obj = _loads(data)
    version = obj.get("schema_version", DEFAULT_SCHEMA_VERSION)
    if version != DEFAULT_SCHEMA_VERSION:
        raise ValueError(f"不支持的 schema_version={version}")
    return [_upgrade_schema_if_needed(item) for item in obj.get("items", [])]


def _upgrade_schema_if_needed(obj: Dict[str, Any]) -> MessageEnvelope:
    """
    针对未来的 schema 版本演进预留兼容入口。
    """
    version = obj.get("schema_version", DEFAULT_SCHEMA_VERSION)
    if version == DEFAULT_SCHEMA_VERSION:
        return obj  # type: ignore[return-value]
    raise ValueError(f"不支持的 schema_version={version}")



def _strip_raw_bytes(msg: MessageEnvelope) -> MessageEnvelope:
    if isinstance(msg, dict) and "raw_bytes" in msg:
        new_msg = dict(msg)
        new_msg.pop("raw_bytes", None)
        return new_msg  # type: ignore[return-value]
    return msg

__all__ = [
    "DEFAULT_SCHEMA_VERSION",
    "dumps_message",
    "dumps_messages",
    "loads_message",
    "loads_messages",
]
