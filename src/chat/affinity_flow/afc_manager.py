"""
亲和力聊天处理流管理器
管理不同聊天流的亲和力聊天处理流，统一获取新消息并分发到对应的亲和力聊天处理流
"""
import time
import traceback
from typing import Dict, Optional, List

from src.chat.planner_actions.action_manager import ActionManager
from src.chat.planner_actions.planner import ActionPlanner
from src.chat.affinity_flow.chatter import AffinityFlowChatter
from src.common.logger import get_logger

logger = get_logger("afc_manager")


class AFCManager:
    """亲和力聊天处理流管理器"""

    def __init__(self):
        self.affinity_flow_chatters: Dict[str, "AffinityFlowChatter"] = {}
        '''所有聊天流的亲和力聊天处理流，stream_id -> affinity_flow_chatter'''

        # 动作管理器
        self.action_manager = ActionManager()

        # 管理器统计
        self.manager_stats = {
            "total_messages_processed": 0,
            "total_plans_created": 0,
            "total_actions_executed": 0,
            "active_chatters": 0,
            "last_activity_time": time.time(),
        }

    def get_or_create_chatter(self, stream_id: str) -> "AffinityFlowChatter":
        """获取或创建聊天流处理器"""
        if stream_id not in self.affinity_flow_chatters:
            # 创建增强版规划器
            planner = ActionPlanner(stream_id, self.action_manager)

            chatter = AffinityFlowChatter(
                stream_id=stream_id,
                planner=planner,
                action_manager=self.action_manager
            )
            self.affinity_flow_chatters[stream_id] = chatter
            logger.info(f"创建新的亲和力聊天处理器: {stream_id}")

        return self.affinity_flow_chatters[stream_id]

    async def process_message(self, stream_id: str, message_data: dict) -> Dict[str, any]:
        """处理消息"""
        try:
            # 获取或创建聊天处理器
            chatter = self.get_or_create_chatter(stream_id)

            # 处理消息
            result = await chatter.process_message(message_data)

            # 更新统计
            self.manager_stats["total_messages_processed"] += 1
            self.manager_stats["total_actions_executed"] += result.get("executed_count", 0)
            self.manager_stats["last_activity_time"] = time.time()

            return result

        except Exception as e:
            logger.error(f"处理消息时出错: {e}\n{traceback.format_exc()}")
            return {
                "success": False,
                "error_message": str(e),
                "executed_count": 0,
            }

    async def process_messages_batch(self, stream_id: str, messages_data: List[dict]) -> List[Dict[str, any]]:
        """批量处理消息"""
        results = []
        for message_data in messages_data:
            result = await self.process_message(stream_id, message_data)
            results.append(result)
        return results

    def get_chatter_stats(self, stream_id: str) -> Optional[Dict[str, any]]:
        """获取聊天处理器统计"""
        if stream_id in self.affinity_flow_chatters:
            return self.affinity_flow_chatters[stream_id].get_stats()
        return None

    def get_manager_stats(self) -> Dict[str, any]:
        """获取管理器统计"""
        stats = self.manager_stats.copy()
        stats["active_chatters"] = len(self.affinity_flow_chatters)
        return stats

    def cleanup_inactive_chatters(self, max_inactive_minutes: int = 60):
        """清理不活跃的聊天处理器"""
        current_time = time.time()
        max_inactive_seconds = max_inactive_minutes * 60

        inactive_streams = []
        for stream_id, chatter in self.affinity_flow_chatters.items():
            if current_time - chatter.last_activity_time > max_inactive_seconds:
                inactive_streams.append(stream_id)

        for stream_id in inactive_streams:
            del self.affinity_flow_chatters[stream_id]
            logger.info(f"清理不活跃聊天处理器: {stream_id}")

    def get_planner_stats(self, stream_id: str) -> Optional[Dict[str, any]]:
        """获取规划器统计"""
        if stream_id in self.affinity_flow_chatters:
            return self.affinity_flow_chatters[stream_id].get_planner_stats()
        return None

    def get_interest_scoring_stats(self, stream_id: str) -> Optional[Dict[str, any]]:
        """获取兴趣度评分统计"""
        if stream_id in self.affinity_flow_chatters:
            return self.affinity_flow_chatters[stream_id].get_interest_scoring_stats()
        return None

    def get_relationship_stats(self, stream_id: str) -> Optional[Dict[str, any]]:
        """获取用户关系统计"""
        if stream_id in self.affinity_flow_chatters:
            return self.affinity_flow_chatters[stream_id].get_relationship_stats()
        return None

    def get_user_relationship(self, stream_id: str, user_id: str) -> float:
        """获取用户关系分"""
        if stream_id in self.affinity_flow_chatters:
            return self.affinity_flow_chatters[stream_id].get_user_relationship(user_id)
        return 0.3  # 默认新用户关系分

    def update_interest_keywords(self, stream_id: str, new_keywords: dict):
        """更新兴趣关键词"""
        if stream_id in self.affinity_flow_chatters:
            self.affinity_flow_chatters[stream_id].update_interest_keywords(new_keywords)
            logger.info(f"已更新聊天流 {stream_id} 的兴趣关键词: {list(new_keywords.keys())}")

afc_manager = AFCManager()