import asyncio
import time
import traceback
from typing import Optional

from src.common.logger import get_logger
from src.config.config import global_config
from src.person_info.relationship_builder_manager import relationship_builder_manager
from src.chat.express.expression_learner import expression_learner_manager
from src.plugin_system.base.component_types import ChatMode
from src.schedule.schedule_manager import schedule_manager
from src.plugin_system.apis import message_api

from .hfc_context import HfcContext
from .energy_manager import EnergyManager
from .proactive_thinker import ProactiveThinker
from .cycle_processor import CycleProcessor
from .response_handler import ResponseHandler
from .normal_mode_handler import NormalModeHandler
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
        self.normal_mode_handler = NormalModeHandler(self.context, self.cycle_processor)
        self.wakeup_manager = WakeUpManager(self.context)
        
        # 将唤醒度管理器设置到上下文中
        self.context.wakeup_manager = self.wakeup_manager
        self.context.energy_manager = self.energy_manager
        
        self._loop_task: Optional[asyncio.Task] = None
        
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
            if global_config.chat.group_chat_mode == "focus":
                self.context.loop_mode = ChatMode.FOCUS
                self.context.energy_value = 35
            elif global_config.chat.group_chat_mode == "normal":
                self.context.loop_mode = ChatMode.NORMAL
                self.context.energy_value = 15

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

        await self.energy_manager.start()
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
        
        await self.energy_manager.stop()
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
        is_sleeping = schedule_manager.is_sleeping(self.wakeup_manager)

        # --- 失眠状态管理 ---
        if self.context.is_in_insomnia and time.time() > self.context.insomnia_end_time:
            # 失眠状态结束
            self.context.is_in_insomnia = False
            await self.proactive_thinker.trigger_goodnight_thinking()

        if is_sleeping and not self.context.was_sleeping:
            # 刚刚进入睡眠状态，进行一次入睡检查
            if self.wakeup_manager and self.wakeup_manager.check_for_insomnia():
                # 触发失眠
                self.context.is_in_insomnia = True
                duration = global_config.wakeup_system.insomnia_duration_minutes * 60
                self.context.insomnia_end_time = time.time() + duration
                
                # 判断失眠原因并触发思考
                reason = "random"
                if self.context.sleep_pressure < global_config.wakeup_system.sleep_pressure_threshold:
                    reason = "low_pressure"
                await self.proactive_thinker.trigger_insomnia_thinking(reason)

        # 核心修复：在睡眠模式(包括失眠)下获取消息时，不过滤命令消息，以确保@消息能被接收
        filter_command_flag = not is_sleeping
        
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
        
        # 只有在有新消息时才进行思考循环处理
        if has_new_messages:
            self.context.last_message_time = time.time()
            self.context.last_read_time = time.time()
            
            # 处理唤醒度逻辑
            if is_sleeping:
                self._handle_wakeup_messages(recent_messages)
                # 再次检查睡眠状态，因为_handle_wakeup_messages可能会触发唤醒
                current_is_sleeping = schedule_manager.is_sleeping(self.wakeup_manager)
                
                if not self.context.is_in_insomnia and current_is_sleeping:
                    # 仍然在睡眠，跳过本轮的消息处理
                    return has_new_messages
                else:
                    # 从睡眠中被唤醒，需要继续处理本轮消息
                    logger.info(f"{self.context.log_prefix} 从睡眠中被唤醒，将处理积压的消息。")
                    self.context.last_wakeup_time = time.time()

            # 根据聊天模式处理新消息
            if self.context.loop_mode == ChatMode.FOCUS:
                for message in recent_messages:
                    await self.cycle_processor.observe(message)
                self._check_focus_exit()
            elif self.context.loop_mode == ChatMode.NORMAL:
                self._check_focus_entry(len(recent_messages))
                for message in recent_messages:
                    await self.normal_mode_handler.handle_message(message)
        else:
            # 无新消息时，只进行模式检查，不进行思考循环
            if self.context.loop_mode == ChatMode.FOCUS:
                self._check_focus_exit()
            elif self.context.loop_mode == ChatMode.NORMAL:
                self._check_focus_entry(0)  # 传入0表示无新消息

        # 更新上一帧的睡眠状态
        self.context.was_sleeping = is_sleeping
        
        # --- 重新入睡逻辑 ---
        # 如果被吵醒了，并且在一定时间内没有新消息，则尝试重新入睡
        if schedule_manager._is_woken_up and not has_new_messages:
            re_sleep_delay = global_config.wakeup_system.re_sleep_delay_minutes * 60
            # 使用 last_message_time 来判断空闲时间
            if time.time() - self.context.last_message_time > re_sleep_delay:
                logger.info(f"{self.context.log_prefix} 已被唤醒且超过 {re_sleep_delay / 60} 分钟无新消息，尝试重新入睡。")
                schedule_manager.reset_wakeup_state()
        
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
            self.context.loop_mode = ChatMode.NORMAL  # 切换到普通模式

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
            self.context.loop_mode = ChatMode.FOCUS
            self.context.energy_value = 10
            return

        if is_group_chat and global_config.chat.group_chat_mode == "normal":
            return
        
        if global_config.chat.focus_value != 0:  # 如果专注值配置不为0（启用自动专注）
            if new_message_count > 3 / pow(global_config.chat.focus_value, 0.5):  # 如果新消息数超过阈值（基于专注值计算）
                self.context.loop_mode = ChatMode.FOCUS  # 进入专注模式
                self.context.energy_value = 10 + (new_message_count / (3 / pow(global_config.chat.focus_value, 0.5))) * 10  # 根据消息数量计算能量值
                return  # 返回，不再检查其他条件

            if self.context.energy_value >= 30:  # 如果能量值达到或超过30
                self.context.loop_mode = ChatMode.FOCUS  # 进入专注模式
    
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
