"""消息处理器 - 将 Napcat OneBot 消息转换为 MessageEnvelope"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from mofox_wire import MessageBuilder
from src.common.logger import get_logger
from src.plugin_system.apis import config_api
from mofox_wire import (
    MessageEnvelope,
    SegPayload,
    MessageInfoPayload,
    UserInfoPayload,
    GroupInfoPayload,
)

from ...event_models import ACCEPT_FORMAT, QQ_FACE
from ..utils import (
    get_group_info,
    get_image_base64,
    get_self_info,
    get_member_info,
    get_message_detail,
)

if TYPE_CHECKING:
    from ....plugin import NapcatAdapter

logger = get_logger("napcat_adapter.message_handler")


class MessageHandler:
    """处理来自 Napcat 的消息事件"""

    def __init__(self, adapter: "NapcatAdapter"):
        self.adapter = adapter
        self.plugin_config: Optional[Dict[str, Any]] = None

    def set_plugin_config(self, config: Dict[str, Any]) -> None:
        """设置插件配置"""
        self.plugin_config = config

    async def handle_raw_message(self, raw: Dict[str, Any]):
        """
        处理原始消息并转换为 MessageEnvelope

        Args:
            raw: OneBot 原始消息数据

        Returns:
            MessageEnvelope (dict)
        """

        message_type = raw.get("message_type")
        message_id = str(raw.get("message_id", ""))
        message_time = time.time()

        msg_builder = MessageBuilder()

        # 构造用户信息
        sender_info = raw.get("sender", {})

        (
            msg_builder.direction("incoming")
            .message_id(message_id)
            .timestamp_ms(int(message_time * 1000))
            .from_user(
                user_id=str(sender_info.get("user_id", "")),
                platform="qq",
                nickname=sender_info.get("nickname", ""),
                cardname=sender_info.get("card", ""),
                user_avatar=sender_info.get("avatar", ""),
            )
        )

        # 构造群组信息（如果是群消息）
        if message_type == "group":
            group_id = raw.get("group_id")
            if group_id:
                fetched_group_info = await get_group_info(group_id)
                (
                    msg_builder.from_group(
                        group_id=str(group_id),
                        platform="qq",
                        name=(
                            fetched_group_info.get("group_name", "")
                            if fetched_group_info
                            else raw.get("group_name", "")
                        ),
                    )
                )

        # 解析消息段
        message_segments = raw.get("message", [])
        seg_list: List[SegPayload] = []

        for segment in message_segments:
            seg_message = await self.handle_single_segment(segment, raw)
            if seg_message:
                seg_list.append(seg_message)

        msg_builder.format_info(
            content_format=[seg["type"] for seg in seg_list],
            accept_format=ACCEPT_FORMAT,
        )

        msg_builder.seg_list(seg_list)

        return msg_builder.build()

    async def handle_single_segment(
        self, segment: dict, raw_message: dict, in_reply: bool = False
    ) -> SegPayload | None:
        """
        处理单一消息段并转换为 MessageEnvelope

        Args:
            segment: 单一原始消息段
            raw_message: 完整的原始消息数据

        Returns:
            SegPayload | List[SegPayload] | None
        """
        seg_type = segment.get("type")
        seg_data: dict = segment.get("data", {})
        match seg_type:
            case "text":
                return {"type": "text", "data": seg_data.get("text", "")}
            case "image":
                image_sub_type = seg_data.get("sub_type")
                try:
                    image_base64 = await get_image_base64(seg_data.get("url", ""))
                except Exception as e:
                    logger.error(f"图片消息处理失败: {str(e)}")
                    return None
                if image_sub_type == 0:
                    """这部分认为是图片"""
                    return {"type": "image", "data": image_base64}
                elif image_sub_type not in [4, 9]:
                    """这部分认为是表情包"""
                    return {"type": "emoji", "data": image_base64}
                else:
                    logger.warning(f"不支持的图片子类型：{image_sub_type}")
                    return None
            case "face":
                message_data: dict = segment.get("data", {})
                face_raw_id: str = str(message_data.get("id"))
                if face_raw_id in QQ_FACE:
                    face_content: str = QQ_FACE.get(face_raw_id, "[未知表情]")
                    return {"type": "text", "data": face_content}
                else:
                    logger.warning(f"不支持的表情：{face_raw_id}")
                    return None
            case "at":
                if seg_data:
                    qq_id = seg_data.get("qq")
                    self_id = raw_message.get("self_id")
                    group_id = raw_message.get("group_id")
                    if str(self_id) == str(qq_id):
                        logger.debug("机器人被at")
                        self_info = await get_self_info()
                        if self_info:
                            # 返回包含昵称和用户ID的at格式，便于后续处理
                            return {
                                "type": "at",
                                "data": f"{self_info.get('nickname')}:{self_info.get('user_id')}",
                            }
                        else:
                            return None
                    else:
                        if qq_id and group_id:
                            member_info = await get_member_info(
                                group_id=group_id, user_id=qq_id
                            )
                            if member_info:
                                # 返回包含昵称和用户ID的at格式，便于后续处理
                                return {
                                    "type": "at",
                                    "data": f"{member_info.get('nickname')}:{member_info.get('user_id')}",
                                }
                            else:
                                return None
            case "emoji":
                seg_data = segment.get("id", "")
            case "reply":
                if not in_reply:
                    message_id = None
                    if seg_data:
                        message_id = seg_data.get("id")
                    else:
                        return None
                    message_detail = await get_message_detail(message_id)
                    if not message_detail:
                        logger.warning("获取被引用的消息详情失败")
                        return None
                    reply_message = await self.handle_single_segment(
                        message_detail, raw_message, in_reply=True
                    )
                    if reply_message is None:
                        reply_message = [
                            {"type": "text", "data": "[无法获取被引用的消息]"}
                        ]
                    sender_info: dict = message_detail.get("sender", {})
                    sender_nickname: str = sender_info.get("nickname", "")
                    sender_id = sender_info.get("user_id")
                    if not sender_nickname:
                        logger.warning("无法获取被引用的人的昵称，返回默认值")
                        return {
                            "type": "text",
                            "data": f"[回复<未知用户>：{reply_message}]，说：",
                        }

                    else:
                        if sender_id:
                            return {
                                "type": "text",
                                "data": f"[回复<{sender_nickname}({sender_id})>：{reply_message}]，说：",
                            }
                        else:
                            return {
                                "type": "text",
                                "data": f"[回复<{sender_nickname}>：{reply_message}]，说：",
                            }

            case "voice":
                seg_data = segment.get("url", "")
            case _:
                logger.warning(f"Unsupported segment type: {seg_type}")
