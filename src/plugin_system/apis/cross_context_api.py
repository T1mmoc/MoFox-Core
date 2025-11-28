"""
跨群聊上下文API
"""

import time
from typing import Any, TYPE_CHECKING
from src.common.message_repository import find_messages

from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.utils.chat_message_builder import (
    build_readable_messages_with_id,
    get_raw_msg_before_timestamp_with_chat,
)
from src.common.logger import get_logger
from src.common.message_repository import get_user_messages_from_streams
from src.config.config import global_config

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream

logger = get_logger("cross_context_api")


async def build_cross_context_s4u(
    chat_stream: "ChatStream",
    target_user_info: dict[str, Any] | None,
) -> str:
    """
    构建跨群聊/私聊上下文 (S4U模式)。
    优先展示目标用户的私聊记录（双向），其次按时间顺序展示其他群聊记录。
    """
    # 记录S4U上下文构建开始
    logger.debug("[S4U] Starting S4U context build.")
    
    # 检查全局配置是否存在且包含必要部分
    if not global_config or not global_config.cross_context or not global_config.bot:
        logger.error("全局配置尚未初始化或缺少关键配置，无法构建S4U上下文。")
        return ""
    
    # 获取跨上下文配置
    cross_context_config = global_config.cross_context
    
    # 检查目标用户信息和用户ID是否存在
    if not target_user_info or not (user_id := target_user_info.get("user_id")):
        logger.warning(f"[S4U] Failed: target_user_info ({target_user_info}) or user_id is missing.")
        return ""
    
    # 记录目标用户ID
    logger.debug(f"[S4U] Target user ID: {user_id}")

    # 获取聊天管理器实例
    chat_manager = get_chat_manager()
    private_context_block = ""
    group_context_blocks = []

    # --- 1. 优先处理私聊上下文 ---
    # 获取与目标用户的私聊流ID
    private_stream_id = chat_manager.get_stream_id(chat_stream.platform, user_id, is_group=False)
    
    # 如果存在私聊流且不是当前聊天流
    if private_stream_id and private_stream_id != chat_stream.stream_id:
        logger.debug(f"[S4U] Found private chat with target user: {private_stream_id}")
        try:
            # 定义需要获取消息的用户ID列表（目标用户和机器人自己）
            user_ids_to_fetch = [str(user_id), str(global_config.bot.qq_account)]
            
            # 从指定私聊流中获取双方的消息
            messages_by_stream = await get_user_messages_from_streams(
                user_ids=user_ids_to_fetch,
                stream_ids=[private_stream_id],
                timestamp_after=time.time() - (3 * 24 * 60 * 60),  # 最近3天的消息
                limit_per_stream=cross_context_config.s4u_limit,
            )
            
            # 如果获取到了私聊消息
            if private_messages := messages_by_stream.get(private_stream_id):
                chat_name = await chat_manager.get_stream_name(private_stream_id) or "私聊"
                title = f'[以下是您与"{chat_name}"的近期私聊记录]\n'
                
                # 格式化消息为可读字符串
                formatted, _ = await build_readable_messages_with_id(private_messages, timestamp_mode="relative")
                private_context_block = f"{title}{formatted}"
                logger.debug(f"[S4U] Generated private context block of length {len(private_context_block)}.")
        except Exception as e:
            logger.error(f"S4U模式下处理私聊记录失败: {e}")

    # --- 2. 处理其他群聊上下文 ---
    streams_to_scan = []
    
    # 根据S4U配置模式（白名单/黑名单）确定要扫描的聊天范围
    if cross_context_config.s4u_mode == "whitelist":
        # 白名单模式：只扫描在白名单中的聊天
        for chat_str in cross_context_config.s4u_whitelist_chats:
            try:
                platform, chat_type, chat_raw_id = chat_str.split(":")
                is_group = chat_type == "group"
                stream_id = chat_manager.get_stream_id(platform, chat_raw_id, is_group=is_group)
                
                # 排除当前聊和私聊
                if stream_id and stream_id != chat_stream.stream_id and stream_id != private_stream_id:
                    streams_to_scan.append(stream_id)
            except ValueError:
                logger.warning(f"无效的S4U白名单格式: {chat_str}")
    else:  # 黑名单模式
        # 黑名单模式：扫描所有聊天，除了黑名单中的和私聊
        blacklisted_streams = {private_stream_id}
        for chat_str in cross_context_config.s4u_blacklist_chats:
            try:
                platform, chat_type, chat_raw_id = chat_str.split(":")
                is_group = chat_type == "group"
                stream_id = chat_manager.get_stream_id(platform, chat_raw_id, is_group=is_group)
                if stream_id:
                    blacklisted_streams.add(stream_id)
            except ValueError:
                logger.warning(f"无效的S4U黑名单格式: {chat_str}")
        
        # 将不在黑名单中的流添加到扫描列表
        streams_to_scan.extend(
            stream_id for stream_id in chat_manager.streams
            if stream_id != chat_stream.stream_id and stream_id not in blacklisted_streams
        )

    logger.debug(f"[S4U] Found {len(streams_to_scan)} group streams to scan.")

    if streams_to_scan:
        # 获取目标用户在这些群聊中的消息
        messages_by_stream = await get_user_messages_from_streams(
            user_ids=[str(user_id)],
            stream_ids=streams_to_scan,
            timestamp_after=time.time() - (3 * 24 * 60 * 60), # 最近3天
            limit_per_stream=cross_context_config.s4u_limit,
        )

        all_group_messages = []
        # 将所有群聊消息聚合，并附带最新时间戳
        for stream_id, user_messages in messages_by_stream.items():
            if user_messages:
                latest_timestamp = max(msg.get("time", 0) for msg in user_messages)
                all_group_messages.append(
                    {"stream_id": stream_id, "messages": user_messages, "latest_timestamp": latest_timestamp}
                )

        # 按最新消息时间倒序排序
        all_group_messages.sort(key=lambda x: x["latest_timestamp"], reverse=True)

        # 计算群聊上下文的额度
        remaining_limit = cross_context_config.s4u_stream_limit - (1 if private_context_block else 0)
        limited_group_messages = all_group_messages[:remaining_limit]

        # 格式化每个群聊的消息
        for item in limited_group_messages:
            try:
                chat_name = await chat_manager.get_stream_name(item["stream_id"]) or "未知群聊"
                user_name = target_user_info.get("person_name") or target_user_info.get("user_nickname") or user_id
                title = f'[以下是"{user_name}"在"{chat_name}"的近期发言]\n'
                formatted, _ = await build_readable_messages_with_id(item["messages"], timestamp_mode="relative")
                group_context_blocks.append(f"{title}{formatted}")
            except Exception as e:
                logger.error(f"S4U模式下格式化群聊消息失败 (stream: {item['stream_id']}): {e}")

    # --- 3. 组合最终上下文 ---
    # 如果没有任何上下文内容，则返回空
    if not private_context_block and not group_context_blocks:
        logger.debug("[S4U] No context blocks were generated. Returning empty string.")
        return ""

    final_context_parts = []
    # 添加私聊部分
    if private_context_block:
        final_context_parts.append(private_context_block)
    # 添加群聊部分
    if group_context_blocks:
        group_context_str = "\n\n".join(group_context_blocks)
        final_context_parts.append(f"### 其他群聊中的聊天记录\n{group_context_str}")

    # 组合最终的上下文字符串
    final_context = "\n\n".join(final_context_parts) + "\n"
    logger.debug(f"[S4U] Successfully generated S4U context. Total length: {len(final_context)}.")
    return final_context

