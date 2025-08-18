# -*- coding: utf-8 -*-
"""
LLM反注入系统主模块

本模块实现了完整的LLM反注入防护流程，按照设计的流程图进行消息处理：
1. 检查系统是否启用
2. 黑白名单验证
3. 规则集检测
4. LLM二次分析（可选）
5. 处理模式选择（严格/宽松）
6. 消息加盾或丢弃
"""

import time
import asyncio
from typing import Optional, Tuple, Dict, Any
import datetime

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.message_receive.message import MessageRecv
from .config import DetectionResult, ProcessResult
from .detector import PromptInjectionDetector
from .shield import MessageShield

# 数据库相关导入
from src.common.database.sqlalchemy_models import BanUser, AntiInjectionStats, get_db_session

logger = get_logger("anti_injector")


class AntiPromptInjector:
    """LLM反注入系统主类"""
    
    def __init__(self):
        """初始化反注入系统"""
        self.config = global_config.anti_prompt_injection
        self.detector = PromptInjectionDetector()
        self.shield = MessageShield()
        
    async def _get_or_create_stats(self):
        """获取或创建统计记录"""
        try:
            with get_db_session() as session:
                # 获取最新的统计记录，如果没有则创建
                stats = session.query(AntiInjectionStats).order_by(AntiInjectionStats.id.desc()).first()
                if not stats:
                    stats = AntiInjectionStats()
                    session.add(stats)
                    session.commit()
                    session.refresh(stats)
                return stats
        except Exception as e:
            logger.error(f"获取统计记录失败: {e}")
            return None
    
    async def _update_stats(self, **kwargs):
        """更新统计数据"""
        try:
            with get_db_session() as session:
                stats = session.query(AntiInjectionStats).order_by(AntiInjectionStats.id.desc()).first()
                if not stats:
                    stats = AntiInjectionStats()
                    session.add(stats)
                
                # 更新统计字段
                for key, value in kwargs.items():
                    if key == 'processing_time_delta':
                        # 处理时间累加 - 确保不为None
                        if stats.processing_time_total is None:
                            stats.processing_time_total = 0.0
                        stats.processing_time_total += value
                        continue
                    elif key == 'last_processing_time':
                        # 直接设置最后处理时间
                        stats.last_processing_time = value
                        continue
                    elif hasattr(stats, key):
                        if key in ['total_messages', 'detected_injections', 
                                  'blocked_messages', 'shielded_messages', 'error_count']:
                            # 累加类型的字段 - 确保不为None
                            current_value = getattr(stats, key)
                            if current_value is None:
                                setattr(stats, key, value)
                            else:
                                setattr(stats, key, current_value + value)
                        else:
                            # 直接设置的字段
                            setattr(stats, key, value)
                
                session.commit()
        except Exception as e:
            logger.error(f"更新统计数据失败: {e}")
    
    async def process_message(self, message: MessageRecv) -> Tuple[ProcessResult, Optional[str], Optional[str]]:
        """处理消息并返回结果
        
        Args:
            message: 接收到的消息对象
            
        Returns:
            Tuple[ProcessResult, Optional[str], Optional[str]]: 
            - 处理结果状态枚举
            - 处理后的消息内容（如果有修改）
            - 处理结果说明
        """
        start_time = time.time()
        
        try:
            # 统计更新
            await self._update_stats(total_messages=1)
            
            # 1. 检查系统是否启用
            if not self.config.enabled:
                return ProcessResult.ALLOWED, None, "反注入系统未启用"
            
            # 2. 检查用户是否被封禁
            if self.config.auto_ban_enabled:
                user_id = message.message_info.user_info.user_id
                platform = message.message_info.platform
                ban_result = await self._check_user_ban(user_id, platform)
                if ban_result is not None:
                    return ProcessResult.BLOCKED_BAN, None, ban_result[2]
            
            # 3. 用户白名单检测
            whitelist_result = self._check_whitelist(message)
            if whitelist_result is not None:
                return ProcessResult.ALLOWED, None, whitelist_result[2]
            
            # 4. 内容检测
            detection_result = await self.detector.detect(message.processed_plain_text)
            
            # 5. 处理检测结果
            if detection_result.is_injection:
                await self._update_stats(detected_injections=1)
                
                # 记录违规行为
                if self.config.auto_ban_enabled:
                    user_id = message.message_info.user_info.user_id
                    platform = message.message_info.platform
                    await self._record_violation(user_id, platform, detection_result)
                
                # 根据处理模式决定如何处理
                if self.config.process_mode == "strict":
                    # 严格模式：直接拒绝
                    await self._update_stats(blocked_messages=1)
                    return ProcessResult.BLOCKED_INJECTION, None, f"检测到提示词注入攻击，消息已拒绝 (置信度: {detection_result.confidence:.2f})"
                
                elif self.config.process_mode == "lenient":
                    # 宽松模式：加盾处理
                    if self.shield.is_shield_needed(detection_result.confidence, detection_result.matched_patterns):
                        await self._update_stats(shielded_messages=1)
                        
                        # 创建加盾后的消息内容
                        shielded_content = self.shield.create_shielded_message(
                            message.processed_plain_text, 
                            detection_result.confidence
                        )
                        
                        summary = self.shield.create_safety_summary(detection_result.confidence, detection_result.matched_patterns)
                        
                        return ProcessResult.SHIELDED, shielded_content, f"检测到可疑内容已加盾处理: {summary}"
                    else:
                        # 置信度不高，允许通过
                        return ProcessResult.ALLOWED, None, "检测到轻微可疑内容，已允许通过"
            
            # 6. 正常消息
            return ProcessResult.ALLOWED, None, "消息检查通过"
            
        except Exception as e:
            logger.error(f"反注入处理异常: {e}", exc_info=True)
            await self._update_stats(error_count=1)
            
            # 异常情况下直接阻止消息
            return ProcessResult.BLOCKED_INJECTION, None, f"反注入系统异常，消息已阻止: {str(e)}"
            
        finally:
            # 更新处理时间统计
            process_time = time.time() - start_time
            await self._update_stats(processing_time_delta=process_time, last_processing_time=process_time)
    
    async def _check_user_ban(self, user_id: str, platform: str) -> Optional[Tuple[bool, Optional[str], str]]:
        """检查用户是否被封禁
        
        Args:
            user_id: 用户ID
            platform: 平台名称
            
        Returns:
            如果用户被封禁则返回拒绝结果，否则返回None
        """
        try:
            with get_db_session() as session:
                ban_record = session.query(BanUser).filter_by(user_id=user_id, platform=platform).first()
                
                if ban_record:
                    # 只有违规次数达到阈值时才算被封禁
                    if ban_record.violation_num >= self.config.auto_ban_violation_threshold:
                        # 检查封禁是否过期
                        ban_duration = datetime.timedelta(hours=self.config.auto_ban_duration_hours)
                        if datetime.datetime.now() - ban_record.created_at < ban_duration:
                            remaining_time = ban_duration - (datetime.datetime.now() - ban_record.created_at)
                            return False, None, f"用户被封禁中，剩余时间: {remaining_time}"
                        else:
                            # 封禁已过期，重置违规次数
                            ban_record.violation_num = 0
                            ban_record.created_at = datetime.datetime.now()
                            session.commit()
                            logger.info(f"用户 {platform}:{user_id} 封禁已过期，违规次数已重置")
                
            return None
            
        except Exception as e:
            logger.error(f"检查用户封禁状态失败: {e}", exc_info=True)
            return None
    
    async def _record_violation(self, user_id: str, platform: str, detection_result: DetectionResult):
        """记录用户违规行为
        
        Args:
            user_id: 用户ID
            platform: 平台名称
            detection_result: 检测结果
        """
        try:
            with get_db_session() as session:
                # 查找或创建违规记录
                ban_record = session.query(BanUser).filter_by(user_id=user_id, platform=platform).first()
                
                if ban_record:
                    ban_record.violation_num += 1
                    ban_record.reason = f"提示词注入攻击 (置信度: {detection_result.confidence:.2f})"
                else:
                    ban_record = BanUser(
                        platform=platform,
                        user_id=user_id,
                        violation_num=1,
                        reason=f"提示词注入攻击 (置信度: {detection_result.confidence:.2f})",
                        created_at=datetime.datetime.now()
                    )
                    session.add(ban_record)
                
                session.commit()
                
                # 检查是否需要自动封禁
                if ban_record.violation_num >= self.config.auto_ban_violation_threshold:
                    logger.warning(f"用户 {platform}:{user_id} 违规次数达到 {ban_record.violation_num}，触发自动封禁")
                    # 只有在首次达到阈值时才更新封禁开始时间
                    if ban_record.violation_num == self.config.auto_ban_violation_threshold:
                        ban_record.created_at = datetime.datetime.now()
                    session.commit()
                else:
                    logger.info(f"用户 {platform}:{user_id} 违规记录已更新，当前违规次数: {ban_record.violation_num}")
                
        except Exception as e:
            logger.error(f"记录违规行为失败: {e}", exc_info=True)
    
    def _check_whitelist(self, message: MessageRecv) -> Optional[Tuple[bool, Optional[str], str]]:
        """检查用户白名单"""
        user_id = message.message_info.user_info.user_id
        platform = message.message_info.platform
        
        # 检查用户白名单：格式为 [[platform, user_id], ...]
        for whitelist_entry in self.config.whitelist:
            if len(whitelist_entry) == 2 and whitelist_entry[0] == platform and whitelist_entry[1] == user_id:
                logger.debug(f"用户 {platform}:{user_id} 在白名单中，跳过检测")
                return True, None, "用户白名单"
        
        return None

    async def _detect_injection(self, message: MessageRecv) -> DetectionResult:
        """检测提示词注入"""
        # 获取待检测的文本内容
        text_content = self._extract_text_content(message)
        
        if not text_content:
            return DetectionResult(
                is_injection=False,
                confidence=0.0,
                reason="无文本内容"
            )
        
        # 执行检测
        result = await self.detector.detect(text_content)
        
        logger.debug(f"检测结果: 注入={result.is_injection}, "
                    f"置信度={result.confidence:.2f}, "
                    f"方法={result.detection_method}")
        
        return result
    
    def _extract_text_content(self, message: MessageRecv) -> str:
        """提取消息中的文本内容"""
        # 主要检测处理后的纯文本
        text_parts = [message.processed_plain_text]
        
        # 如果有原始消息，也加入检测
        if hasattr(message, 'raw_message') and message.raw_message:
            text_parts.append(str(message.raw_message))
        
        # 合并所有文本内容
        return " ".join(filter(None, text_parts))
    
    async def _process_detection_result(self, message: MessageRecv, 
                                      detection_result: DetectionResult) -> Tuple[bool, Optional[str], str]:
        """处理检测结果"""
        if not detection_result.is_injection:
            return True, None, "检测通过"
        
        # 确定处理模式
        if self.config.process_mode == "strict":
            # 严格模式：直接丢弃消息
            logger.warning(f"严格模式：丢弃危险消息 (置信度: {detection_result.confidence:.2f})")
            await self._update_stats(blocked_messages=1)
            return False, None, f"严格模式阻止 - {detection_result.reason}"
        
        elif self.config.process_mode == "lenient":
            # 宽松模式：消息加盾
            if self.shield.is_shield_needed(detection_result.confidence, detection_result.matched_patterns):
                original_text = message.processed_plain_text
                shielded_text = self.shield.shield_message(
                    original_text, 
                    detection_result.matched_patterns
                )
                
                logger.info(f"宽松模式：消息已加盾 (置信度: {detection_result.confidence:.2f})")
                await self._update_stats(shielded_messages=1)
                
                # 创建处理摘要
                summary = self.shield.create_safety_summary(
                    len(original_text),
                    len(shielded_text), 
                    detection_result.confidence,
                    detection_result.matched_patterns
                )
                
                return True, shielded_text, f"宽松模式加盾 - {summary}"
            else:
                # 置信度不够，允许通过
                return True, None, f"置信度不足，允许通过 - {detection_result.reason}"
        
        # 默认允许通过
        return True, None, "默认允许通过"
    
    def _log_processing_result(self, message: MessageRecv, detection_result: DetectionResult,
                             process_result: Tuple[bool, Optional[str], str], processing_time: float):

        
        allowed, modified_content, reason = process_result
        user_id = message.message_info.user_info.user_id
        group_info = message.message_info.group_info
        group_id = group_info.group_id if group_info else "私聊"
        
        log_data = {
            "user_id": user_id,
            "group_id": group_id,
            "message_length": len(message.processed_plain_text),
            "is_injection": detection_result.is_injection,
            "confidence": detection_result.confidence,
            "detection_method": detection_result.detection_method,
            "matched_patterns": len(detection_result.matched_patterns),
            "processing_time": f"{processing_time:.3f}s",
            "allowed": allowed,
            "modified": modified_content is not None,
            "reason": reason
        }
        
        if detection_result.is_injection:
            logger.warning(f"检测到注入攻击: {log_data}")
        else:
            logger.debug(f"消息检测通过: {log_data}")
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        try:
            stats = await self._get_or_create_stats()
            
            # 计算派生统计信息 - 处理None值
            total_messages = stats.total_messages or 0
            detected_injections = stats.detected_injections or 0
            processing_time_total = stats.processing_time_total or 0.0
            
            detection_rate = (detected_injections / total_messages * 100) if total_messages > 0 else 0
            avg_processing_time = (processing_time_total / total_messages) if total_messages > 0 else 0
            
            current_time = datetime.datetime.now()
            uptime = current_time - stats.start_time
            
            return {
                "uptime": str(uptime),
                "total_messages": total_messages,
                "detected_injections": detected_injections,
                "blocked_messages": stats.blocked_messages or 0,
                "shielded_messages": stats.shielded_messages or 0,
                "detection_rate": f"{detection_rate:.2f}%",
                "average_processing_time": f"{avg_processing_time:.3f}s",
                "last_processing_time": f"{stats.last_processing_time:.3f}s" if stats.last_processing_time else "0.000s",
                "error_count": stats.error_count or 0
            }
        except Exception as e:
            logger.error(f"获取统计信息失败: {e}")
            return {"error": f"获取统计信息失败: {e}"}
    
    async def reset_stats(self):
        """重置统计信息"""
        try:
            with get_db_session() as session:
                # 删除现有统计记录
                session.query(AntiInjectionStats).delete()
                session.commit()
                logger.info("统计信息已重置")
        except Exception as e:
            logger.error(f"重置统计信息失败: {e}")


# 全局反注入器实例
_global_injector: Optional[AntiPromptInjector] = None


def get_anti_injector() -> AntiPromptInjector:
    """获取全局反注入器实例"""
    global _global_injector
    if _global_injector is None:
        _global_injector = AntiPromptInjector()
    return _global_injector


def initialize_anti_injector() -> AntiPromptInjector:
    """初始化反注入器"""
    global _global_injector
    _global_injector = AntiPromptInjector()
    return _global_injector
