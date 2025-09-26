import asyncio
import time
import traceback
import random
from typing import Optional, List, Dict, Any
from collections import deque

from src.common.logger import get_logger
from src.config.config import global_config
from src.person_info.relationship_builder_manager import relationship_builder_manager
from src.chat.express.expression_learner import expression_learner_manager
from src.chat.chat_loop.sleep_manager.sleep_manager import SleepManager, SleepState

from .hfc_context import HfcContext
from .energy_manager import EnergyManager
from .proactive.proactive_thinker import ProactiveThinker
from .cycle_processor import CycleProcessor
from .response_handler import ResponseHandler
from .cycle_tracker import CycleTracker
from .sleep_manager.wakeup_manager import WakeUpManager
from .proactive.events import ProactiveTriggerEvent

logger = get_logger("hfc")


class HeartFChatting:
    def __init__(self, chat_id: str):
        """
        初始化心跳聊天管理器

        Args:
            chat_id: 聊天ID标识符

        功能说明:
        - 创建聊天上下文和所有子管理器
        - 初始化循环跟踪器、响应处理器、循环处理器等核心组件
        - 设置能量管理器、主动思考器和普通模式处理器
        - 初始化聊天模式并记录初始化完成日志
        """
        self.context = HfcContext(chat_id)
        self.context.new_message_queue = asyncio.Queue()

        self.cycle_tracker = CycleTracker(self.context)
        self.response_handler = ResponseHandler(self.context)
        self.cycle_processor = CycleProcessor(self.context, self.response_handler, self.cycle_tracker)
        self.energy_manager = EnergyManager(self.context)
        self.proactive_thinker = ProactiveThinker(self.context, self.cycle_processor)
        self.wakeup_manager = WakeUpManager(self.context)
        self.sleep_manager = SleepManager()

        # 将唤醒度管理器设置到上下文中
        self.context.wakeup_manager = self.wakeup_manager
        self.context.energy_manager = self.energy_manager
        self.context.sleep_manager = self.sleep_manager
        # 将HeartFChatting实例设置到上下文中，以便其他组件可以调用其方法
        self.context.chat_instance = self

        self._loop_task: Optional[asyncio.Task] = None
        self._proactive_monitor_task: Optional[asyncio.Task] = None

        # 记录最近3次的兴趣度
        self.recent_interest_records: deque = deque(maxlen=3)
        self._initialize_chat_mode()
        logger.info(f"{self.context.log_prefix} HeartFChatting 初始化完成")

    def _initialize_chat_mode(self):
        """
        初始化聊天模式

        功能说明:
        - 检测是否为群聊环境
        - 根据全局配置设置强制聊天模式
        - 在focus模式下设置能量值为35
        - 在normal模式下设置能量值为15
        - 如果是auto模式则保持默认设置
        """
        is_group_chat = self.context.chat_stream.group_info is not None if self.context.chat_stream else False
        if is_group_chat and global_config.chat.group_chat_mode != "auto":
            self.context.energy_value = 25

    async def start(self):
        """
        启动心跳聊天系统

        功能说明:
        - 检查是否已经在运行，避免重复启动
        - 初始化关系构建器和表达学习器
        - 启动能量管理器和主动思考器
        - 创建主聊天循环任务并设置完成回调
        - 记录启动完成日志
        """
        if self.context.running:
            return
        self.context.running = True

        self.context.relationship_builder = relationship_builder_manager.get_or_create_builder(self.context.stream_id)
        self.context.expression_learner = await expression_learner_manager.get_expression_learner(self.context.stream_id)

        # 启动主动思考监视器
        if global_config.chat.enable_proactive_thinking:
            self._proactive_monitor_task = asyncio.create_task(self._proactive_monitor_loop())
            self._proactive_monitor_task.add_done_callback(self._handle_proactive_monitor_completion)
            logger.info(f"{self.context.log_prefix} 主动思考监视器已启动")

        await self.wakeup_manager.start()

        self._loop_task = asyncio.create_task(self._main_chat_loop())
        self._loop_task.add_done_callback(self._handle_loop_completion)
        logger.info(f"{self.context.log_prefix} HeartFChatting 启动完成")

    async def add_message(self, message: Dict[str, Any]):
        """从外部接收新消息并放入队列"""
        await self.context.new_message_queue.put(message)

    async def stop(self):
        """
        停止心跳聊天系统

        功能说明:
        - 检查是否正在运行，避免重复停止
        - 设置运行状态为False
        - 停止能量管理器和主动思考器
        - 取消主聊天循环任务
        - 记录停止完成日志
        """
        if not self.context.running:
            return
        self.context.running = False

        # 停止主动思考监视器
        if self._proactive_monitor_task and not self._proactive_monitor_task.done():
            self._proactive_monitor_task.cancel()
            await asyncio.sleep(0)
            logger.info(f"{self.context.log_prefix} 主动思考监视器已停止")

        await self.wakeup_manager.stop()

        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            await asyncio.sleep(0)
        logger.info(f"{self.context.log_prefix} HeartFChatting 已停止")

    def _handle_loop_completion(self, task: asyncio.Task):
        """
        处理主循环任务完成

        Args:
            task: 完成的异步任务对象

        功能说明:
        - 处理任务异常完成的情况
        - 区分正常停止和异常终止
        - 记录相应的日志信息
        - 处理取消任务的情况
        """
        try:
            if exception := task.exception():
                logger.error(f"{self.context.log_prefix} HeartFChatting: 脱离了聊天(异常): {exception}")
                logger.error(traceback.format_exc())
            else:
                logger.info(f"{self.context.log_prefix} HeartFChatting: 脱离了聊天 (外部停止)")
        except asyncio.CancelledError:
            logger.info(f"{self.context.log_prefix} HeartFChatting: 结束了聊天")

    def _handle_proactive_monitor_completion(self, task: asyncio.Task):
        """
        处理主动思考监视器任务完成

        Args:
            task: 完成的异步任务对象

        功能说明:
        - 处理任务异常完成的情况
        - 记录任务正常结束或被取消的日志
        """
        try:
            if exception := task.exception():
                logger.error(f"{self.context.log_prefix} 主动思考监视器异常: {exception}")
            else:
                logger.info(f"{self.context.log_prefix} 主动思考监视器正常结束")
        except asyncio.CancelledError:
            logger.info(f"{self.context.log_prefix} 主动思考监视器被取消")

    async def _proactive_monitor_loop(self):
        """
        主动思考监视器循环

        功能说明:
        - 定期检查是否需要进行主动思考
        - 计算聊天沉默时间，并与动态思考间隔比较
        - 当沉默时间超过阈值时，触发主动思考
        - 处理思考过程中的异常
        """
        while self.context.running:
            await asyncio.sleep(15)

            if not self._should_enable_proactive_thinking():
                continue

            current_time = time.time()
            silence_duration = current_time - self.context.last_message_time
            target_interval = self._get_dynamic_thinking_interval()

            if silence_duration >= target_interval:
                try:
                    formatted_time = self._format_duration(silence_duration)
                    event = ProactiveTriggerEvent(
                        source="silence_monitor",
                        reason=f"聊天已沉默 {formatted_time}",
                        metadata={"silence_duration": silence_duration},
                    )
                    await self.proactive_thinker.think(event)
                    self.context.last_message_time = current_time
                except Exception as e:
                    logger.error(f"{self.context.log_prefix} 主动思考触发执行出错: {e}")
                    logger.error(traceback.format_exc())

    def _should_enable_proactive_thinking(self) -> bool:
        """
        判断是否应启用主动思考

        Returns:
            bool: 如果应启用主动思考则返回True，否则返回False

        功能说明:
        - 检查全局配置和特定聊天设置
        - 支持按群聊和私聊分别配置
        - 支持白名单模式，只在特定聊天中启用
        """
        if not self.context.chat_stream:
            return False

        is_group_chat = self.context.chat_stream.group_info is not None

        if is_group_chat and not global_config.chat.proactive_thinking_in_group:
            return False
        if not is_group_chat and not global_config.chat.proactive_thinking_in_private:
            return False

        stream_parts = self.context.stream_id.split(":")
        current_chat_identifier = f"{stream_parts}:{stream_parts}" if len(stream_parts) >= 2 else self.context.stream_id

        enable_list = getattr(
            global_config.chat,
            "proactive_thinking_enable_in_groups" if is_group_chat else "proactive_thinking_enable_in_private",
            [],
        )
        return not enable_list or current_chat_identifier in enable_list

    def _get_dynamic_thinking_interval(self) -> float:
        """
        获取动态思考间隔时间

        Returns:
            float: 思考间隔秒数

        功能说明:
        - 尝试从timing_utils导入正态分布间隔函数
        - 根据配置计算动态间隔，增加随机性
        - 在无法导入或计算出错时，回退到固定的间隔
        """
        try:
            from src.utils.timing_utils import get_normal_distributed_interval

            base_interval = global_config.chat.proactive_thinking_interval
            delta_sigma = getattr(global_config.chat, "delta_sigma", 120)

            if base_interval <= 0:
                base_interval = abs(base_interval)
            if delta_sigma < 0:
                delta_sigma = abs(delta_sigma)

            if base_interval == 0 and delta_sigma == 0:
                return 300
            if delta_sigma == 0:
                return base_interval

            sigma_percentage = delta_sigma / base_interval if base_interval > 0 else delta_sigma / 1000
            return get_normal_distributed_interval(base_interval, sigma_percentage, 1, 86400, use_3sigma_rule=True)

        except ImportError:
            logger.warning(f"{self.context.log_prefix} timing_utils不可用，使用固定间隔")
            return max(300, abs(global_config.chat.proactive_thinking_interval))
        except Exception as e:
            logger.error(f"{self.context.log_prefix} 动态间隔计算出错: {e}，使用固定间隔")
            return max(300, abs(global_config.chat.proactive_thinking_interval))

    @staticmethod
    def _format_duration(seconds: float) -> str:
        """
        格式化时长为可读字符串

        Args:
            seconds: 时长秒数

        Returns:
            str: 格式化后的字符串 (例如 "1小时2分3秒")
        """
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        parts = []
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0:
            parts.append(f"{minutes}分")
        if secs > 0 or not parts:
            parts.append(f"{secs}秒")
        return "".join(parts)

    async def _main_chat_loop(self):
        """
        主聊天循环

        功能说明:
        - 持续运行聊天处理循环
        - 只有在有新消息时才进行思考循环
        - 无新消息时等待新消息到达（由主动思考系统单独处理主动发言）
        - 处理取消和异常情况
        - 在异常时尝试重新启动循环
        """
        try:
            while self.context.running:
                has_new_messages = await self._loop_body()

                if has_new_messages:
                    # 有新消息时，继续快速检查是否还有更多消息
                    await asyncio.sleep(1)
                else:
                    # 无新消息时，等待较长时间再检查
                    # 这里只是为了定期检查系统状态，不进行思考循环
                    # 真正的新消息响应依赖于消息到达时的通知
                    await asyncio.sleep(1.0)

        except asyncio.CancelledError:
            logger.info(f"{self.context.log_prefix} 麦麦已关闭聊天")
        except Exception:
            logger.error(f"{self.context.log_prefix} 麦麦聊天意外错误，将于3s后尝试重新启动")
            print(traceback.format_exc())
            await asyncio.sleep(3)
            self._loop_task = asyncio.create_task(self._main_chat_loop())
        logger.error(f"{self.context.log_prefix} 结束了当前聊天循环")

    async def _loop_body(self) -> bool:
        """
        单次循环体处理

        Returns:
            bool: 是否处理了新消息

        功能说明:
        - 检查是否处于睡眠模式，如果是则处理唤醒度逻辑
        - 获取最近的新消息（过滤机器人自己的消息和命令）
        - 只有在有新消息时才进行思考循环处理
        - 更新最后消息时间和读取时间
        - 根据当前聊天模式执行不同的处理逻辑
        - FOCUS模式：直接处理所有消息并检查退出条件
        - NORMAL模式：检查进入FOCUS模式的条件，并通过normal_mode_handler处理消息
        """
        # --- 核心状态更新 ---
        await self.sleep_manager.update_sleep_state(self.wakeup_manager)
        current_sleep_state = self.sleep_manager.get_current_sleep_state()
        is_sleeping = current_sleep_state == SleepState.SLEEPING
        is_in_insomnia = current_sleep_state == SleepState.INSOMNIA

        # 核心修复：在睡眠模式(包括失眠)下获取消息时，不过滤命令消息，以确保@消息能被接收
        filter_command_flag = not (is_sleeping or is_in_insomnia)

        # 从队列中获取所有待处理的新消息
        recent_messages = []
        while not self.context.new_message_queue.empty():
            recent_messages.append(await self.context.new_message_queue.get())

        has_new_messages = bool(recent_messages)
        new_message_count = len(recent_messages)

        # 只有在有新消息时才进行思考循环处理
        if has_new_messages:
            self.context.last_message_time = time.time()
            self.context.last_read_time = time.time()

            # --- 专注模式安静群组检查 ---
            quiet_groups = global_config.chat.focus_mode_quiet_groups
            if quiet_groups and self.context.chat_stream:
                is_group_chat = self.context.chat_stream.group_info is not None
                if is_group_chat:
                    try:
                        platform = self.context.chat_stream.platform
                        group_id = self.context.chat_stream.group_info.group_id
                        
                        # 兼容不同QQ适配器的平台名称
                        is_qq_platform = platform in ["qq", "napcat"]
                        
                        current_chat_identifier = f"{platform}:{group_id}"
                        config_identifier_for_qq = f"qq:{group_id}"
                        
                        is_in_quiet_list = (current_chat_identifier in quiet_groups or
                                            (is_qq_platform and config_identifier_for_qq in quiet_groups))

                        if is_in_quiet_list:
                            is_mentioned_in_batch = False
                            for msg in recent_messages:
                                if msg.get("is_mentioned"):
                                    is_mentioned_in_batch = True
                                    break
                            
                            if not is_mentioned_in_batch:
                                logger.info(f"{self.context.log_prefix} 在专注安静模式下，因未被提及而忽略了消息。")
                                return True  # 消耗消息但不做回复
                    except Exception as e:
                        logger.error(f"{self.context.log_prefix} 检查专注安静群组时出错: {e}")

            # 处理唤醒度逻辑
            if current_sleep_state in [SleepState.SLEEPING, SleepState.PREPARING_SLEEP, SleepState.INSOMNIA]:
                self._handle_wakeup_messages(recent_messages)

                # 再次获取最新状态，因为 handle_wakeup 可能导致状态变为 WOKEN_UP
                current_sleep_state = self.sleep_manager.get_current_sleep_state()

                if current_sleep_state == SleepState.SLEEPING:
                    # 只有在纯粹的 SLEEPING 状态下才跳过消息处理
                    return True

                if current_sleep_state == SleepState.WOKEN_UP:
                    logger.info(f"{self.context.log_prefix} 从睡眠中被唤醒，将处理积压的消息。")

            # 根据聊天模式处理新消息
            should_process, interest_value = await self._should_process_messages(recent_messages)
            if not should_process:
                # 消息数量不足或兴趣不够，等待
                await asyncio.sleep(0.5)
                return True  # Skip rest of the logic for this iteration

            # Messages should be processed
            action_type = await self.cycle_processor.observe(interest_value=interest_value)

            # 尝试触发表达学习
            if self.context.expression_learner:
                try:
                    await self.context.expression_learner.trigger_learning_for_chat()
                except Exception as e:
                    logger.error(f"{self.context.log_prefix} 表达学习触发失败: {e}")

            # 管理no_reply计数器
            if action_type != "no_reply":
                self.recent_interest_records.clear()
                self.context.no_reply_consecutive = 0
                logger.debug(f"{self.context.log_prefix} 执行了{action_type}动作，重置no_reply计数器")
            else:  # action_type == "no_reply"
                self.context.no_reply_consecutive += 1
                self._determine_form_type()

            # 在一轮动作执行完毕后，增加睡眠压力
            if self.context.energy_manager and global_config.sleep_system.enable_insomnia_system:
                if action_type not in ["no_reply", "no_action"]:
                    self.context.energy_manager.increase_sleep_pressure()

            # 如果成功观察，增加能量值并重置累积兴趣值
            self.context.energy_value += 1 / global_config.chat.focus_value
            # 重置累积兴趣值，因为消息已经被成功处理
            self.context.breaking_accumulated_interest = 0.0
            logger.info(
                f"{self.context.log_prefix} 能量值增加，当前能量值：{self.context.energy_value:.1f}，重置累积兴趣值"
            )

        # 更新上一帧的睡眠状态
        self.context.was_sleeping = is_sleeping

        # --- 重新入睡逻辑 ---
        # 如果被吵醒了，并且在一定时间内没有新消息，则尝试重新入睡
        if self.sleep_manager.get_current_sleep_state() == SleepState.WOKEN_UP and not has_new_messages:
            re_sleep_delay = global_config.sleep_system.re_sleep_delay_minutes * 60
            # 使用 last_message_time 来判断空闲时间
            if time.time() - self.context.last_message_time > re_sleep_delay:
                logger.info(
                    f"{self.context.log_prefix} 已被唤醒且超过 {re_sleep_delay / 60} 分钟无新消息，尝试重新入睡。"
                )
                self.sleep_manager.reset_sleep_state_after_wakeup()

        # 保存HFC上下文状态
        self.context.save_context_state()

        return has_new_messages

    def _handle_wakeup_messages(self, messages):
        """
        处理休眠状态下的消息，累积唤醒度

        Args:
            messages: 消息列表

        功能说明:
        - 区分私聊和群聊消息
        - 检查群聊消息是否艾特了机器人
        - 调用唤醒度管理器累积唤醒度
        - 如果达到阈值则唤醒并进入愤怒状态
        """
        if not self.wakeup_manager:
            return

        is_private_chat = self.context.chat_stream.group_info is None if self.context.chat_stream else False

        for message in messages:
            is_mentioned = False

            # 检查群聊消息是否艾特了机器人
            if not is_private_chat:
                # 最终修复：直接使用消息对象中由上游处理好的 is_mention 字段。
                # 该字段在 message.py 的 MessageRecv._process_single_segment 中被设置。
                if message.get("is_mentioned"):
                    is_mentioned = True

            # 累积唤醒度
            woke_up = self.wakeup_manager.add_wakeup_value(is_private_chat, is_mentioned)

            if woke_up:
                logger.info(f"{self.context.log_prefix} 被消息吵醒，进入愤怒状态！")
                break

    def _determine_form_type(self) -> str:
        """判断使用哪种形式的no_reply"""
        # 检查是否启用breaking模式
        if not getattr(global_config.chat, "enable_breaking_mode", False):
            logger.info(f"{self.context.log_prefix} breaking模式已禁用，使用waiting形式")
            self.context.focus_energy = 1
            return "waiting"

        # 如果连续no_reply次数少于3次，使用waiting形式
        if self.context.no_reply_consecutive <= 3:
            self.context.focus_energy = 1
            return "waiting"
        else:
            # 使用累积兴趣值而不是最近3次的记录
            total_interest = self.context.breaking_accumulated_interest

            # 计算调整后的阈值
            adjusted_threshold = 1 / global_config.chat.get_current_talk_frequency(self.context.stream_id)

            logger.info(
                f"{self.context.log_prefix} 累积兴趣值: {total_interest:.2f}, 调整后阈值: {adjusted_threshold:.2f}"
            )

            # 如果累积兴趣值小于阈值，进入breaking形式
            if total_interest < adjusted_threshold:
                logger.info(f"{self.context.log_prefix} 累积兴趣度不足，进入breaking形式")
                self.context.focus_energy = random.randint(3, 6)
                return "breaking"
            else:
                logger.info(f"{self.context.log_prefix} 累积兴趣度充足，使用waiting形式")
                self.context.focus_energy = 1
                return "waiting"

    async def _should_process_messages(self, new_message: List[Dict[str, Any]]) -> tuple[bool, float]:
        """
        统一判断是否应该处理消息的函数
        根据当前循环模式和消息内容决定是否继续处理
        """
        if not new_message:
            return False, 0.0

        # 计算平均兴趣值
        total_interest = 0.0
        message_count = 0
        for msg_dict in new_message:
            interest_value = msg_dict.get("interest_value", 0.0)
            if msg_dict.get("processed_plain_text", ""):
                total_interest += interest_value
                message_count += 1
        
        avg_interest = total_interest / message_count if message_count > 0 else 0.0

        logger.info(f"{self.context.log_prefix} 收到 {len(new_message)} 条新消息，立即处理！平均兴趣值: {avg_interest:.2f}")
        return True, avg_interest
