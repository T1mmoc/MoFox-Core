import asyncio
import time
from typing import Optional
from src.common.logger import get_logger
from src.config.config import global_config
from src.plugin_system.base.component_types import ChatMode
from .hfc_context import HfcContext
from src.schedule.schedule_manager import schedule_manager

logger = get_logger("hfc")

class EnergyManager:
    def __init__(self, context: HfcContext):
        """
        初始化能量管理器
        
        Args:
            context: HFC聊天上下文对象
            
        功能说明:
        - 管理聊天机器人的能量值系统
        - 根据聊天模式自动调整能量消耗
        - 控制能量值的衰减和记录
        """
        self.context = context
        self._energy_task: Optional[asyncio.Task] = None
        self.last_energy_log_time = 0
        self.energy_log_interval = 90

    async def start(self):
        """
        启动能量管理器
        
        功能说明:
        - 检查运行状态，避免重复启动
        - 创建能量循环异步任务
        - 设置任务完成回调
        - 记录启动日志
        """
        if self.context.running and not self._energy_task:
            self._energy_task = asyncio.create_task(self._energy_loop())
            self._energy_task.add_done_callback(self._handle_energy_completion)
            logger.info(f"{self.context.log_prefix} 能量管理器已启动")

    async def stop(self):
        """
        停止能量管理器
        
        功能说明:
        - 取消正在运行的能量循环任务
        - 等待任务完全停止
        - 记录停止日志
        """
        if self._energy_task and not self._energy_task.done():
            self._energy_task.cancel()
            await asyncio.sleep(0)
            logger.info(f"{self.context.log_prefix} 能量管理器已停止")

    def _handle_energy_completion(self, task: asyncio.Task):
        """
        处理能量循环任务完成
        
        Args:
            task: 完成的异步任务对象
            
        功能说明:
        - 处理任务正常完成或异常情况
        - 记录相应的日志信息
        - 区分取消和异常终止的情况
        """
        try:
            if exception := task.exception():
                logger.error(f"{self.context.log_prefix} 能量循环异常: {exception}")
            else:
                logger.info(f"{self.context.log_prefix} 能量循环正常结束")
        except asyncio.CancelledError:
            logger.info(f"{self.context.log_prefix} 能量循环被取消")

    async def _energy_loop(self):
        """
        能量与睡眠压力管理的主循环
        
        功能说明:
        - 每10秒执行一次能量更新
        - 根据群聊配置设置固定的聊天模式和能量值
        - 在自动模式下根据聊天模式进行能量衰减
        - NORMAL模式每次衰减0.3，FOCUS模式每次衰减0.6
        - 确保能量值不低于0.3的最小值
        """
        while self.context.running:
            await asyncio.sleep(10)

            if not self.context.chat_stream:
                continue

            # 判断当前是否为睡眠时间
            is_sleeping = schedule_manager.is_sleeping(self.context.wakeup_manager)

            if is_sleeping:
                # 睡眠中：减少睡眠压力
                decay_per_10s = global_config.sleep_system.sleep_pressure_decay_rate / 6
                self.context.sleep_pressure -= decay_per_10s
                self.context.sleep_pressure = max(self.context.sleep_pressure, 0)
                self._log_sleep_pressure_change("睡眠压力释放")
            else:
                # 清醒时：处理能量衰减
                is_group_chat = self.context.chat_stream.group_info is not None
                if is_group_chat and global_config.chat.group_chat_mode != "auto":
                    if global_config.chat.group_chat_mode == "focus":
                        self.context.loop_mode = ChatMode.FOCUS
                        self.context.energy_value = 35
                    elif global_config.chat.group_chat_mode == "normal":
                        self.context.loop_mode = ChatMode.NORMAL
                        self.context.energy_value = 15
                    continue

                if self.context.loop_mode == ChatMode.NORMAL:
                    self.context.energy_value -= 0.3
                    self.context.energy_value = max(self.context.energy_value, 0.3)
                if self.context.loop_mode == ChatMode.FOCUS:
                    self.context.energy_value -= 0.6
                    self.context.energy_value = max(self.context.energy_value, 0.3)
                
                self._log_energy_change("能量值衰减")

    def _should_log_energy(self) -> bool:
        """
        判断是否应该记录能量变化日志
        
        Returns:
            bool: 如果距离上次记录超过间隔时间则返回True
            
        功能说明:
        - 控制能量日志的记录频率，避免日志过于频繁
        - 默认间隔90秒记录一次详细日志
        - 其他时间使用调试级别日志
        """
        current_time = time.time()
        if current_time - self.last_energy_log_time >= self.energy_log_interval:
            self.last_energy_log_time = current_time
            return True
        return False

    def increase_sleep_pressure(self):
        """
        在执行动作后增加睡眠压力
        """
        increment = global_config.sleep_system.sleep_pressure_increment
        self.context.sleep_pressure += increment
        self.context.sleep_pressure = min(self.context.sleep_pressure, 100.0) # 设置一个100的上限
        self._log_sleep_pressure_change("执行动作，睡眠压力累积")

    def _log_energy_change(self, action: str, reason: str = ""):
        """
        记录能量变化日志
        
        Args:
            action: 能量变化的动作描述
            reason: 可选的变化原因
            
        功能说明:
        - 根据时间间隔决定使用info还是debug级别的日志
        - 格式化能量值显示（保留一位小数）
        - 可选择性地包含变化原因
        """
        if self._should_log_energy():
            log_message = f"{self.context.log_prefix} {action}，当前能量值：{self.context.energy_value:.1f}"
            if reason:
                log_message = f"{self.context.log_prefix} {action}，{reason}，当前能量值：{self.context.energy_value:.1f}"
            logger.info(log_message)
        else:
            log_message = f"{self.context.log_prefix} {action}，当前能量值：{self.context.energy_value:.1f}"
            if reason:
                log_message = f"{self.context.log_prefix} {action}，{reason}，当前能量值：{self.context.energy_value:.1f}"
            logger.debug(log_message)

    def _log_sleep_pressure_change(self, action: str):
        """
        记录睡眠压力变化日志
        """
        # 使用与能量日志相同的频率控制
        if self._should_log_energy():
            logger.info(f"{self.context.log_prefix} {action}，当前睡眠压力：{self.context.sleep_pressure:.1f}")
        else:
            logger.debug(f"{self.context.log_prefix} {action}，当前睡眠压力：{self.context.sleep_pressure:.1f}")