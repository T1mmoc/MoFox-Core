from enum import Enum, auto
from datetime import datetime
from src.common.logger import get_logger
from src.manager.local_store_manager import local_storage

logger = get_logger("sleep_state")


class SleepState(Enum):
    """
    定义了角色可能处于的几种睡眠状态。
    这是一个状态机，用于管理角色的睡眠周期。
    """

    AWAKE = auto()  # 清醒状态
    INSOMNIA = auto()  # 失眠状态
    PREPARING_SLEEP = auto()  # 准备入睡状态，一个短暂的过渡期
    SLEEPING = auto()  # 正在睡觉状态
    WOKEN_UP = auto()  # 被吵醒状态


class SleepStateSerializer:
    """
    睡眠状态序列化器。
    负责将内存中的睡眠状态对象持久化到本地存储（如JSON文件），
    以及在程序启动时从本地存储中恢复状态。
    这样可以确保即使程序重启，角色的睡眠状态也能得以保留。
    """
    @staticmethod
    def save(state_data: dict):
        """
        将当前的睡眠状态数据保存到本地存储。

        Args:
            state_data (dict): 包含睡眠状态信息的字典。
                               datetime对象会被转换为时间戳，Enum成员会被转换为其名称字符串。
        """
        try:
            # 准备要序列化的数据字典
            state = {
                # 保存当前状态的枚举名称
                "current_state": state_data["_current_state"].name,
                # 将datetime对象转换为Unix时间戳以便序列化
                "sleep_buffer_end_time_ts": state_data["_sleep_buffer_end_time"].timestamp()
                if state_data["_sleep_buffer_end_time"]
                else None,
                "total_delayed_minutes_today": state_data["_total_delayed_minutes_today"],
                # 将date对象转换为ISO格式的字符串
                "last_sleep_check_date_str": state_data["_last_sleep_check_date"].isoformat()
                if state_data["_last_sleep_check_date"]
                else None,
                "re_sleep_attempt_time_ts": state_data["_re_sleep_attempt_time"].timestamp()
                if state_data["_re_sleep_attempt_time"]
                else None,
            }
            # 写入本地存储
            local_storage["schedule_sleep_state"] = state
            logger.debug(f"已保存睡眠状态: {state}")
        except Exception as e:
            logger.error(f"保存睡眠状态失败: {e}")

    @staticmethod
    def load() -> dict:
        """
        从本地存储加载并解析睡眠状态。

        Returns:
            dict: 包含恢复后睡眠状态信息的字典。
                  如果加载失败或没有找到数据，则返回一个默认的清醒状态。
        """
        # 定义一个默认的状态，以防加载失败
        state_data = {
            "_current_state": SleepState.AWAKE,
            "_sleep_buffer_end_time": None,
            "_total_delayed_minutes_today": 0,
            "_last_sleep_check_date": None,
            "_re_sleep_attempt_time": None,
        }
        try:
            # 从本地存储读取数据
            state = local_storage["schedule_sleep_state"]
            if state and isinstance(state, dict):
                # 恢复当前状态枚举
                state_name = state.get("current_state")
                if state_name and hasattr(SleepState, state_name):
                    state_data["_current_state"] = SleepState[state_name]

                # 从时间戳恢复datetime对象
                end_time_ts = state.get("sleep_buffer_end_time_ts")
                if end_time_ts:
                    state_data["_sleep_buffer_end_time"] = datetime.fromtimestamp(end_time_ts)

                # 恢复重新入睡尝试时间
                re_sleep_ts = state.get("re_sleep_attempt_time_ts")
                if re_sleep_ts:
                    state_data["_re_sleep_attempt_time"] = datetime.fromtimestamp(re_sleep_ts)

                # 恢复今日延迟睡眠总分钟数
                state_data["_total_delayed_minutes_today"] = state.get("total_delayed_minutes_today", 0)

                # 从ISO格式字符串恢复date对象
                date_str = state.get("last_sleep_check_date_str")
                if date_str:
                    state_data["_last_sleep_check_date"] = datetime.fromisoformat(date_str).date()

                logger.info(f"成功从本地存储加载睡眠状态: {state}")
        except Exception as e:
            # 如果加载过程中出现任何问题，记录警告并返回默认状态
            logger.warning(f"加载睡眠状态失败，将使用默认值: {e}")
        return state_data