"""
主规划器入口，负责协调 PlanGenerator, PlanFilter, 和 PlanExecutor。
集成兴趣度评分系统和用户关系追踪机制，实现智能化的聊天决策。
"""

from dataclasses import asdict
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from src.plugins.built_in.affinity_flow_chatter.plan_executor import ChatterPlanExecutor
from src.plugins.built_in.affinity_flow_chatter.plan_filter import ChatterPlanFilter
from src.plugins.built_in.affinity_flow_chatter.plan_generator import ChatterPlanGenerator
from src.plugins.built_in.affinity_flow_chatter.interest_scoring import chatter_interest_scoring_system
from src.mood.mood_manager import mood_manager


from src.common.logger import get_logger
from src.config.config import global_config

if TYPE_CHECKING:
    from src.common.data_models.message_manager_data_model import StreamContext
    from src.common.data_models.info_data_model import Plan
    from src.chat.planner_actions.action_manager import ChatterActionManager

# 导入提示词模块以确保其被初始化
from src.plugins.built_in.affinity_flow_chatter import planner_prompts  # noqa

logger = get_logger("planner")


class ChatterActionPlanner:
    """
    增强版ActionPlanner，集成兴趣度评分和用户关系追踪机制。

    核心功能：
    1. 兴趣度评分系统：根据兴趣匹配度、关系分、提及度、时间因子对消息评分
    2. 用户关系追踪：自动追踪用户交互并更新关系分
    3. 智能回复决策：基于兴趣度阈值和连续不回复概率的智能决策
    4. 完整的规划流程：生成→筛选→执行的完整三阶段流程
    """

    def __init__(self, chat_id: str, action_manager: "ChatterActionManager"):
        """
        初始化增强版ActionPlanner。

        Args:
            chat_id (str): 当前聊天的 ID。
            action_manager (ChatterActionManager): 一个 ChatterActionManager 实例。
        """
        self.chat_id = chat_id
        self.action_manager = action_manager
        self.generator = ChatterPlanGenerator(chat_id)
        self.executor = ChatterPlanExecutor(action_manager)

        # 使用新的统一兴趣度管理系统

        # 规划器统计
        self.planner_stats = {
            "total_plans": 0,
            "successful_plans": 0,
            "failed_plans": 0,
            "replies_generated": 0,
            "other_actions_executed": 0,
        }

    async def plan(self, context: "StreamContext" = None) -> Tuple[List[Dict], Optional[Dict]]:
        """
        执行完整的增强版规划流程。

        Args:
            context (StreamContext): 包含聊天流消息的上下文对象。

        Returns:
            Tuple[List[Dict], Optional[Dict]]: 一个元组，包含：
                - final_actions_dict (List[Dict]): 最终确定的动作列表（字典格式）。
                - final_target_message_dict (Optional[Dict]): 最终的目标消息（字典格式）。
        """
        try:
            self.planner_stats["total_plans"] += 1

            return await self._enhanced_plan_flow(context)

        except Exception as e:
            logger.error(f"规划流程出错: {e}")
            self.planner_stats["failed_plans"] += 1
            return [], None

    async def _enhanced_plan_flow(self, context: "StreamContext") -> Tuple[List[Dict], Optional[Dict]]:
        """执行增强版规划流程"""
        try:
            # 在规划前，先进行动作修改
            from src.chat.planner_actions.action_modifier import ActionModifier
            action_modifier = ActionModifier(self.action_manager, self.chat_id)
            await action_modifier.modify_actions()
            
            # 1. 生成初始 Plan
            initial_plan = await self.generator.generate(context.chat_mode)

            # 确保Plan中包含所有当前可用的动作
            initial_plan.available_actions = self.action_manager.get_using_actions()
            
            unread_messages = context.get_unread_messages() if context else []
            # 2. 使用新的兴趣度管理系统进行评分
            score = 0.0
            should_reply = False
            reply_not_available = False
            interest_updates: List[Dict[str, Any]] = []

            if unread_messages:
                # 为每条消息计算兴趣度，并延迟提交数据库更新
                for message in unread_messages:
                    try:
                        interest_score = await chatter_interest_scoring_system._calculate_single_message_score(
                            message=message,
                            bot_nickname=global_config.bot.nickname,
                        )
                        message_interest = interest_score.total_score

                        message.interest_value = message_interest
                        message.should_reply = message_interest > global_config.affinity_flow.non_reply_action_interest_threshold

                        interest_updates.append(
                            {
                                "message_id": message.message_id,
                                "interest_value": message_interest,
                                "should_reply": message.should_reply,
                            }
                        )

                        logger.debug(
                            f"消息 {message.message_id} 兴趣度: {message_interest:.3f}, 应回复: {message.should_reply}"
                        )

                        if message_interest > score:
                            score = message_interest
                            if message.should_reply:
                                should_reply = True
                            else:
                                reply_not_available = True

                    except Exception as e:
                        logger.warning(f"计算消息 {message.message_id} 兴趣度失败: {e}")
                        message.interest_value = 0.0
                        message.should_reply = False
                        interest_updates.append(
                            {
                                "message_id": message.message_id,
                                "interest_value": 0.0,
                                "should_reply": False,
                            }
                        )

                if interest_updates:
                    await self._commit_interest_updates(interest_updates)

            # 检查兴趣度是否达到非回复动作阈值
            non_reply_action_interest_threshold = global_config.affinity_flow.non_reply_action_interest_threshold
            if score < non_reply_action_interest_threshold:
                logger.info(f"兴趣度 {score:.3f} 低于阈值 {non_reply_action_interest_threshold:.3f}，不执行动作")
                # 直接返回 no_action
                from src.common.data_models.info_data_model import ActionPlannerInfo

                no_action = ActionPlannerInfo(
                    action_type="no_action",
                    reasoning=f"兴趣度评分 {score:.3f} 未达阈值 {non_reply_action_interest_threshold:.3f}",
                    action_data={},
                    action_message=None,
                )
                filtered_plan = initial_plan
                filtered_plan.decided_actions = [no_action]
            else:
                # 4. 筛选 Plan
                available_actions = list(initial_plan.available_actions.keys())
                plan_filter = ChatterPlanFilter(self.chat_id, available_actions)
                filtered_plan = await plan_filter.filter(reply_not_available, initial_plan)

            # 检查filtered_plan是否有reply动作，用于统计
            has_reply_action = any(decision.action_type == "reply" for decision in filtered_plan.decided_actions)

            # 5. 使用 PlanExecutor 执行 Plan
            execution_result = await self.executor.execute(filtered_plan)

            # 6. 根据执行结果更新统计信息
            self._update_stats_from_execution_result(execution_result)

            # 7. 返回结果
            return self._build_return_result(filtered_plan)

        except Exception as e:
            logger.error(f"增强版规划流程出错: {e}")
            self.planner_stats["failed_plans"] += 1
            return [], None

    async def _commit_interest_updates(self, updates: List[Dict[str, Any]]) -> None:
        """统一更新消息兴趣度，减少数据库写入次数"""
        if not updates:
            return

        try:
            from src.chat.message_manager.message_manager import message_manager

            await message_manager.bulk_update_messages(self.chat_id, updates)
        except Exception as e:
            logger.warning(f"批量更新上下文消息兴趣度失败: {e}")

        try:
            from src.chat.message_receive.storage import MessageStorage

            interest_map = {item["message_id"]: item["interest_value"] for item in updates if "interest_value" in item}
            reply_map = {item["message_id"]: item["should_reply"] for item in updates if "should_reply" in item}

            await MessageStorage.bulk_update_interest_values(
                interest_map=interest_map,
                reply_map=reply_map if reply_map else None,
            )

            logger.debug(f"已批量更新 {len(interest_map)} 条消息的兴趣度")
        except Exception as e:
            logger.warning(f"批量更新数据库兴趣度失败: {e}")

    def _update_stats_from_execution_result(self, execution_result: Dict[str, any]):
        """根据执行结果更新规划器统计"""
        if not execution_result:
            return

        successful_count = execution_result.get("successful_count", 0)

        # 更新成功执行计数
        self.planner_stats["successful_plans"] += successful_count

        # 统计回复动作和其他动作
        reply_count = 0
        other_count = 0

        for result in execution_result.get("results", []):
            action_type = result.get("action_type", "")
            if action_type in ["reply", "proactive_reply"]:
                reply_count += 1
            else:
                other_count += 1

        self.planner_stats["replies_generated"] += reply_count
        self.planner_stats["other_actions_executed"] += other_count

    def _build_return_result(self, plan: "Plan") -> Tuple[List[Dict], Optional[Dict]]:
        """构建返回结果"""
        final_actions = plan.decided_actions or []
        final_target_message = next((act.action_message for act in final_actions if act.action_message), None)

        final_actions_dict = [asdict(act) for act in final_actions]

        if final_target_message:
            if hasattr(final_target_message, "__dataclass_fields__"):
                final_target_message_dict = asdict(final_target_message)
            else:
                final_target_message_dict = final_target_message
        else:
            final_target_message_dict = None

        return final_actions_dict, final_target_message_dict

    def get_planner_stats(self) -> Dict[str, any]:
        """获取规划器统计"""
        return self.planner_stats.copy()

    def get_current_mood_state(self) -> str:
        """获取当前聊天的情绪状态"""
        chat_mood = mood_manager.get_mood_by_chat_id(self.chat_id)
        return chat_mood.mood_state

    def get_mood_stats(self) -> Dict[str, any]:
        """获取情绪状态统计"""
        chat_mood = mood_manager.get_mood_by_chat_id(self.chat_id)
        return {
            "current_mood": chat_mood.mood_state,
            "is_angry_from_wakeup": chat_mood.is_angry_from_wakeup,
            "regression_count": chat_mood.regression_count,
            "last_change_time": chat_mood.last_change_time,
        }


# 全局兴趣度评分系统实例 - 在 individuality 模块中创建
