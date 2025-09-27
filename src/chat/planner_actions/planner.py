"""
主规划器入口，负责协调 PlanGenerator, PlanFilter, 和 PlanExecutor。
"""
from dataclasses import asdict
from typing import Dict, List, Optional, Tuple

from src.chat.planner_actions.action_manager import ActionManager
from src.chat.planner_actions.plan_executor import PlanExecutor
from src.chat.planner_actions.plan_filter import PlanFilter
from src.chat.planner_actions.plan_generator import PlanGenerator
from src.common.logger import get_logger
from src.plugin_system.base.component_types import ChatMode
import src.chat.planner_actions.planner_prompts #noga  # noqa: F401
# 导入提示词模块以确保其被初始化

logger = get_logger("planner")


class ActionPlanner:
    """
    ActionPlanner 是规划系统的核心协调器。

    它负责整合规划流程的三个主要阶段：
    1.  **生成 (Generate)**: 使用 PlanGenerator 创建一个初始的行动计划。
    2.  **筛选 (Filter)**: 使用 PlanFilter 对生成的计划进行审查和优化。
    3.  **执行 (Execute)**: 使用 PlanExecutor 执行最终确定的行动。

    Attributes:
        chat_id (str): 当前聊天的唯一标识符。
        action_manager (ActionManager): 用于执行具体动作的管理器。
        generator (PlanGenerator): 负责生成初始计划。
        filter (PlanFilter): 负责筛选和优化计划。
        executor (PlanExecutor): 负责执行最终计划。
    """

    def __init__(self, chat_id: str, action_manager: ActionManager):
        """
        初始化 ActionPlanner。

        Args:
            chat_id (str): 当前聊天的 ID。
            action_manager (ActionManager): 一个 ActionManager 实例。
        """
        self.chat_id = chat_id
        self.action_manager = action_manager
        self.generator = PlanGenerator(chat_id)
        self.filter = PlanFilter()
        self.executor = PlanExecutor(action_manager)

    async def plan(
        self, mode: ChatMode = ChatMode.FOCUS
    ) -> Tuple[List[Dict], Optional[Dict]]:
        """
        执行从生成到执行的完整规划流程。

        这个方法按顺序协调生成、筛选和执行三个阶段。

        Args:
            mode (ChatMode): 当前的聊天模式，默认为 FOCUS。

        Returns:
            Tuple[List[Dict], Optional[Dict]]: 一个元组，包含：
                - final_actions_dict (List[Dict]): 最终确定的动作列表（字典格式）。
                - final_target_message_dict (Optional[Dict]): 最终的目标消息（字典格式），如果没有则为 None。
                这与旧版 planner 的返回值保持兼容。
        """
        # 1. 生成初始 Plan
        initial_plan = await self.generator.generate(mode)

        # 2. 筛选 Plan
        filtered_plan = await self.filter.filter(initial_plan)

        # 3. 执行 Plan(临时引爆因为它暂时还跑不了)
        #await self.executor.execute(filtered_plan)

        # 4. 返回结果 (与旧版 planner 的返回值保持兼容)
        final_actions = filtered_plan.decided_actions or []
        final_target_message = next(
            (act.action_message for act in final_actions if act.action_message), None
        )
        
        final_actions_dict = [asdict(act) for act in final_actions]
        # action_message现在可能是字典而不是dataclass实例，需要特殊处理
        if final_target_message:
            if hasattr(final_target_message, '__dataclass_fields__'):
                # 如果是dataclass实例，使用asdict转换
                final_target_message_dict = asdict(final_target_message)
            else:
                # 如果已经是字典，直接使用
                final_target_message_dict = final_target_message
        else:
            final_target_message_dict = None

        return final_actions_dict, final_target_message_dict
