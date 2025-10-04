import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from src.config.config import global_config
from src.plugin_system.apis import message_api, chat_api, person_api
from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.logger import get_logger

logger = get_logger("HTTP消息API")

router = APIRouter()

@router.get("/messages/recent")
async def get_message_stats(
    days: int = Query(1, ge=1, description="指定查询过去多少天的数据"),
    message_type: Literal["all", "sent", "received"] = Query("all", description="筛选消息类型: 'sent' (BOT发送的), 'received' (BOT接收的), or 'all' (全部)")
):
    """
    获取BOT在指定天数内的消息统计数据。
    """
    try:
        end_time = time.time()
        start_time = end_time - (days * 24 * 3600)

        messages = await message_api.get_messages_by_time(start_time, end_time)

        sent_count = 0
        received_count = 0
        bot_qq = str(global_config.bot.qq_account)

        for msg in messages:
            if msg.get("user_id") == bot_qq:
                sent_count += 1
            else:
                received_count += 1
        if message_type == "sent":
            return {"days": days, "message_type": message_type, "count": sent_count}
        elif message_type == "received":
            return {"days": days, "message_type": message_type, "count": received_count}
        else:
            return {
                "days": days,
                "message_type": message_type,
                "sent_count": sent_count,
                "received_count": received_count,
                "total_count": len(messages)
            }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/messages/stats_by_chat")
async def get_message_stats_by_chat(
    days: int = Query(1, ge=1, description="指定查询过去多少天的数据"),
    group_by_user: bool = Query(False, description="是否按用户进行分组统计"),
    format: bool = Query(False, description="是否格式化输出，包含群聊和用户信息"),
):
    """
    获取BOT在指定天数内按聊天流或按用户统计的消息数据。
    """
    try:
        end_time = time.time()
        start_time = end_time - (days * 24 * 3600)
        messages = await message_api.get_messages_by_time(start_time, end_time)
        bot_qq = str(global_config.bot.qq_account)

        messages = [msg for msg in messages if msg.get("user_id") != bot_qq]

        stats = {}

        for msg in messages:
            chat_id = msg.get("chat_id", "unknown")
            user_id = msg.get("user_id")

            if chat_id not in stats:
                stats[chat_id] = {
                    "total_stats": {"total": 0},
                    "user_stats": {}
                }

            stats[chat_id]["total_stats"]["total"] += 1

            if group_by_user:
                if user_id not in stats[chat_id]["user_stats"]:
                    stats[chat_id]["user_stats"][user_id] = 0
                
                stats[chat_id]["user_stats"][user_id] += 1

        if not group_by_user:
            stats = {chat_id: data["total_stats"] for chat_id, data in stats.items()}

        if format:
            chat_manager = get_chat_manager()
            formatted_stats = {}
            for chat_id, data in stats.items():
                stream = chat_manager.streams.get(chat_id)
                chat_name = "未知会话"
                if stream:
                    if stream.group_info and stream.group_info.group_name:
                        chat_name = stream.group_info.group_name
                    elif stream.user_info and stream.user_info.user_nickname:
                        chat_name = stream.user_info.user_nickname
                else:
                    chat_name = f"未知会话 ({chat_id})"

                formatted_data = {
                    "chat_name": chat_name,
                    "total_stats": data if not group_by_user else data["total_stats"],
                }

                if group_by_user and "user_stats" in data:
                    formatted_data["user_stats"] = {}
                    for user_id, count in data["user_stats"].items():
                        person_id = person_api.get_person_id("qq", user_id)
                        nickname = await person_api.get_person_value(person_id, "nickname", "未知用户")
                        formatted_data["user_stats"][user_id] = {
                            "nickname": nickname,
                            "count": count
                        }
                
                formatted_stats[chat_id] = formatted_data
            return formatted_stats

        return stats

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/messages/bot_stats_by_chat")
async def get_bot_message_stats_by_chat(
    days: int = Query(1, ge=1, description="指定查询过去多少天的数据"),
    format: bool = Query(False, description="是否格式化输出，包含群聊和用户信息"),
):
    """
    获取BOT在指定天数内按聊天流统计的已发送消息数据。
    """
    try:
        end_time = time.time()
        start_time = end_time - (days * 24 * 3600)
        messages = await message_api.get_messages_by_time(start_time, end_time)
        bot_qq = str(global_config.bot.qq_account)

        # 筛选出机器人发送的消息
        bot_messages = [msg for msg in messages if msg.get("user_id") == bot_qq]

        stats = {}
        for msg in bot_messages:
            chat_id = msg.get("chat_id", "unknown")
            if chat_id not in stats:
                stats[chat_id] = 0
            stats[chat_id] += 1

        if format:
            chat_manager = get_chat_manager()
            formatted_stats = {}
            for chat_id, count in stats.items():
                stream = chat_manager.streams.get(chat_id)
                chat_name = f"未知会话 ({chat_id})"
                if stream:
                    if stream.group_info and stream.group_info.group_name:
                        chat_name = stream.group_info.group_name
                    elif stream.user_info and stream.user_info.user_nickname:
                        chat_name = stream.user_info.user_nickname
                
                formatted_stats[chat_id] = {
                    "chat_name": chat_name,
                    "count": count
                }
            return formatted_stats

        return stats

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))