"""
PlanExecutor: 接收 Plan 对象并执行其中的所有动作。
集成用户关系追踪机制，自动记录交互并更新关系。
"""

import asyncio
import time
from typing import Dict, List

from src.config.config import global_config
from src.chat.planner_actions.action_manager import ChatterActionManager
from src.common.data_models.info_data_model import Plan, ActionPlannerInfo
from src.common.logger import get_logger

logger = get_logger("plan_executor")


class ChatterPlanExecutor:
    """
    增强版PlanExecutor，集成用户关系追踪机制。

    功能：
    1. 执行Plan中的所有动作
    2. 自动记录用户交互并添加到关系追踪
    3. 分类执行回复动作和其他动作
    4. 提供完整的执行统计和监控
    """

    def __init__(self, action_manager: ChatterActionManager):
        """
        初始化增强版PlanExecutor。

        Args:
            action_manager (ChatterActionManager): 用于实际执行各种动作的管理器实例。
        """
        self.action_manager = action_manager

        # 执行统计
        self.execution_stats = {
            "total_executed": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "reply_executions": 0,
            "other_action_executions": 0,
            "execution_times": [],
        }

        # 用户关系追踪引用
        self.relationship_tracker = None

    def set_relationship_tracker(self, relationship_tracker):
        """设置关系追踪器"""
        self.relationship_tracker = relationship_tracker

    async def execute(self, plan: Plan) -> Dict[str, any]:
        """
        遍历并执行Plan对象中`decided_actions`列表里的所有动作。

        Args:
            plan (Plan): 包含待执行动作列表的Plan对象。

        Returns:
            Dict[str, any]: 执行结果统计信息
        """
        if not plan.decided_actions:
            logger.info("没有需要执行的动作。")
            return {"executed_count": 0, "results": []}

        execution_results = []
        reply_actions = []
        other_actions = []

        # 分类动作：回复动作和其他动作
        for action_info in plan.decided_actions:
            if action_info.action_type in ["reply", "proactive_reply"]:
                reply_actions.append(action_info)
            else:
                other_actions.append(action_info)

        # 执行回复动作（优先执行）
        if reply_actions:
            reply_result = await self._execute_reply_actions(reply_actions, plan)
            execution_results.extend(reply_result["results"])
            self.execution_stats["reply_executions"] += len(reply_actions)

        # 将其他动作放入后台任务执行，避免阻塞主流程
        if other_actions:
            asyncio.create_task(self._execute_other_actions(other_actions, plan))
            logger.info(f"已将 {len(other_actions)} 个其他动作放入后台任务执行。")
            # 注意：后台任务的结果不会立即计入本次返回的统计数据

        # 更新总体统计
        self.execution_stats["total_executed"] += len(plan.decided_actions)
        successful_count = sum(1 for r in execution_results if r["success"])
        self.execution_stats["successful_executions"] += successful_count
        self.execution_stats["failed_executions"] += len(execution_results) - successful_count

        logger.info(
            f"规划执行完成: 总数={len(plan.decided_actions)}, 成功={successful_count}, 失败={len(execution_results) - successful_count}"
        )

        return {
            "executed_count": len(plan.decided_actions),
            "successful_count": successful_count,
            "failed_count": len(execution_results) - successful_count,
            "results": execution_results,
        }

    async def _execute_reply_actions(self, reply_actions: List[ActionPlannerInfo], plan: Plan) -> Dict[str, any]:
        """执行回复动作"""
        results = []

        for action_info in reply_actions:
            result = await self._execute_single_reply_action(action_info, plan)
            results.append(result)

        return {"results": results}

    async def _execute_single_reply_action(self, action_info: ActionPlannerInfo, plan: Plan) -> Dict[str, any]:
        """执行单个回复动作"""
        start_time = time.time()
        success = False
        error_message = ""
        reply_content = ""

        try:
            logger.info(f"执行回复动作: {action_info.action_type} (原因: {action_info.reasoning})")

            # 获取用户ID - 兼容对象和字典
            if hasattr(action_info.action_message, "user_info"):
                user_id = action_info.action_message.user_info.user_id
            else:
                user_id = action_info.action_message.get("user_info", {}).get("user_id")

            if user_id == str(global_config.bot.qq_account):
                logger.warning("尝试回复自己，跳过此动作以防止死循环。")
                return {
                    "action_type": action_info.action_type,
                    "success": False,
                    "error_message": "尝试回复自己，跳过此动作以防止死循环。",
                    "execution_time": 0,
                    "reasoning": action_info.reasoning,
                    "reply_content": "",
                }
            # 构建回复动作参数
            action_params = {
                "chat_id": plan.chat_id,
                "target_message": action_info.action_message,
                "reasoning": action_info.reasoning,
                "action_data": action_info.action_data or {},
            }

            logger.debug(f"📬 [PlanExecutor] 准备调用 ActionManager，target_message: {action_info.action_message}")

            # 通过动作管理器执行回复
            reply_content = await self.action_manager.execute_action(
                action_name=action_info.action_type, **action_params
            )

            success = True
            logger.info(f"回复动作 '{action_info.action_type}' 执行成功。")

        except Exception as e:
            error_message = str(e)
            logger.error(f"执行回复动作失败: {action_info.action_type}, 错误: {error_message}")

        # 记录用户关系追踪
        if success and action_info.action_message:
            await self._track_user_interaction(action_info, plan, reply_content)

        execution_time = time.time() - start_time
        self.execution_stats["execution_times"].append(execution_time)

        return {
            "action_type": action_info.action_type,
            "success": success,
            "error_message": error_message,
            "execution_time": execution_time,
            "reasoning": action_info.reasoning,
            "reply_content": reply_content[:200] + "..." if len(reply_content) > 200 else reply_content,
        }

    async def _execute_other_actions(self, other_actions: List[ActionPlannerInfo], plan: Plan) -> Dict[str, any]:
        """执行其他动作"""
        results = []

        # 并行执行其他动作
        tasks = []
        for action_info in other_actions:
            task = self._execute_single_other_action(action_info, plan)
            tasks.append(task)

        if tasks:
            executed_results = await asyncio.gather(*tasks, return_exceptions=True)
            for i, result in enumerate(executed_results):
                if isinstance(result, Exception):
                    logger.error(f"执行动作 {other_actions[i].action_type} 时发生异常: {result}")
                    results.append(
                        {
                            "action_type": other_actions[i].action_type,
                            "success": False,
                            "error_message": str(result),
                            "execution_time": 0,
                            "reasoning": other_actions[i].reasoning,
                        }
                    )
                else:
                    results.append(result)

        return {"results": results}

    async def _execute_single_other_action(self, action_info: ActionPlannerInfo, plan: Plan) -> Dict[str, any]:
        """执行单个其他动作"""
        start_time = time.time()
        success = False
        error_message = ""

        try:
            logger.info(f"执行其他动作: {action_info.action_type} (原因: {action_info.reasoning})")

            action_data = action_info.action_data or {}

            # 针对 poke_user 动作，特殊处理
            if action_info.action_type == "poke_user":
                target_message = action_info.action_message
                if target_message:
                    # 优先直接获取 user_id，这才是最可靠的信息
                    user_id = target_message.get("user_id")
                    if user_id:
                        action_data["user_id"] = user_id
                        logger.info(f"检测到戳一戳动作，目标用户ID: {user_id}")
                    else:
                        # 如果没有 user_id，再尝试用 user_nickname 作为备用方案
                        user_name = target_message.get("user_nickname")
                        if user_name:
                            action_data["user_name"] = user_name
                            logger.info(f"检测到戳一戳动作，目标用户: {user_name}")
                        else:
                            logger.warning("无法从戳一戳消息中获取用户ID或昵称。")

                    # 传递原始消息ID以支持引用
                    action_data["target_message_id"] = target_message.get("message_id")

            # 构建动作参数
            action_params = {
                "chat_id": plan.chat_id,
                "target_message": action_info.action_message,
                "reasoning": action_info.reasoning,
                "action_data": action_data,
            }

            # 通过动作管理器执行动作
            await self.action_manager.execute_action(action_name=action_info.action_type, **action_params)

            success = True
            logger.info(f"其他动作 '{action_info.action_type}' 执行成功。")

        except Exception as e:
            error_message = str(e)
            logger.error(f"执行其他动作失败: {action_info.action_type}, 错误: {error_message}")

        execution_time = time.time() - start_time
        self.execution_stats["execution_times"].append(execution_time)

        return {
            "action_type": action_info.action_type,
            "success": success,
            "error_message": error_message,
            "execution_time": execution_time,
            "reasoning": action_info.reasoning,
        }

    async def _track_user_interaction(self, action_info: ActionPlannerInfo, plan: Plan, reply_content: str):
        """追踪用户交互 - 集成回复后关系追踪"""
        try:
            if not action_info.action_message:
                return

            # 获取用户信息 - 处理对象和字典两种情况
            if hasattr(action_info.action_message, "user_info"):
                # 对象情况
                user_info = action_info.action_message.user_info
                user_id = user_info.user_id
                user_name = user_info.user_nickname or user_id
                user_message = action_info.action_message.content
            else:
                # 字典情况
                user_info = action_info.action_message.get("user_info", {})
                user_id = user_info.get("user_id")
                user_name = user_info.get("user_nickname") or user_id
                user_message = action_info.action_message.get("content", "")

            if not user_id:
                logger.debug("跳过追踪：缺少用户ID")
                return

            # 如果有设置关系追踪器，执行回复后关系追踪
            if self.relationship_tracker:
                # 记录基础交互信息（保持向后兼容）
                self.relationship_tracker.add_interaction(
                    user_id=user_id,
                    user_name=user_name,
                    user_message=user_message,
                    bot_reply=reply_content,
                    reply_timestamp=time.time(),
                )

                # 执行新的回复后关系追踪
                await self.relationship_tracker.track_reply_relationship(
                    user_id=user_id, user_name=user_name, bot_reply_content=reply_content, reply_timestamp=time.time()
                )

                logger.debug(f"已执行用户交互追踪: {user_id}")

        except Exception as e:
            logger.error(f"追踪用户交互时出错: {e}")
            logger.debug(f"action_message类型: {type(action_info.action_message)}")
            logger.debug(f"action_message内容: {action_info.action_message}")

    def get_execution_stats(self) -> Dict[str, any]:
        """获取执行统计信息"""
        stats = self.execution_stats.copy()

        # 计算平均执行时间
        if stats["execution_times"]:
            avg_time = sum(stats["execution_times"]) / len(stats["execution_times"])
            stats["average_execution_time"] = avg_time
            stats["max_execution_time"] = max(stats["execution_times"])
            stats["min_execution_time"] = min(stats["execution_times"])
        else:
            stats["average_execution_time"] = 0
            stats["max_execution_time"] = 0
            stats["min_execution_time"] = 0

        # 移除执行时间列表以避免返回过大数据
        stats.pop("execution_times", None)

        return stats

    def reset_stats(self):
        """重置统计信息"""
        self.execution_stats = {
            "total_executed": 0,
            "successful_executions": 0,
            "failed_executions": 0,
            "reply_executions": 0,
            "other_action_executions": 0,
            "execution_times": [],
        }

    def get_recent_performance(self, limit: int = 10) -> List[Dict[str, any]]:
        """获取最近的执行性能"""
        recent_times = self.execution_stats["execution_times"][-limit:]
        if not recent_times:
            return []

        return [
            {
                "execution_index": i + 1,
                "execution_time": time_val,
                "timestamp": time.time() - (len(recent_times) - i) * 60,  # 估算时间戳
            }
            for i, time_val in enumerate(recent_times)
        ]
