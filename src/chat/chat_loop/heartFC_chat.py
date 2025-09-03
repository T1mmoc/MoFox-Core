import asyncio
import time
import traceback
import random
from typing import Optional, List, Dict, Any, Tuple
from collections import deque

from src.common.logger import get_logger
from src.config.config import global_config
from src.person_info.relationship_builder_manager import relationship_builder_manager
from src.chat.express.expression_learner import expression_learner_manager
from src.plugin_system.base.component_types import ChatMode
from src.schedule.schedule_manager import schedule_manager, SleepState
from src.plugin_system.apis import message_api

from .hfc_context import HfcContext
from .energy_manager import EnergyManager
from .proactive_thinker import ProactiveThinker
from .cycle_processor import CycleProcessor
from .response_handler import ResponseHandler
from .cycle_tracker import CycleTracker
from .wakeup_manager import WakeUpManager

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

        self.cycle_tracker = CycleTracker(self.context)
        self.response_handler = ResponseHandler(self.context)
        self.cycle_processor = CycleProcessor(self.context, self.response_handler, self.cycle_tracker)
        self.energy_manager = EnergyManager(self.context)
        self.proactive_thinker = ProactiveThinker(self.context, self.cycle_processor)
        self.wakeup_manager = WakeUpManager(self.context)

        # 将唤醒度管理器设置到上下文中
        self.context.wakeup_manager = self.wakeup_manager
        self.context.energy_manager = self.energy_manager
        # 将HeartFChatting实例设置到上下文中，以便其他组件可以调用其方法
        self.context.chat_instance = self

        self._loop_task: Optional[asyncio.Task] = None
        
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
        self.context.expression_learner = expression_learner_manager.get_expression_learner(self.context.stream_id)

        #await self.energy_manager.start()
        await self.proactive_thinker.start()
        await self.wakeup_manager.start()

        self._loop_task = asyncio.create_task(self._main_chat_loop())
        self._loop_task.add_done_callback(self._handle_loop_completion)
        logger.info(f"{self.context.log_prefix} HeartFChatting 启动完成")

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

        #await self.energy_manager.stop()
        await self.proactive_thinker.stop()
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
        await schedule_manager.update_sleep_state(self.wakeup_manager)
        current_sleep_state = schedule_manager.get_current_sleep_state()
        is_sleeping = current_sleep_state == SleepState.SLEEPING
        is_in_insomnia = current_sleep_state == SleepState.INSOMNIA

        # 核心修复：在睡眠模式(包括失眠)下获取消息时，不过滤命令消息，以确保@消息能被接收
        filter_command_flag = not (is_sleeping or is_in_insomnia)

        recent_messages = message_api.get_messages_by_time_in_chat(
            chat_id=self.context.stream_id,
            start_time=self.context.last_read_time,
            end_time=time.time(),
            limit=10,
            limit_mode="latest",
            filter_mai=True,
            filter_command=filter_command_flag,
        )

        has_new_messages = bool(recent_messages)
        new_message_count = len(recent_messages)

        # 只有在有新消息时才进行思考循环处理
        if has_new_messages:
            self.context.last_message_time = time.time()
            self.context.last_read_time = time.time()

            # 处理唤醒度逻辑
            if current_sleep_state in [SleepState.SLEEPING, SleepState.PREPARING_SLEEP, SleepState.INSOMNIA]:
                self._handle_wakeup_messages(recent_messages)

                # 再次获取最新状态，因为 handle_wakeup 可能导致状态变为 WOKEN_UP
                current_sleep_state = schedule_manager.get_current_sleep_state()

                if current_sleep_state == SleepState.SLEEPING:
                    # 只有在纯粹的 SLEEPING 状态下才跳过消息处理
                    return True

                if current_sleep_state == SleepState.WOKEN_UP:
                    logger.info(f"{self.context.log_prefix} 从睡眠中被唤醒，将处理积压的消息。")

            # 根据聊天模式处理新消息
            # 统一使用 _should_process_messages 判断是否应该处理
            should_process,interest_value = await self._should_process_messages(recent_messages if has_new_messages else None)
            if should_process:
                self.context.last_read_time = time.time()
                await self.cycle_processor.observe(interest_value = interest_value)
            else:
                # Normal模式：消息数量不足，等待
                await asyncio.sleep(0.5)
                return True
        
            if not await self._should_process_messages(recent_messages if has_new_messages else None):
                return has_new_messages
                
            # 处理新消息
            for message in recent_messages:
                await self.cycle_processor.observe(interest_value = interest_value)
                    
            # 如果成功观察，增加能量值并重置累积兴趣值
            if has_new_messages:
                self.context.energy_value += 1 / global_config.chat.focus_value
                # 重置累积兴趣值，因为消息已经被成功处理
                self.context.breaking_accumulated_interest = 0.0
                logger.info(f"{self.context.log_prefix} 能量值增加，当前能量值：{self.context.energy_value:.1f}，重置累积兴趣值")
                    
            self._check_focus_exit()

        else:
            # 无新消息时，只进行模式检查，不进行思考循环
            self._check_focus_exit()


        # 更新上一帧的睡眠状态
        self.context.was_sleeping = is_sleeping

        # --- 重新入睡逻辑 ---
        # 如果被吵醒了，并且在一定时间内没有新消息，则尝试重新入睡
        if schedule_manager.get_current_sleep_state() == SleepState.WOKEN_UP and not has_new_messages:
            re_sleep_delay = global_config.sleep_system.re_sleep_delay_minutes * 60
            # 使用 last_message_time 来判断空闲时间
            if time.time() - self.context.last_message_time > re_sleep_delay:
                logger.info(
                    f"{self.context.log_prefix} 已被唤醒且超过 {re_sleep_delay / 60} 分钟无新消息，尝试重新入睡。"
                )
                schedule_manager.reset_sleep_state_after_wakeup()

        # 保存HFC上下文状态
        self.context.save_context_state()

        return has_new_messages

    def _check_focus_exit(self):
        """
        检查是否应该退出FOCUS模式

        功能说明:
        - 区分私聊和群聊环境
        - 在强制私聊focus模式下，能量值低于1时重置为5但不退出
        - 在群聊focus模式下，如果配置为focus则不退出
        - 其他情况下，能量值低于1时退出到NORMAL模式
        """
        is_private_chat = self.context.chat_stream.group_info is None if self.context.chat_stream else False
        is_group_chat = not is_private_chat

        if global_config.chat.force_focus_private and is_private_chat:
            if self.context.energy_value <= 1:
                self.context.energy_value = 5
            return

        if is_group_chat and global_config.chat.group_chat_mode == "focus":
            return

        if self.context.energy_value <= 1:  # 如果能量值小于等于1（非强制情况）
            self.context.energy_value = 1  # 将能量值设置为1

    def _check_focus_entry(self, new_message_count: int):
        """
        检查是否应该进入FOCUS模式

        Args:
            new_message_count: 新消息数量

        功能说明:
        - 区分私聊和群聊环境
        - 强制私聊focus模式：直接进入FOCUS模式并设置能量值为10
        - 群聊normal模式：不进入FOCUS模式
        - 根据focus_value配置和消息数量决定是否进入FOCUS模式
        - 当消息数量超过阈值或能量值达到30时进入FOCUS模式
        """
        is_private_chat = self.context.chat_stream.group_info is None if self.context.chat_stream else False
        is_group_chat = not is_private_chat

        if global_config.chat.force_focus_private and is_private_chat:
            self.context.energy_value = 10
            return

        if is_group_chat and global_config.chat.group_chat_mode == "normal":
            return

        if global_config.chat.focus_value != 0:  # 如果专注值配置不为0（启用自动专注）
            if new_message_count > 3 / pow(
                global_config.chat.focus_value, 0.5
            ):  # 如果新消息数超过阈值（基于专注值计算）
                self.context.energy_value = (
                    10 + (new_message_count / (3 / pow(global_config.chat.focus_value, 0.5))) * 10
                )  # 根据消息数量计算能量值
                return  # 返回，不再检查其他条件

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
        # 如果连续no_reply次数少于3次，使用waiting形式
        if self.context.no_reply_consecutive <= 3:
            self.context.focus_energy = 1
        else:
            # 使用累积兴趣值而不是最近3次的记录
            total_interest = self.context.breaking_accumulated_interest
          
            # 计算调整后的阈值
            adjusted_threshold = 1 / global_config.chat.get_current_talk_frequency(self.context.stream_id)
            
            logger.info(f"{self.context.log_prefix} 累积兴趣值: {total_interest:.2f}, 调整后阈值: {adjusted_threshold:.2f}")
        
            # 如果累积兴趣值小于阈值，进入breaking形式
            if total_interest < adjusted_threshold:
                logger.info(f"{self.context.log_prefix} 累积兴趣度不足，进入breaking形式")
                self.context.focus_energy = random.randint(3, 6)
            else:
                logger.info(f"{self.context.log_prefix} 累积兴趣度充足，使用waiting形式")
                self.context.focus_energy = 1

    async def _should_process_messages(self, new_message: List[Dict[str, Any]]) -> tuple[bool,float]:
        """
        统一判断是否应该处理消息的函数
        根据当前循环模式和消息内容决定是否继续处理
        """
        new_message_count = len(new_message)

        talk_frequency = global_config.chat.get_current_talk_frequency(self.context.chat_stream.stream_id)

        modified_exit_count_threshold = self.context.focus_energy * 0.5 / talk_frequency
        modified_exit_interest_threshold = 1.5 / talk_frequency
        
        # 计算当前批次消息的兴趣值
        batch_interest = 0.0
        for msg_dict in new_message:
            interest_value = msg_dict.get("interest_value", 0.0)
            if msg_dict.get("processed_plain_text", ""):
                batch_interest += interest_value
        
        # 在breaking形式下累积所有消息的兴趣值
        if new_message_count > 0:
            self.context.breaking_accumulated_interest += batch_interest
            total_interest = self.context.breaking_accumulated_interest
        else:
            total_interest = self.context.breaking_accumulated_interest
        
        if new_message_count >= modified_exit_count_threshold:
            # 记录兴趣度到列表
            self.recent_interest_records.append(total_interest)
            # 重置累积兴趣值，因为已经达到了消息数量阈值
            self.context.breaking_accumulated_interest = 0.0
            
            logger.info(
                f"{self.context.log_prefix} 累计消息数量达到{new_message_count}条(>{modified_exit_count_threshold:.1f})，结束等待，累积兴趣值: {total_interest:.2f}"
            )
            return True,total_interest/new_message_count

        # 检查累计兴趣值
        if new_message_count > 0:
            # 只在兴趣值变化时输出log
            if not hasattr(self, "_last_accumulated_interest") or total_interest != self._last_accumulated_interest:
                logger.info(f"{self.context.log_prefix} breaking形式当前累积兴趣值: {total_interest:.2f}, 专注度: {global_config.chat.focus_value:.1f}")
                self._last_accumulated_interest = total_interest
            if total_interest >= modified_exit_interest_threshold:
                # 记录兴趣度到列表
                self.recent_interest_records.append(total_interest)
                # 重置累积兴趣值，因为已经达到了兴趣值阈值
                self.context.breaking_accumulated_interest = 0.0
                logger.info(
                    f"{self.context.log_prefix} 累计兴趣值达到{total_interest:.2f}(>{modified_exit_interest_threshold:.1f})，结束等待"
                )
                return True,total_interest/new_message_count
            
        # 每10秒输出一次等待状态
        if int(time.time() - self.context.last_read_time) > 0 and int(time.time() - self.context.last_read_time) % 10 == 0:
            logger.info(
                f"{self.context.log_prefix} 已等待{time.time() - self.last_read_time:.0f}秒，累计{new_message_count}条消息，累积兴趣{total_interest:.1f}，继续等待..."
            )
            await asyncio.sleep(0.5)
        
        return False,0.0
    
    async def _execute_no_reply(self, new_message: List[Dict[str, Any]]) -> bool:
        """执行breaking形式的no_reply（原有逻辑）"""
        new_message_count = len(new_message)
        # 检查消息数量是否达到阈值
        talk_frequency = global_config.chat.get_current_talk_frequency(self.context.stream_id)
        modified_exit_count_threshold = self.context.focus_energy / talk_frequency
        
        if new_message_count >= modified_exit_count_threshold:
            # 记录兴趣度到列表
            total_interest = 0.0
            for msg_dict in new_message:
                interest_value = msg_dict.get("interest_value", 0.0)
                if msg_dict.get("processed_plain_text", ""):
                    total_interest += interest_value
            
            self.recent_interest_records.append(total_interest)
            
            logger.info(
                f"{self.context.log_prefix} 累计消息数量达到{new_message_count}条(>{modified_exit_count_threshold})，结束等待"
            )

            return True

        # 检查累计兴趣值
        if new_message_count > 0:
            accumulated_interest = 0.0
            for msg_dict in new_message:
                text = msg_dict.get("processed_plain_text", "")
                interest_value = msg_dict.get("interest_value", 0.0)
                if text:
                    accumulated_interest += interest_value
            
            # 只在兴趣值变化时输出log
            if not hasattr(self, "_last_accumulated_interest") or accumulated_interest != self._last_accumulated_interest:
                logger.info(f"{self.context.log_prefix} breaking形式当前累计兴趣值: {accumulated_interest:.2f}, 当前聊天频率: {talk_frequency:.2f}")
                self._last_accumulated_interest = accumulated_interest
            
            if accumulated_interest >= 3 / talk_frequency:
                # 记录兴趣度到列表
                self.recent_interest_records.append(accumulated_interest)
                
                logger.info(
                    f"{self.context.log_prefix} 累计兴趣值达到{accumulated_interest:.2f}(>{3 / talk_frequency})，结束等待"
                )
                return True

        # 每10秒输出一次等待状态
        if int(time.time() - self.context.last_read_time) > 0 and int(time.time() - self.context.last_read_time) % 10 == 0:
            logger.info(
                f"{self.context.log_prefix} 已等待{time.time() - self.context.last_read_time:.0f}秒，累计{new_message_count}条消息，继续等待..."
            )
        
        return False
