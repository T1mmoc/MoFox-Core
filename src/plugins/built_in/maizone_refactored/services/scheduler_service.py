# -*- coding: utf-8 -*-
"""
定时任务服务
根据日程表定时发送说说。
"""
import asyncio
import datetime
import random
import traceback
from typing import Callable

from src.common.logger import get_logger
from src.manager.schedule_manager import schedule_manager
from src.common.database.sqlalchemy_database_api import get_db_session
from src.common.database.sqlalchemy_models import MaiZoneScheduleStatus

from .qzone_service import QZoneService

logger = get_logger('MaiZone.SchedulerService')


class SchedulerService:
    """
    定时任务管理器，负责根据全局日程表定时触发说说发送任务。
    """
    
    def __init__(self, get_config: Callable, qzone_service: QZoneService):
        """
        初始化定时任务服务。

        :param get_config: 用于获取插件配置的函数。
        :param qzone_service: QQ空间服务实例，用于执行发送任务。
        """
        self.get_config = get_config
        self.qzone_service = qzone_service
        self.is_running = False
        self.task = None

    async def start(self):
        """启动定时任务的主循环。"""
        if self.is_running:
            logger.warning("定时任务已在运行中，无需重复启动。")
            return
        self.is_running = True
        self.task = asyncio.create_task(self._schedule_loop())
        logger.info("基于日程表的说说定时发送任务已启动。")

    async def stop(self):
        """停止定时任务的主循环。"""
        if not self.is_running:
            return
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass  # 任务取消是正常操作
        logger.info("基于日程表的说说定时发送任务已停止。")

    async def _schedule_loop(self):
        """
        定时任务的核心循环。
        每隔一段时间检查当前是否有日程活动，并判断是否需要触发发送流程。
        """
        while self.is_running:
            try:
                # 1. 检查定时任务总开关是否开启
                if not self.get_config("schedule.enable_schedule", False):
                    await asyncio.sleep(60)  # 如果被禁用，则每分钟检查一次状态
                    continue
                
                # 2. 获取当前时间的日程活动
                current_activity = schedule_manager.get_current_activity()
                logger.info(current_activity)
                if current_activity:
                    now = datetime.datetime.now()
                    hour_str = now.strftime("%Y-%m-%d %H")
                    
                    # 3. 检查这个小时的这个活动是否已经处理过，防止重复发送
                    if not await self._is_processed(hour_str, current_activity):
                        logger.info(f"检测到新的日程活动: '{current_activity}'，准备发送说说。")
                        
                        # 4. 调用QZoneService执行完整的发送流程
                        result = await self.qzone_service.send_feed_from_activity(current_activity)
                        
                        # 5. 将处理结果记录到数据库
                        await self._mark_as_processed(
                            hour_str, 
                            current_activity, 
                            result.get("success", False), 
                            result.get("message", "")
                        )
                
                # 6. 计算并等待一个随机的时间间隔
                min_minutes = self.get_config("schedule.random_interval_min_minutes", 5)
                max_minutes = self.get_config("schedule.random_interval_max_minutes", 15)
                wait_seconds = random.randint(min_minutes * 60, max_minutes * 60)
                logger.info(f"下一次检查将在 {wait_seconds / 60:.2f} 分钟后进行。")
                await asyncio.sleep(wait_seconds)
                
            except asyncio.CancelledError:
                logger.info("定时任务循环被取消。")
                break
            except Exception as e:
                logger.error(f"定时任务循环中发生未知错误: {e}\n{traceback.format_exc()}")
                await asyncio.sleep(300)  # 发生错误后，等待一段时间再重试

    async def _is_processed(self, hour_str: str, activity: str) -> bool:
        """
        检查指定的任务（某个小时的某个活动）是否已经被成功处理过。

        :param hour_str: 时间字符串，格式为 "YYYY-MM-DD HH"。
        :param activity: 活动名称。
        :return: 如果已处理过，返回 True，否则返回 False。
        """
        try:
            with get_db_session() as session:
                record = session.query(MaiZoneScheduleStatus).filter(
                    MaiZoneScheduleStatus.datetime_hour == hour_str,
                    MaiZoneScheduleStatus.is_processed == True
                ).first()
                return record is not None
        except Exception as e:
            logger.error(f"检查日程处理状态时发生数据库错误: {e}")
            return False  # 数据库异常时，默认为未处理，允许重试

    async def _mark_as_processed(self, hour_str: str, activity: str, success: bool, content: str):
        """
        将任务的处理状态和结果写入数据库。

        :param hour_str: 时间字符串。
        :param activity: 活动名称。
        :param success: 发送是否成功。
        :param content: 最终发送的说说内容或错误信息。
        """
        try:
            with get_db_session() as session:
                # 查找是否已存在该记录
                record = session.query(MaiZoneScheduleStatus).filter(
                    MaiZoneScheduleStatus.datetime_hour == hour_str
                ).first()
                
                if record:
                    # 如果存在，则更新状态
                    record.is_processed = True
                    record.processed_at = datetime.datetime.now()
                    record.send_success = success
                    record.story_content = content
                else:
                    # 如果不存在，则创建新记录
                    new_record = MaiZoneScheduleStatus(
                        datetime_hour=hour_str,
                        activity=activity,
                        is_processed=True,
                        processed_at=datetime.datetime.now(),
                        story_content=content,
                        send_success=success
                    )
                    session.add(new_record)
                session.commit()
                logger.info(f"已更新日程处理状态: {hour_str} - {activity} - 成功: {success}")
        except Exception as e:
            logger.error(f"更新日程处理状态时发生数据库错误: {e}")