"""统一消息发送器"""

from __future__ import annotations

import asyncio
import traceback
from typing import TYPE_CHECKING

from rich.traceback import install

from mofox_wire import MessageEnvelope

from src.chat.message_receive.message_processor import process_message_from_dict
from src.chat.message_receive.storage import MessageStorage
from src.chat.utils.utils import calculate_typing_time, truncate_message
from src.common.data_models.database_data_model import DatabaseMessages, DatabaseUserInfo
from src.common.logger import get_logger
from src.config.config import global_config

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream

install(extra_lines=3)

logger = get_logger("sender")


async def send_envelope(
    envelope: MessageEnvelope,
    chat_stream: ChatStream | None = None,
    db_message: DatabaseMessages | None = None,
    show_log: bool = True,
) -> bool:
    """发送消息"""
    message_preview = truncate_message(
        (db_message.processed_plain_text or "" if db_message else str(envelope.get("message_segment", ""))),
        max_length=120,
    )

    try:
        from src.common.core_sink_manager import get_core_sink_manager

        manager = get_core_sink_manager()
        await manager.send_outgoing(envelope)

        if show_log:
            logger.info(f"已将消息 '{message_preview}' 发送到平台'{envelope.get('platform')}'")

        try:
            from src.plugin_system.base.component_types import EventType
            from src.plugin_system.core.event_manager import event_manager

            if chat_stream:
                event_manager.emit_event(
                    EventType.AFTER_SEND,
                    permission_group="SYSTEM",
                    stream_id=chat_stream.stream_id,
                    message=db_message or envelope,
                )
        except Exception as event_error:
            logger.error(f"触发 AFTER_SEND 事件时出错: {event_error}")

        return True

    except Exception as e:
        logger.error(f"发送消息 '{message_preview}' 到平台'{envelope.get('platform')}' 失败: {e!s}")
        traceback.print_exc()
        raise


class HeartFCSender:
    """发送消息并负责存储、上下文更新等后续处理."""

    async def send_message(
        self,
        envelope: MessageEnvelope,
        chat_stream: "ChatStream",
        *,
        typing: bool = False,
        storage_message: bool = True,
        show_log: bool = True,
        thinking_start_time: float = 0.0,
        display_message: str | None = None,
        storage_user_info: "DatabaseUserInfo | None" = None,
    ) -> bool:
        if not chat_stream:
            logger.error("消息缺少 chat_stream，无法发送")
            raise ValueError("消息缺少 chat_stream，无法发送")

        try:
            db_message = await process_message_from_dict(
                message_dict=envelope,
                stream_id=chat_stream.stream_id,
                platform=chat_stream.platform,
            )

            # 如果提供了用于存储的用户信息，则覆盖
            if storage_message and storage_user_info:
                db_message.user_info.user_id = storage_user_info.user_id
                db_message.user_info.user_nickname = storage_user_info.user_nickname
                db_message.user_info.user_cardname = storage_user_info.user_cardname
                db_message.user_info.platform = storage_user_info.platform

            # 使用调用方指定的展示文本
            if display_message:
                db_message.display_message = display_message
            if db_message.processed_plain_text is None:
                db_message.processed_plain_text = ""

            # 填充基础字段，确保上下文和存储一致
            db_message.is_read = True
            db_message.should_reply = False
            db_message.should_act = False
            if db_message.interest_value is None:
                db_message.interest_value = 0.5

            db_message.chat_info.create_time = chat_stream.create_time
            db_message.chat_info.last_active_time = chat_stream.last_active_time

            # 可选的打字机等待
            if typing:
                typing_time = calculate_typing_time(
                    input_string=db_message.processed_plain_text or "",
                    thinking_start_time=thinking_start_time,
                    is_emoji=bool(getattr(db_message, "is_emoji", False)),
                )
                await asyncio.sleep(typing_time)

            await send_envelope(envelope, chat_stream=chat_stream, db_message=db_message, show_log=show_log)

            if storage_message:
                await MessageStorage.store_message(db_message, chat_stream)

                # 将发送的消息写入上下文历史
                try:
                    if chat_stream and chat_stream.context and global_config and global_config.chat:
                        context = chat_stream.context
                        chat_config = global_config.chat
                        if chat_config:
                            max_context_size = getattr(chat_config, "max_context_size", 40)
                        else:
                            max_context_size = 40

                        if len(context.history_messages) >= max_context_size:
                            context.history_messages = context.history_messages[1:]
                            logger.debug(f"[{chat_stream.stream_id}] Send API发送前移除 1 条历史消息以控制上下文大小")

                        context.history_messages.append(db_message)
                        logger.debug(f"[{chat_stream.stream_id}] Send API消息已写入上下文: {db_message.message_id}")
                except Exception as context_error:
                    logger.warning(f"[{chat_stream.stream_id}] 将消息写入上下文失败: {context_error}")

            return True

        except Exception as e:
            logger.error(f"[{chat_stream.stream_id}] 发送或存储消息时出错: {e}")
            raise
