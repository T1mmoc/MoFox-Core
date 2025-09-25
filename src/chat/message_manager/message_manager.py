"""
消息管理模块
管理每个聊天流的上下文信息，包含历史记录和未读消息，定期检查并处理新消息
"""

import asyncio
import random
import time
import traceback
from typing import Dict, Optional, Any, TYPE_CHECKING

from src.common.logger import get_logger
from src.common.data_models.database_data_model import DatabaseMessages
from src.common.data_models.message_manager_data_model import StreamContext, MessageManagerStats, StreamStats
from src.chat.chatter_manager import ChatterManager
from src.chat.planner_actions.action_manager import ChatterActionManager
from .sleep_manager.sleep_manager import SleepManager
from .sleep_manager.wakeup_manager import WakeUpManager
from src.config.config import global_config

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

        # 初始化睡眠和唤醒管理器
        self.sleep_manager = SleepManager()
        self.wakeup_manager = WakeUpManager(self.sleep_manager)

    async def start(self):
        """启动消息管理器"""
        if self.is_running:
            logger.warning("消息管理器已经在运行")
            return

        self.is_running = True
        self.manager_task = asyncio.create_task(self._manager_loop())
        await self.wakeup_manager.start()
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

        await self.wakeup_manager.stop()

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
        """管理器主循环 - 独立聊天流分发周期版本"""
        while self.is_running:
            try:
                # 更新睡眠状态
                await self.sleep_manager.update_sleep_state(self.wakeup_manager)

                # 执行独立分发周期的检查
                await self._check_streams_with_individual_intervals()

                # 计算下次检查时间（使用最小间隔或固定间隔）
                if global_config.chat.dynamic_distribution_enabled:
                    next_check_delay = self._calculate_next_manager_delay()
                else:
                    next_check_delay = self.check_interval

                await asyncio.sleep(next_check_delay)
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

            # 检查是否需要打断现有处理
            await self._check_and_handle_interruption(context, stream_id)

            # --- 睡眠状态检查 ---
            if self.sleep_manager.is_sleeping():
                logger.info(f"Bot正在睡觉，检查聊天流 {stream_id} 是否有唤醒触发器。")

                was_woken_up = False
                is_private = context.is_private_chat()

                for message in unread_messages:
                    is_mentioned = message.is_mentioned or False
                    if is_private or is_mentioned:
                        if self.wakeup_manager.add_wakeup_value(is_private, is_mentioned):
                            was_woken_up = True
                            break  # 一旦被吵醒，就跳出循环并处理消息

                if not was_woken_up:
                    logger.debug(f"聊天流 {stream_id} 中没有唤醒触发器，保持消息未读状态。")
                    return  # 退出，不处理消息

                logger.info(f"Bot被聊天流 {stream_id} 中的消息吵醒，继续处理。")
            # --- 睡眠状态检查结束 ---

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
            has_active_task=bool(context.processing_task and not context.processing_task.done()),
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

    async def _check_and_handle_interruption(self, context: StreamContext, stream_id: str):
        """检查并处理消息打断"""
        if not global_config.chat.interruption_enabled:
            return

        # 检查是否有正在进行的处理任务
        if context.processing_task and not context.processing_task.done():
            # 计算打断概率
            interruption_probability = context.calculate_interruption_probability(
                global_config.chat.interruption_max_limit, global_config.chat.interruption_probability_factor
            )

            # 根据概率决定是否打断
            if random.random() < interruption_probability:
                logger.info(f"聊天流 {stream_id} 触发消息打断，打断概率: {interruption_probability:.2f}")

                # 取消现有任务
                context.processing_task.cancel()
                try:
                    await context.processing_task
                except asyncio.CancelledError:
                    pass

                # 增加打断计数并应用afc阈值降低
                context.increment_interruption_count()
                context.apply_interruption_afc_reduction(global_config.chat.interruption_afc_reduction)
                logger.info(
                    f"聊天流 {stream_id} 已打断，当前打断次数: {context.interruption_count}/{global_config.chat.interruption_max_limit}, afc阈值调整: {context.get_afc_threshold_adjustment()}"
                )
            else:
                logger.debug(f"聊天流 {stream_id} 未触发打断，打断概率: {interruption_probability:.2f}")

    def _calculate_dynamic_distribution_interval(self) -> float:
        """根据所有活跃聊天流的focus_energy动态计算分发周期"""
        if not global_config.chat.dynamic_distribution_enabled:
            return self.check_interval  # 使用固定间隔

        if not self.stream_contexts:
            return self.check_interval  # 默认间隔

        # 计算活跃流的平均focus_energy
        active_streams = [ctx for ctx in self.stream_contexts.values() if ctx.is_active]
        if not active_streams:
            return self.check_interval

        total_focus_energy = 0.0
        max_focus_energy = 0.0
        stream_count = 0

        for context in active_streams:
            if hasattr(context, 'chat_stream') and context.chat_stream:
                focus_energy = context.chat_stream.focus_energy
                total_focus_energy += focus_energy
                max_focus_energy = max(max_focus_energy, focus_energy)
                stream_count += 1

        if stream_count == 0:
            return self.check_interval

        avg_focus_energy = total_focus_energy / stream_count

        # 使用配置参数
        base_interval = global_config.chat.dynamic_distribution_base_interval
        min_interval = global_config.chat.dynamic_distribution_min_interval
        max_interval = global_config.chat.dynamic_distribution_max_interval
        jitter_factor = global_config.chat.dynamic_distribution_jitter_factor

        # 根据平均兴趣度调整间隔
        # 高兴趣度 -> 更频繁检查 (间隔更短)
        # 低兴趣度 -> 较少检查 (间隔更长)
        if avg_focus_energy >= 0.7:
            # 高兴趣度：1-5秒
            interval = base_interval * (1.0 - (avg_focus_energy - 0.7) * 2.0)
        elif avg_focus_energy >= 0.4:
            # 中等兴趣度：5-15秒
            interval = base_interval * (1.0 + (avg_focus_energy - 0.4) * 3.33)
        else:
            # 低兴趣度：15-30秒
            interval = base_interval * (3.0 + (0.4 - avg_focus_energy) * 5.0)

        # 添加随机扰动避免同步
        import random
        jitter = random.uniform(1.0 - jitter_factor, 1.0 + jitter_factor)
        final_interval = interval * jitter

        # 限制在合理范围内
        final_interval = max(min_interval, min(max_interval, final_interval))

        logger.debug(
            f"动态分发周期: {final_interval:.2f}s | "
            f"平均兴趣度: {avg_focus_energy:.3f} | "
            f"活跃流数: {stream_count}"
        )

        return final_interval

    def _calculate_stream_distribution_interval(self, context: StreamContext) -> float:
        """计算单个聊天流的分发周期 - 基于阈值感知的focus_energy"""
        if not global_config.chat.dynamic_distribution_enabled:
            return self.check_interval  # 使用固定间隔

        # 获取该流的focus_energy（新的阈值感知版本）
        focus_energy = 0.5  # 默认值
        avg_message_interest = 0.5  # 默认平均兴趣度

        if hasattr(context, 'chat_stream') and context.chat_stream:
            focus_energy = context.chat_stream.focus_energy
            # 获取平均消息兴趣度用于更精确的计算
            if context.chat_stream.message_count > 0:
                avg_message_interest = context.chat_stream.message_interest_total / context.chat_stream.message_count

        # 获取AFC阈值用于参考，添加None值检查
        reply_threshold = getattr(global_config.affinity_flow, 'reply_action_interest_threshold', 0.4)
        non_reply_threshold = getattr(global_config.affinity_flow, 'non_reply_action_interest_threshold', 0.2)
        high_match_threshold = getattr(global_config.affinity_flow, 'high_match_interest_threshold', 0.8)

        # 使用配置参数
        base_interval = global_config.chat.dynamic_distribution_base_interval
        min_interval = global_config.chat.dynamic_distribution_min_interval
        max_interval = global_config.chat.dynamic_distribution_max_interval
        jitter_factor = global_config.chat.dynamic_distribution_jitter_factor

        # 基于阈值感知的智能分发周期计算
        if avg_message_interest >= high_match_threshold:
            # 超高兴趣度：极快响应 (1-2秒)
            interval_multiplier = 0.3 + (focus_energy - 0.7) * 2.0
        elif avg_message_interest >= reply_threshold:
            # 高兴趣度：快速响应 (2-6秒)
            gap_from_reply = (avg_message_interest - reply_threshold) / (high_match_threshold - reply_threshold)
            interval_multiplier = 0.6 + gap_from_reply * 0.4
        elif avg_message_interest >= non_reply_threshold:
            # 中等兴趣度：正常响应 (6-15秒)
            gap_from_non_reply = (avg_message_interest - non_reply_threshold) / (reply_threshold - non_reply_threshold)
            interval_multiplier = 1.2 + gap_from_non_reply * 1.8
        else:
            # 低兴趣度：缓慢响应 (15-30秒)
            gap_ratio = max(0, avg_message_interest / non_reply_threshold)
            interval_multiplier = 3.0 + (1.0 - gap_ratio) * 3.0

        # 应用focus_energy微调
        energy_adjustment = 1.0 + (focus_energy - 0.5) * 0.5
        interval = base_interval * interval_multiplier * energy_adjustment

        # 添加随机扰动避免同步
        import random
        jitter = random.uniform(1.0 - jitter_factor, 1.0 + jitter_factor)
        final_interval = interval * jitter

        # 限制在合理范围内
        final_interval = max(min_interval, min(max_interval, final_interval))

        # 根据兴趣度级别调整日志级别
        if avg_message_interest >= high_match_threshold:
            log_level = "info"
        elif avg_message_interest >= reply_threshold:
            log_level = "info"
        else:
            log_level = "debug"

        log_msg = (
            f"流 {context.stream_id} 分发周期: {final_interval:.2f}s | "
            f"focus_energy: {focus_energy:.3f} | "
            f"avg_interest: {avg_message_interest:.3f} | "
            f"阈值参考: {non_reply_threshold:.2f}/{reply_threshold:.2f}/{high_match_threshold:.2f}"
        )

        if log_level == "info":
            logger.info(log_msg)
        else:
            logger.debug(log_msg)

        return final_interval

    def _calculate_next_manager_delay(self) -> float:
        """计算管理器下次检查的延迟时间"""
        current_time = time.time()
        min_delay = float('inf')

        # 找到最近需要检查的流
        for context in self.stream_contexts.values():
            if not context.is_active:
                continue

            time_until_check = context.next_check_time - current_time
            if time_until_check > 0:
                min_delay = min(min_delay, time_until_check)
            else:
                min_delay = 0.1  # 立即检查
                break

        # 如果没有活跃流，使用默认间隔
        if min_delay == float('inf'):
            return self.check_interval

        # 确保最小延迟
        return max(0.1, min(min_delay, self.check_interval))

    async def _check_streams_with_individual_intervals(self):
        """检查所有达到检查时间的聊天流"""
        current_time = time.time()
        processed_streams = 0

        for stream_id, context in self.stream_contexts.items():
            if not context.is_active:
                continue

            # 检查是否达到检查时间
            if current_time >= context.next_check_time:
                # 更新检查时间
                context.last_check_time = current_time

                # 计算下次检查时间和分发周期
                if global_config.chat.dynamic_distribution_enabled:
                    context.distribution_interval = self._calculate_stream_distribution_interval(context)
                else:
                    context.distribution_interval = self.check_interval

                # 设置下次检查时间
                context.next_check_time = current_time + context.distribution_interval

                # 检查未读消息
                unread_messages = context.get_unread_messages()
                if unread_messages:
                    processed_streams += 1
                    self.stats.total_unread_messages = len(unread_messages)

                    # 如果没有处理任务，创建一个
                    if not context.processing_task or context.processing_task.done():
                        focus_energy = context.chat_stream.focus_energy if hasattr(context, 'chat_stream') and context.chat_stream else 0.5

                        # 根据优先级记录日志
                        if focus_energy >= 0.7:
                            logger.info(
                                f"高优先级流 {stream_id} 开始处理 | "
                                f"focus_energy: {focus_energy:.3f} | "
                                f"分发周期: {context.distribution_interval:.2f}s | "
                                f"未读消息: {len(unread_messages)}"
                            )
                        else:
                            logger.debug(
                                f"流 {stream_id} 开始处理 | "
                                f"focus_energy: {focus_energy:.3f} | "
                                f"分发周期: {context.distribution_interval:.2f}s"
                            )

                        context.processing_task = asyncio.create_task(self._process_stream_messages(stream_id))

        # 更新活跃流计数
        active_count = sum(1 for ctx in self.stream_contexts.values() if ctx.is_active)
        self.stats.active_streams = active_count

        if processed_streams > 0:
            logger.debug(
                f"本次循环处理了 {processed_streams} 个流 | "
                f"活跃流总数: {active_count}"
            )

    async def _check_all_streams_with_priority(self):
        """按优先级检查所有聊天流，高focus_energy的流优先处理"""
        if not self.stream_contexts:
            return

        # 获取活跃的聊天流并按focus_energy排序
        active_streams = []
        for stream_id, context in self.stream_contexts.items():
            if not context.is_active:
                continue

            # 获取focus_energy，如果不存在则使用默认值
            focus_energy = 0.5
            if hasattr(context, 'chat_stream') and context.chat_stream:
                focus_energy = context.chat_stream.focus_energy

            # 计算流优先级分数
            priority_score = self._calculate_stream_priority(context, focus_energy)
            active_streams.append((priority_score, stream_id, context))

        # 按优先级降序排序
        active_streams.sort(reverse=True, key=lambda x: x[0])

        # 处理排序后的流
        active_stream_count = 0
        total_unread = 0

        for priority_score, stream_id, context in active_streams:
            active_stream_count += 1

            # 检查是否有未读消息
            unread_messages = context.get_unread_messages()
            if unread_messages:
                total_unread += len(unread_messages)

                # 如果没有处理任务，创建一个
                if not context.processing_task or context.processing_task.done():
                    context.processing_task = asyncio.create_task(self._process_stream_messages(stream_id))

                    # 高优先级流的额外日志
                    if priority_score > 0.7:
                        logger.info(
                            f"高优先级流 {stream_id} 开始处理 | "
                            f"优先级: {priority_score:.3f} | "
                            f"未读消息: {len(unread_messages)}"
                        )

        # 更新统计
        self.stats.active_streams = active_stream_count
        self.stats.total_unread_messages = total_unread

    def _calculate_stream_priority(self, context: StreamContext, focus_energy: float) -> float:
        """计算聊天流的优先级分数"""
        # 基础优先级：focus_energy
        base_priority = focus_energy

        # 未读消息数量加权
        unread_count = len(context.get_unread_messages())
        message_count_bonus = min(unread_count * 0.1, 0.3)  # 最多30%加成

        # 时间加权：最近活跃的流优先级更高
        current_time = time.time()
        time_since_active = current_time - context.last_check_time
        time_penalty = max(0, 1.0 - time_since_active / 3600.0)  # 1小时内无惩罚

        # 连续无回复惩罚
        if hasattr(context, 'chat_stream') and context.chat_stream:
            consecutive_no_reply = context.chat_stream.consecutive_no_reply
            no_reply_penalty = max(0, 1.0 - consecutive_no_reply * 0.05)  # 每次无回复降低5%
        else:
            no_reply_penalty = 1.0

        # 综合优先级计算
        final_priority = (
            base_priority * 0.6 +           # 基础兴趣度权重60%
            message_count_bonus * 0.2 +     # 消息数量权重20%
            time_penalty * 0.1 +            # 时间权重10%
            no_reply_penalty * 0.1         # 回复状态权重10%
        )

        return max(0.0, min(1.0, final_priority))

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
                logger.debug(f"强制清除消息 {msg.message_id}，标记为已读")
            except Exception as e:
                logger.error(f"清除消息 {msg.message_id} 时出错: {e}")


# 创建全局消息管理器实例
message_manager = MessageManager()
