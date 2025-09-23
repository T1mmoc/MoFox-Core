"""
消息管理模块
管理每个聊天流的上下文信息，包含历史记录和未读消息，定期检查并处理新消息
"""

import asyncio
import time
import traceback
from typing import Dict, Optional, Any, TYPE_CHECKING

from src.common.logger import get_logger
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.data_models.message_manager_data_model import StreamContext, MessageManagerStats, StreamStats
from src.chat.chatter_manager import ChatterManager
from src.chat.planner_actions.action_manager import ChatterActionManager
from src.plugin_system.base.component_types import ChatMode

if TYPE_CHECKING:
    from src.common.data_models.message_manager_data_model import StreamContext

logger = get_logger("message_manager")


class MessageManager:
    """消息管理器"""

    def __init__(self, check_interval: float = 5.0):
        self.stream_contexts: Dict[str, StreamContext] = {}
        self.check_interval = check_interval  # 检查间隔（秒）
        self.is_running = False
        self.manager_task: Optional[asyncio.Task] = None

        # 统计信息
        self.stats = MessageManagerStats()

        # 初始化chatter manager
        self.action_manager = ChatterActionManager()
        self.chatter_manager = ChatterManager(self.action_manager)

    async def start(self):
        """启动消息管理器"""
        if self.is_running:
            logger.warning("消息管理器已经在运行")
            return

        self.is_running = True
        self.manager_task = asyncio.create_task(self._manager_loop())
        logger.info("消息管理器已启动")

    async def stop(self):
        """停止消息管理器"""
        if not self.is_running:
            return

        self.is_running = False

        # 停止所有流处理任务
        for context in self.stream_contexts.values():
            if context.processing_task and not context.processing_task.done():
                context.processing_task.cancel()

        # 停止管理器任务
        if self.manager_task and not self.manager_task.done():
            self.manager_task.cancel()

        logger.info("消息管理器已停止")

    def add_message(self, stream_id: str, message: DatabaseMessages):
        """添加消息到指定聊天流"""
        # 获取或创建流上下文
        if stream_id not in self.stream_contexts:
            self.stream_contexts[stream_id] = StreamContext(stream_id=stream_id)
            self.stats.total_streams += 1

        context = self.stream_contexts[stream_id]
        context.set_chat_mode(ChatMode.FOCUS)
        context.add_message(message)

        logger.debug(f"添加消息到聊天流 {stream_id}: {message.message_id}")

    async def _manager_loop(self):
        """管理器主循环"""
        while self.is_running:
            try:
                await self._check_all_streams()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"消息管理器循环出错: {e}")
                traceback.print_exc()

    async def _check_all_streams(self):
        """检查所有聊天流"""
        active_streams = 0
        total_unread = 0

        for stream_id, context in self.stream_contexts.items():
            if not context.is_active:
                continue

            active_streams += 1

            # 检查是否有未读消息
            unread_messages = context.get_unread_messages()
            if unread_messages:
                total_unread += len(unread_messages)

                # 如果没有处理任务，创建一个
                if not context.processing_task or context.processing_task.done():
                    context.processing_task = asyncio.create_task(self._process_stream_messages(stream_id))

        # 更新统计
        self.stats.active_streams = active_streams
        self.stats.total_unread_messages = total_unread

    async def _process_stream_messages(self, stream_id: str):
        """处理指定聊天流的消息"""
        if stream_id not in self.stream_contexts:
            return

        context = self.stream_contexts[stream_id]

        try:
            # 获取未读消息
            unread_messages = context.get_unread_messages()
            if not unread_messages:
                return

            logger.debug(f"开始处理聊天流 {stream_id} 的 {len(unread_messages)} 条未读消息")

            # 直接使用StreamContext对象进行处理
            if unread_messages:
                try:
                    # 记录当前chat type用于调试
                    logger.debug(f"聊天流 {stream_id} 检测到的chat type: {context.chat_type.value}")

                    # 发送到chatter manager，传递StreamContext对象
                    results = await self.chatter_manager.process_stream_context(stream_id, context)

                    # 处理结果，标记消息为已读
                    if results.get("success", False):
                        self._clear_all_unread_messages(context)
                        logger.debug(f"聊天流 {stream_id} 处理成功，清除了 {len(unread_messages)} 条未读消息")
                    else:
                        logger.warning(f"聊天流 {stream_id} 处理失败: {results.get('error_message', '未知错误')}")

                except Exception as e:
                    logger.error(f"处理聊天流 {stream_id} 时发生异常，将清除所有未读消息: {e}")
                    # 出现异常时也清除未读消息，避免重复处理
                    self._clear_all_unread_messages(context)
                    raise

            logger.debug(f"聊天流 {stream_id} 消息处理完成")

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"处理聊天流 {stream_id} 消息时出错: {e}")
            traceback.print_exc()

    def deactivate_stream(self, stream_id: str):
        """停用聊天流"""
        if stream_id in self.stream_contexts:
            context = self.stream_contexts[stream_id]
            context.is_active = False

            # 取消处理任务
            if context.processing_task and not context.processing_task.done():
                context.processing_task.cancel()

            logger.info(f"停用聊天流: {stream_id}")

    def activate_stream(self, stream_id: str):
        """激活聊天流"""
        if stream_id in self.stream_contexts:
            self.stream_contexts[stream_id].is_active = True
            logger.info(f"激活聊天流: {stream_id}")

    def get_stream_stats(self, stream_id: str) -> Optional[StreamStats]:
        """获取聊天流统计"""
        if stream_id not in self.stream_contexts:
            return None

        context = self.stream_contexts[stream_id]
        return StreamStats(
            stream_id=stream_id,
            is_active=context.is_active,
            unread_count=len(context.get_unread_messages()),
            history_count=len(context.history_messages),
            last_check_time=context.last_check_time,
            has_active_task=context.processing_task and not context.processing_task.done(),
        )

    def get_manager_stats(self) -> Dict[str, Any]:
        """获取管理器统计"""
        return {
            "total_streams": self.stats.total_streams,
            "active_streams": self.stats.active_streams,
            "total_unread_messages": self.stats.total_unread_messages,
            "total_processed_messages": self.stats.total_processed_messages,
            "uptime": self.stats.uptime,
            "start_time": self.stats.start_time,
        }

    def cleanup_inactive_streams(self, max_inactive_hours: int = 24):
        """清理不活跃的聊天流"""
        current_time = time.time()
        max_inactive_seconds = max_inactive_hours * 3600

        inactive_streams = []
        for stream_id, context in self.stream_contexts.items():
            if current_time - context.last_check_time > max_inactive_seconds and not context.get_unread_messages():
                inactive_streams.append(stream_id)

        for stream_id in inactive_streams:
            self.deactivate_stream(stream_id)
            del self.stream_contexts[stream_id]
            logger.info(f"清理不活跃聊天流: {stream_id}")

    def _clear_all_unread_messages(self, context: StreamContext):
        """清除指定上下文中的所有未读消息，防止意外情况导致消息一直未读"""
        unread_messages = context.get_unread_messages()
        if not unread_messages:
            return

        logger.warning(f"正在清除 {len(unread_messages)} 条未读消息")

        # 将所有未读消息标记为已读并移动到历史记录
        for msg in unread_messages[:]:  # 使用切片复制避免迭代时修改列表
            try:
                context.mark_message_as_read(msg.message_id)
                self.stats.total_processed_messages += 1
                logger.info(f"强制清除消息 {msg.message_id}，标记为已读")
            except Exception as e:
                logger.error(f"清除消息 {msg.message_id} 时出错: {e}")


# 创建全局消息管理器实例
message_manager = MessageManager()
