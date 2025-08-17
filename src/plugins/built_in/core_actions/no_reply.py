from typing import Tuple, List
from collections import deque

# 导入新插件系统
from src.plugin_system import BaseAction, ActionActivationType, ChatMode
from src.plugin_system.base.component_types import ChatType

# 导入依赖的系统组件
from src.common.logger import get_logger


logger = get_logger("no_reply_action")


class NoReplyAction(BaseAction):
    """不回复动作，支持waiting和breaking两种形式."""

    focus_activation_type = ActionActivationType.NEVER
    normal_activation_type = ActionActivationType.NEVER
    mode_enable = ChatMode.FOCUS
    parallel_action = False

    # 动作基本信息
    action_name = "no_reply"
    action_description = "暂时不回复消息"
    
    # 最近三次no_reply的新消息兴趣度记录
    _recent_interest_records: deque = deque(maxlen=3)

    # 兴趣值退出阈值
    _interest_exit_threshold = 3.0
    # 消息数量退出阈值
    _min_exit_message_count = 3
    _max_exit_message_count = 6

    # 动作参数定义
    action_parameters = {}

    # 动作使用场景
    action_require = [""]

    # 关联类型
    associated_types = []

    async def execute(self) -> Tuple[bool, str]:
        """执行不回复动作"""

        try:
            reason = self.action_data.get("reason", "")
            
            logger.info(f"{self.log_prefix} 选择不回复，原因: {reason}")
            
            await self.store_action_info(
                action_build_into_prompt=False,
                action_prompt_display=reason,
                action_done=True,
            )
            return True, reason

        except Exception as e:
            logger.error(f"{self.log_prefix} 不回复动作执行失败: {e}")
            exit_reason = f"执行异常: {str(e)}"
            full_prompt = f"no_reply执行异常: {exit_reason}，你思考是否要进行回复"
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=full_prompt,
                action_done=True,
            )
            return False, f"不回复动作执行失败: {e}"

    @classmethod
    def reset_consecutive_count(cls):
        """重置连续计数器和兴趣度记录"""
        cls._recent_interest_records.clear()
        logger.debug("NoReplyAction连续计数器和兴趣度记录已重置")

    @classmethod
    def get_recent_interest_records(cls) -> List[float]:
        """获取最近的兴趣度记录"""
        return list(cls._recent_interest_records)

