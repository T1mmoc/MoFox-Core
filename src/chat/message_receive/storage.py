import re
import json
import traceback
from typing import Union

import orjson
from sqlalchemy import select, desc, update

from src.common.database.sqlalchemy_models import Messages, Images, get_db_session
from src.common.logger import get_logger
from .chat_stream import ChatStream
from .message import MessageSending, MessageRecv

logger = get_logger("message_storage")


class MessageStorage:
    @staticmethod
    def _serialize_keywords(keywords) -> str:
        """将关键词列表序列化为JSON字符串"""
        if isinstance(keywords, list):
            return orjson.dumps(keywords).decode("utf-8")
        return "[]"

    @staticmethod
    def _deserialize_keywords(keywords_str: str) -> list:
        """将JSON字符串反序列化为关键词列表"""
        if not keywords_str:
            return []
        try:
            return orjson.loads(keywords_str)
        except (orjson.JSONDecodeError, TypeError):
            return []

    @staticmethod
    async def store_message(message: Union[MessageSending, MessageRecv], chat_stream: ChatStream) -> None:
        """存储消息到数据库"""
        try:
            # 过滤敏感信息的正则模式
            pattern = r"<MainRule>.*?</MainRule>|<schedule>.*?</schedule>|<UserMessage>.*?</UserMessage>"

            processed_plain_text = message.processed_plain_text

            if processed_plain_text:
                processed_plain_text = await MessageStorage.replace_image_descriptions(processed_plain_text)
                filtered_processed_plain_text = re.sub(pattern, "", processed_plain_text, flags=re.DOTALL)
            else:
                filtered_processed_plain_text = ""

            if isinstance(message, MessageSending):
                display_message = message.display_message
                if display_message:
                    filtered_display_message = re.sub(pattern, "", display_message, flags=re.DOTALL)
                else:
                    filtered_display_message = ""
                interest_value = 0
                is_mentioned = False
                reply_to = message.reply_to
                priority_mode = ""
                priority_info = {}
                is_emoji = False
                is_picid = False
                is_notify = False
                is_command = False
                key_words = ""
                key_words_lite = ""
            else:
                filtered_display_message = ""
                interest_value = message.interest_value
                is_mentioned = message.is_mentioned
                reply_to = ""
                priority_mode = message.priority_mode
                priority_info = message.priority_info
                is_emoji = message.is_emoji
                is_picid = message.is_picid
                is_notify = message.is_notify
                is_command = message.is_command
                # 序列化关键词列表为JSON字符串
                key_words = MessageStorage._serialize_keywords(message.key_words)
                key_words_lite = MessageStorage._serialize_keywords(message.key_words_lite)

            chat_info_dict = chat_stream.to_dict()
            user_info_dict = message.message_info.user_info.to_dict()  # type: ignore

            # message_id 现在是 TextField，直接使用字符串值
            msg_id = message.message_info.message_id

            # 安全地获取 group_info, 如果为 None 则视为空字典
            group_info_from_chat = chat_info_dict.get("group_info") or {}
            # 安全地获取 user_info, 如果为 None 则视为空字典 (以防万一)
            user_info_from_chat = chat_info_dict.get("user_info") or {}

            # 将priority_info字典序列化为JSON字符串，以便存储到数据库的Text字段
            priority_info_json = orjson.dumps(priority_info).decode("utf-8") if priority_info else None

            # 获取数据库会话

            new_message = Messages(
                message_id=msg_id,
                time=float(message.message_info.time),
                chat_id=chat_stream.stream_id,
                reply_to=reply_to,
                is_mentioned=is_mentioned,
                chat_info_stream_id=chat_info_dict.get("stream_id"),
                chat_info_platform=chat_info_dict.get("platform"),
                chat_info_user_platform=user_info_from_chat.get("platform"),
                chat_info_user_id=user_info_from_chat.get("user_id"),
                chat_info_user_nickname=user_info_from_chat.get("user_nickname"),
                chat_info_user_cardname=user_info_from_chat.get("user_cardname"),
                chat_info_group_platform=group_info_from_chat.get("platform"),
                chat_info_group_id=group_info_from_chat.get("group_id"),
                chat_info_group_name=group_info_from_chat.get("group_name"),
                chat_info_create_time=float(chat_info_dict.get("create_time", 0.0)),
                chat_info_last_active_time=float(chat_info_dict.get("last_active_time", 0.0)),
                user_platform=user_info_dict.get("platform"),
                user_id=user_info_dict.get("user_id"),
                user_nickname=user_info_dict.get("user_nickname"),
                user_cardname=user_info_dict.get("user_cardname"),
                processed_plain_text=filtered_processed_plain_text,
                priority_mode=priority_mode,
                priority_info=priority_info_json,
                is_emoji=is_emoji,
                is_picid=is_picid,
            )
            async with get_db_session() as session:
                await session.add(new_message)
                await session.commit()

        except Exception:
            logger.exception("存储消息失败")
            logger.error(f"消息：{message}")
            traceback.print_exc()

    @staticmethod
    async def update_message(message):
        """更新消息ID"""
        try:
            mmc_message_id = message.message_info.message_id
            qq_message_id = None

            logger.debug(f"尝试更新消息ID: {mmc_message_id}, 消息段类型: {message.message_segment.type}")

            # 根据消息段类型提取message_id
            if message.message_segment.type == "notify":
                qq_message_id = message.message_segment.data.get("id")
            elif message.message_segment.type == "text":
                qq_message_id = message.message_segment.data.get("id")
            elif message.message_segment.type == "reply":
                qq_message_id = message.message_segment.data.get("id")
                logger.debug(f"从reply消息段获取到消息ID: {qq_message_id}")
            elif message.message_segment.type == "adapter_response":
                logger.debug("适配器响应消息，不需要更新ID")
                return
            elif message.message_segment.type == "adapter_command":
                logger.debug("适配器命令消息，不需要更新ID")
                return
            else:
                logger.debug(f"未知的消息段类型: {message.message_segment.type}，跳过ID更新")
                return

            if not qq_message_id:
                logger.debug(f"消息段类型 {message.message_segment.type} 中未找到有效的message_id，跳过更新")
                logger.debug(f"消息段数据: {message.message_segment.data}")
                return

            async with get_db_session() as session:
                matched_message = (
                    await session.execute(
                        select(Messages).where(Messages.message_id == mmc_message_id).order_by(desc(Messages.time))
                    )
                ).scalar()

                if matched_message:
                    await session.execute(
                        update(Messages).where(Messages.id == matched_message.id).values(message_id=qq_message_id)
                    )
                    await session.commit()
                    #  会在上下文管理器中自动调用
                    logger.debug(f"更新消息ID成功: {matched_message.message_id} -> {qq_message_id}")
                else:
                    logger.warning(f"未找到匹配的消息记录: {mmc_message_id}")

        except Exception as e:
            logger.error(f"更新消息ID失败: {e}")
            logger.error(
                f"消息信息: message_id={getattr(message.message_info, 'message_id', 'N/A')}, "
                f"segment_type={getattr(message.message_segment, 'type', 'N/A')}"
            )

    async def replace_image_descriptions(text: str) -> str:
        """将[图片：描述]替换为[picid:image_id]"""
        # 先检查文本中是否有图片标记
        pattern = r"\[图片：([^\]]+)\]"
        matches = list(re.finditer(pattern, text))

        if not matches:
            logger.debug("文本中没有图片标记，直接返回原文本")
            return text

        new_text = ""
        last_end = 0
        for match in matches:
            new_text += text[last_end : match.start()]
            description = match.group(1).strip()
            try:
                from src.common.database.sqlalchemy_models import get_db_session

                async with get_db_session() as session:
                    image_record = (
                        await session.execute(
                            select(Images).where(Images.description == description).order_by(desc(Images.timestamp))
                        )
                    ).scalar()
                    if image_record:
                        new_text += f"[picid:{image_record.image_id}]"
                    else:
                        new_text += match.group(0)
            except Exception:
                new_text += match.group(0)
            last_end = match.end()
        new_text += text[last_end:]
        return new_text
