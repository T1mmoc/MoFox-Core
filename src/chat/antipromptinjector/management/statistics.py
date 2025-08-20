# -*- coding: utf-8 -*-
"""
反注入系统统计模块

负责统计数据的收集、更新和查询
"""

import datetime
from typing import Dict, Any, Optional

from src.common.logger import get_logger
from src.common.database.sqlalchemy_models import AntiInjectionStats, get_db_session
from src.config.config import global_config

logger = get_logger("anti_injector.statistics")


class AntiInjectionStatistics:
    """反注入系统统计管理类"""
    
    def __init__(self):
        """初始化统计管理器"""
        self.session_start_time = datetime.datetime.now()
        """当前会话开始时间"""
    
    async def get_or_create_stats(self):
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
    
    async def update_stats(self, **kwargs):
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
                        stats.last_process_time = value
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
    
    async def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        try:
            # 检查反注入系统是否启用
            if not global_config.anti_prompt_injection.enabled:
                return {
                    "status": "disabled",
                    "message": "反注入系统未启用",
                    "uptime": "N/A",
                    "total_messages": 0,
                    "detected_injections": 0,
                    "blocked_messages": 0,
                    "shielded_messages": 0,
                    "detection_rate": "N/A",
                    "average_processing_time": "N/A",
                    "last_processing_time": "N/A",
                    "error_count": 0
                }
            
            stats = await self.get_or_create_stats()
            
            # 计算派生统计信息 - 处理None值
            total_messages = stats.total_messages or 0
            detected_injections = stats.detected_injections or 0
            processing_time_total = stats.processing_time_total or 0.0
            
            detection_rate = (detected_injections / total_messages * 100) if total_messages > 0 else 0
            avg_processing_time = (processing_time_total / total_messages) if total_messages > 0 else 0
            
            # 使用当前会话开始时间计算运行时间，而不是数据库中的start_time
            # 这样可以避免重启后显示错误的运行时间
            current_time = datetime.datetime.now()
            uptime = current_time - self.session_start_time
            
            return {
                "status": "enabled",
                "uptime": str(uptime),
                "total_messages": total_messages,
                "detected_injections": detected_injections,
                "blocked_messages": stats.blocked_messages or 0,
                "shielded_messages": stats.shielded_messages or 0,
                "detection_rate": f"{detection_rate:.2f}%",
                "average_processing_time": f"{avg_processing_time:.3f}s",
                "last_processing_time": f"{stats.last_process_time:.3f}s" if stats.last_process_time else "0.000s",
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
