# -*- coding: utf-8 -*-
"""
消息内容处理模块

负责消息内容的提取、清理和预处理
"""

import re
from typing import Optional

from src.common.logger import get_logger
from src.chat.message_receive.message import MessageRecv

logger = get_logger("anti_injector.message_processor")


class MessageProcessor:
    """消息内容处理器"""
    
    def __init__(self):
        """初始化消息处理器"""
        pass
    
    def extract_text_content(self, message: MessageRecv) -> str:
        """提取消息中的文本内容，过滤掉引用的历史内容
        
        Args:
            message: 接收到的消息对象
            
        Returns:
            提取的文本内容
        """
        # 主要检测处理后的纯文本
        processed_text = message.processed_plain_text
        
        # 检查是否包含引用消息
        new_content = self.extract_new_content_from_reply(processed_text)
        text_parts = [new_content]
        
        # 如果有原始消息，也加入检测
        if hasattr(message, 'raw_message') and message.raw_message:
            text_parts.append(str(message.raw_message))
        
        # 合并所有文本内容
        return " ".join(filter(None, text_parts))
    
    def extract_new_content_from_reply(self, full_text: str) -> str:
        """从包含引用的完整消息中提取用户新增的内容
        
        Args:
            full_text: 完整的消息文本
            
        Returns:
            用户新增的内容（去除引用部分）
        """
        # 引用消息的格式：[回复<用户昵称:用户ID> 的消息：引用的消息内容]
        # 使用正则表达式匹配引用部分
        reply_pattern = r'\[回复<[^>]*> 的消息：[^\]]*\]'
        
        # 移除所有引用部分
        new_content = re.sub(reply_pattern, '', full_text).strip()
        
        # 如果移除引用后内容为空，说明这是一个纯引用消息，返回一个标识
        if not new_content:
            logger.debug("检测到纯引用消息，无用户新增内容")
            return "[纯引用消息]"
        
        # 记录处理结果
        if new_content != full_text:
            logger.debug(f"从引用消息中提取新内容: '{new_content}' (原始: '{full_text}')")
        
        return new_content
    
    def check_whitelist(self, message: MessageRecv, whitelist: list) -> Optional[tuple]:
        """检查用户白名单
        
        Args:
            message: 消息对象
            whitelist: 白名单配置
            
        Returns:
            如果在白名单中返回结果元组，否则返回None
        """
        user_id = message.message_info.user_info.user_id
        platform = message.message_info.platform
        
        # 检查用户白名单：格式为 [[platform, user_id], ...]
        for whitelist_entry in whitelist:
            if len(whitelist_entry) == 2 and whitelist_entry[0] == platform and whitelist_entry[1] == user_id:
                logger.debug(f"用户 {platform}:{user_id} 在白名单中，跳过检测")
                return True, None, "用户白名单"
        
        return None