async def build_cross_context_for_user(
    user_id: str,
    platform: str,
    limit_per_stream: int,
    stream_limit: int,
) -> str:
    """
    构建指定用户的跨群聊/私聊上下文（简化版API）。
    """
    logger.debug(f"[S4U_SIMPLE] Starting simplified S4U context build for user {user_id} on {platform}.")

    if not global_config or not global_config.cross_context or not global_config.bot:
        logger.error("全局配置尚未初始化或缺少关键配置，无法构建S4U上下文。")
        return ""

    chat_manager = get_chat_manager()

    private_context_block = ""
    group_context_blocks = []

    # --- 1. 优先处理私聊上下文 ---
    private_stream_id = chat_manager.get_stream_id(platform, user_id, is_group=False)
    if private_stream_id:
        try:
            user_ids_to_fetch = [str(user_id), str(global_config.bot.qq_account)]
            messages_by_stream = await get_user_messages_from_streams(
                user_ids=user_ids_to_fetch,
                stream_ids=[private_stream_id],
                timestamp_after=time.time() - (3 * 24 * 60 * 60),
                limit_per_stream=limit_per_stream,
            )
            if private_messages := messages_by_stream.get(private_stream_id):
                chat_name = await chat_manager.get_stream_name(private_stream_id) or "私聊"
                title = f'[以下是您与"{chat_name}"的近期私聊记录]\n'
                formatted, _ = await build_readable_messages_with_id(private_messages, timestamp_mode="relative")
                private_context_block = f"{title}{formatted}"
        except Exception as e:
            logger.error(f"[S4U_SIMPLE] 处理私聊记录失败: {e}")

    # --- 2. 处理其他群聊上下文 ---
    streams_to_scan = [
        stream_id for stream_id in chat_manager.streams
        if stream_id != private_stream_id
    ]

    if streams_to_scan:
        messages_by_stream = await get_user_messages_from_streams(
            user_ids=[str(user_id)],
            stream_ids=streams_to_scan,
            timestamp_after=time.time() - (3 * 24 * 60 * 60),
            limit_per_stream=limit_per_stream,
        )

        all_group_messages = []
        for stream_id, user_messages in messages_by_stream.items():
            if user_messages:
                latest_timestamp = max(msg.get("time", 0) for msg in user_messages)
                all_group_messages.append(
                    {"stream_id": stream_id, "messages": user_messages, "latest_timestamp": latest_timestamp}
                )

        all_group_messages.sort(key=lambda x: x["latest_timestamp"], reverse=True)

        remaining_limit = stream_limit - (1 if private_context_block else 0)
        limited_group_messages = all_group_messages[:remaining_limit]

        for item in limited_group_messages:
            try:
                chat_name = await chat_manager.get_stream_name(item["stream_id"]) or "未知群聊"
                user_name = user_id # 简化处理
                title = f'[以下是"{user_name}"在"{chat_name}"的近期发言]\n'
                formatted, _ = await build_readable_messages_with_id(item["messages"], timestamp_mode="relative")
                group_context_blocks.append(f"{title}{formatted}")
            except Exception as e:
                logger.error(f"[S4U_SIMPLE] 格式化群聊消息失败 (stream: {item['stream_id']}): {e}")

    # --- 3. 组合最终上下文 ---
    if not private_context_block and not group_context_blocks:
        return ""

    final_context_parts = []
    if private_context_block:
        final_context_parts.append(private_context_block)
    if group_context_blocks:
        group_context_str = "\n\n".join(group_context_blocks)
        final_context_parts.append(f"### 其他群聊中的聊天记录\n{group_context_str}")

    final_context = "\n\n".join(final_context_parts) + "\n"
    logger.debug(f"[S4U_SIMPLE] Successfully generated context for user {user_id}. Total length: {len(final_context)}.")
    return final_context
