import asyncio
import time
from typing import Optional, TYPE_CHECKING
from src.common.logger import get_logger
from src.config.config import global_config
from src.manager.local_store_manager import local_storage

if TYPE_CHECKING:
    from .sleep_manager import SleepManager


logger = get_logger("wakeup")


class WakeUpManager:
    def __init__(self, sleep_manager: "SleepManager"):
        """
        初始化唤醒度管理器

        Args:
            sleep_manager: 睡眠管理器实例

        功能说明:
        - 管理休眠状态下的唤醒度累积
        - 处理唤醒度的自然衰减
        - 控制愤怒状态的持续时间
        """
        self.sleep_manager = sleep_manager
        self.wakeup_value = 0.0  # 当前唤醒度
        self.is_angry = False  # 是否处于愤怒状态
        self.angry_start_time = 0.0  # 愤怒状态开始时间
        self.last_decay_time = time.time()  # 上次衰减时间
        self._decay_task: Optional[asyncio.Task] = None
        self.is_running = False
        self.last_log_time = 0
        self.log_interval = 30

        # 从配置文件获取参数
        sleep_config = global_config.sleep_system
        self.wakeup_threshold = sleep_config.wakeup_threshold
        self.private_message_increment = sleep_config.private_message_increment
        self.group_mention_increment = sleep_config.group_mention_increment
        self.decay_rate = sleep_config.decay_rate
        self.decay_interval = sleep_config.decay_interval
        self.angry_duration = sleep_config.angry_duration
        self.enabled = sleep_config.enable
        self.angry_prompt = sleep_config.angry_prompt

        self._load_wakeup_state()

    def _get_storage_key(self) -> str:
        """获取本地存储键"""
        return "global_wakeup_manager_state"

    def _load_wakeup_state(self):
        """从本地存储加载状态"""
        state = local_storage[self._get_storage_key()]
        if state and isinstance(state, dict):
            self.wakeup_value = state.get("wakeup_value", 0.0)
            self.is_angry = state.get("is_angry", False)
            self.angry_start_time = state.get("angry_start_time", 0.0)
            logger.info(f"成功从本地存储加载唤醒状态: {state}")
        else:
            logger.info("未找到本地唤醒状态，将使用默认值初始化。")

    def _save_wakeup_state(self):
        """将当前状态保存到本地存储"""
        state = {
            "wakeup_value": self.wakeup_value,
            "is_angry": self.is_angry,
            "angry_start_time": self.angry_start_time,
        }
        local_storage[self._get_storage_key()] = state
        logger.debug(f"已将唤醒状态保存到本地存储: {state}")

    async def start(self):
        """启动唤醒度管理器"""
        if not self.enabled:
            logger.info("唤醒度系统已禁用，跳过启动")
            return
        
        self.is_running = True
        if not self._decay_task or self._decay_task.done():
            self._decay_task = asyncio.create_task(self._decay_loop())
            self._decay_task.add_done_callback(self._handle_decay_completion)
            logger.info("唤醒度管理器已启动")

    async def stop(self):
        """停止唤醒度管理器"""
        self.is_running = False
        if self._decay_task and not self._decay_task.done():
            self._decay_task.cancel()
            await asyncio.sleep(0)
            logger.info("唤醒度管理器已停止")

    def _handle_decay_completion(self, task: asyncio.Task):
        """处理衰减任务完成"""
        try:
            if exception := task.exception():
                logger.error(f"唤醒度衰减任务异常: {exception}")
            else:
                logger.info("唤醒度衰减任务正常结束")
        except asyncio.CancelledError:
            logger.info("唤醒度衰减任务被取消")

    async def _decay_loop(self):
        """唤醒度衰减循环"""
        while self.is_running:
            await asyncio.sleep(self.decay_interval)

            current_time = time.time()

            # 检查愤怒状态是否过期
            if self.is_angry and current_time - self.angry_start_time >= self.angry_duration:
                self.is_angry = False
                # 通知情绪管理系统清除愤怒状态
                from src.mood.mood_manager import mood_manager
                mood_manager.clear_angry_from_wakeup("global_mood")
                logger.info("愤怒状态结束，恢复正常")
                self._save_wakeup_state()

            # 唤醒度自然衰减
            if self.wakeup_value > 0:
                old_value = self.wakeup_value
                self.wakeup_value = max(0, self.wakeup_value - self.decay_rate)
                if old_value != self.wakeup_value:
                    logger.debug(f"唤醒度衰减: {old_value:.1f} -> {self.wakeup_value:.1f}")
                    self._save_wakeup_state()

    def add_wakeup_value(self, is_private_chat: bool, is_mentioned: bool = False) -> bool:
        """
        增加唤醒度值

        Args:
            is_private_chat: 是否为私聊
            is_mentioned: 是否被艾特(仅群聊有效)

        Returns:
            bool: 是否达到唤醒阈值
        """
        # 如果系统未启用，直接返回
        if not self.enabled:
            return False

        # 只有在休眠且非失眠状态下才累积唤醒度
        from .sleep_state import SleepState

        current_sleep_state = self.sleep_manager.get_current_sleep_state()
        if current_sleep_state != SleepState.SLEEPING:
            return False

        old_value = self.wakeup_value

        if is_private_chat:
            # 私聊每条消息都增加唤醒度
            self.wakeup_value += self.private_message_increment
            logger.debug(f"私聊消息增加唤醒度: +{self.private_message_increment}")
        elif is_mentioned:
            # 群聊只有被艾特才增加唤醒度
            self.wakeup_value += self.group_mention_increment
            logger.debug(f"群聊艾特增加唤醒度: +{self.group_mention_increment}")
        else:
            # 群聊未被艾特，不增加唤醒度
            return False

        current_time = time.time()
        if current_time - self.last_log_time > self.log_interval:
            logger.info(
                f"唤醒度变化: {old_value:.1f} -> {self.wakeup_value:.1f} (阈值: {self.wakeup_threshold})"
            )
            self.last_log_time = current_time
        else:
            logger.debug(
                f"唤醒度变化: {old_value:.1f} -> {self.wakeup_value:.1f} (阈值: {self.wakeup_threshold})"
            )

        # 检查是否达到唤醒阈值
        if self.wakeup_value >= self.wakeup_threshold:
            self._trigger_wakeup()
            return True

        self._save_wakeup_state()
        return False

    def _trigger_wakeup(self):
        """触发唤醒，进入愤怒状态"""
        self.is_angry = True
        self.angry_start_time = time.time()
        self.wakeup_value = 0.0  # 重置唤醒度

        self._save_wakeup_state()

        # 通知情绪管理系统进入愤怒状态
        from src.mood.mood_manager import mood_manager
        mood_manager.set_angry_from_wakeup("global_mood")

        # 通知SleepManager重置睡眠状态
        self.sleep_manager.reset_sleep_state_after_wakeup()

        logger.info(f"唤醒度达到阈值({self.wakeup_threshold})，被吵醒进入愤怒状态！")

    def get_angry_prompt_addition(self) -> str:
        """获取愤怒状态下的提示词补充"""
        if self.is_angry:
            return self.angry_prompt
        return ""

    def is_in_angry_state(self) -> bool:
        """检查是否处于愤怒状态"""
        if self.is_angry:
            current_time = time.time()
            if current_time - self.angry_start_time >= self.angry_duration:
                self.is_angry = False
                # 通知情绪管理系统清除愤怒状态
                from src.mood.mood_manager import mood_manager
                mood_manager.clear_angry_from_wakeup("global_mood")
                logger.info("愤怒状态自动过期")
                return False
        return self.is_angry

    def get_status_info(self) -> dict:
        """获取当前状态信息"""
        return {
            "wakeup_value": self.wakeup_value,
            "wakeup_threshold": self.wakeup_threshold,
            "is_angry": self.is_angry,
            "angry_remaining_time": max(0, self.angry_duration - (time.time() - self.angry_start_time))
            if self.is_angry
            else 0,
        }
