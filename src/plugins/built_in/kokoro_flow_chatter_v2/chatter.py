"""
Kokoro Flow Chatter V2 - Chatter 主类

极简设计，只负责：
1. 收到消息
2. 调用 Replyer 生成响应
3. 执行动作
4. 更新 Session
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from src.common.data_models.message_manager_data_model import StreamContext
from src.common.logger import get_logger
from src.plugin_system.base.base_chatter import BaseChatter
from src.plugin_system.base.component_types import ChatType

from .action_executor import ActionExecutor
from .models import EventType, SessionStatus
from .replyer import generate_response
from .session import get_session_manager

if TYPE_CHECKING:
    from src.chat.planner_actions.action_manager import ChatterActionManager
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("kfc_v2_chatter")

# 控制台颜色
SOFT_PURPLE = "\033[38;5;183m"
RESET = "\033[0m"


class KokoroFlowChatterV2(BaseChatter):
    """
    Kokoro Flow Chatter V2 - 私聊特化的心流聊天器
    
    核心设计：
    - Chatter 只负责 "收到消息 → 规划执行" 的流程
    - 无论 Session 之前是什么状态，流程都一样
    - 区别只体现在提示词中
    
    不负责：
    - 等待超时处理（由 ProactiveThinker 负责）
    - 连续思考（由 ProactiveThinker 负责）
    - 主动发起对话（由 ProactiveThinker 负责）
    """
    
    chatter_name: str = "KokoroFlowChatterV2"
    chatter_description: str = "心流聊天器 V2 - 私聊特化的深度情感交互处理器"
    chat_types: ClassVar[list[ChatType]] = [ChatType.PRIVATE]
    
    def __init__(
        self,
        stream_id: str,
        action_manager: "ChatterActionManager",
        plugin_config: dict | None = None,
    ):
        super().__init__(stream_id, action_manager, plugin_config)
        
        # 核心组件
        self.session_manager = get_session_manager()
        self.action_executor = ActionExecutor(stream_id)
        
        # 并发控制
        self._lock = asyncio.Lock()
        self._processing = False
        
        # 统计
        self._stats = {
            "messages_processed": 0,
            "successful_responses": 0,
            "failed_responses": 0,
        }
        
        logger.info(f"{SOFT_PURPLE}[KFC V2]{RESET} 初始化完成: stream_id={stream_id}")
    
    async def execute(self, context: StreamContext) -> dict:
        """
        执行聊天处理
        
        流程：
        1. 获取 Session
        2. 获取未读消息
        3. 记录用户消息到 mental_log
        4. 确定 situation_type（根据之前的等待状态）
        5. 调用 Replyer 生成响应
        6. 执行动作
        7. 更新 Session（记录 Bot 规划，设置等待状态）
        8. 保存 Session
        """
        async with self._lock:
            self._processing = True
            
            try:
                # 1. 获取未读消息
                unread_messages = context.get_unread_messages()
                if not unread_messages:
                    return self._build_result(success=True, message="no_unread_messages")
                
                # 2. 取最后一条消息作为主消息
                target_message = unread_messages[-1]
                user_info = target_message.user_info
                
                if not user_info:
                    return self._build_result(success=False, message="no_user_info")
                
                user_id = str(user_info.user_id)
                user_name = user_info.user_nickname or user_id
                
                # 3. 获取或创建 Session
                session = await self.session_manager.get_session(user_id, self.stream_id)
                
                # 4. 确定 situation_type（根据之前的等待状态）
                situation_type = self._determine_situation_type(session)
                
                # 5. 记录用户消息到 mental_log
                for msg in unread_messages:
                    msg_content = msg.processed_plain_text or msg.display_message or ""
                    msg_user_name = msg.user_info.user_nickname if msg.user_info else user_name
                    msg_user_id = str(msg.user_info.user_id) if msg.user_info else user_id
                    
                    session.add_user_message(
                        content=msg_content,
                        user_name=msg_user_name,
                        user_id=msg_user_id,
                        timestamp=msg.time,
                    )
                
                # 6. 加载可用动作
                await self.action_executor.load_actions()
                available_actions = self.action_executor.get_available_actions()
                
                # 7. 获取聊天流
                chat_stream = await self._get_chat_stream()
                
                # 8. 调用 Replyer 生成响应
                response = await generate_response(
                    session=session,
                    user_name=user_name,
                    situation_type=situation_type,
                    chat_stream=chat_stream,
                    available_actions=available_actions,
                )
                
                # 9. 执行动作
                exec_result = await self.action_executor.execute(response, chat_stream)
                
                # 10. 记录 Bot 规划到 mental_log
                session.add_bot_planning(
                    thought=response.thought,
                    actions=[a.to_dict() for a in response.actions],
                    expected_reaction=response.expected_reaction,
                    max_wait_seconds=response.max_wait_seconds,
                )
                
                # 11. 更新 Session 状态
                if response.max_wait_seconds > 0:
                    session.start_waiting(
                        expected_reaction=response.expected_reaction,
                        max_wait_seconds=response.max_wait_seconds,
                    )
                else:
                    session.end_waiting()
                
                # 12. 标记消息为已读
                for msg in unread_messages:
                    context.mark_message_as_read(str(msg.message_id))
                
                # 13. 保存 Session
                await self.session_manager.save_session(user_id)
                
                # 14. 更新统计
                self._stats["messages_processed"] += len(unread_messages)
                if exec_result.get("has_reply"):
                    self._stats["successful_responses"] += 1
                
                logger.info(
                    f"{SOFT_PURPLE}[KFC V2]{RESET} 处理完成: "
                    f"user={user_name}, situation={situation_type}, "
                    f"actions={[a.type for a in response.actions]}, "
                    f"wait={response.max_wait_seconds}s"
                )
                
                return self._build_result(
                    success=True,
                    message="processed",
                    has_reply=exec_result.get("has_reply", False),
                    thought=response.thought,
                    situation_type=situation_type,
                )
                
            except Exception as e:
                self._stats["failed_responses"] += 1
                logger.error(f"[KFC V2] 处理失败: {e}")
                import traceback
                traceback.print_exc()
                return self._build_result(success=False, message=str(e), error=True)
            
            finally:
                self._processing = False
    
    def _determine_situation_type(self, session) -> str:
        """
        确定当前情况类型
        
        根据 Session 之前的状态决定提示词的 situation_type
        """
        if session.status == SessionStatus.WAITING:
            # 之前在等待
            if session.waiting_config.is_timeout():
                # 超时了才收到回复
                return "reply_late"
            else:
                # 在预期内收到回复
                return "reply_in_time"
        else:
            # 之前是 IDLE
            return "new_message"
    
    async def _get_chat_stream(self):
        """获取聊天流对象"""
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager
            
            chat_manager = get_chat_manager()
            if chat_manager:
                return await chat_manager.get_stream(self.stream_id)
        except Exception as e:
            logger.warning(f"[KFC V2] 获取 chat_stream 失败: {e}")
        return None
    
    def _build_result(
        self,
        success: bool,
        message: str = "",
        error: bool = False,
        **kwargs,
    ) -> dict:
        """构建返回结果"""
        result = {
            "success": success,
            "stream_id": self.stream_id,
            "message": message,
            "error": error,
            "timestamp": time.time(),
        }
        result.update(kwargs)
        return result
    
    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            **self._stats,
            "action_executor_stats": self.action_executor.get_stats(),
        }
    
    @property
    def is_processing(self) -> bool:
        """是否正在处理"""
        return self._processing
