"""
Kokoro Flow Chatter V2 - 动作执行器

负责执行 LLM 决策的动作
"""

import asyncio
import time
from typing import TYPE_CHECKING, Any, Optional

from src.chat.planner_actions.action_manager import ChatterActionManager
from src.common.logger import get_logger
from src.plugin_system.apis import send_api

from .models import ActionModel, LLMResponse

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream

logger = get_logger("kfc_v2_action_executor")


class ActionExecutor:
    """
    动作执行器
    
    职责：
    - 执行 reply、poke_user 等动作
    - 通过 ActionManager 执行动态注册的动作
    """
    
    # 内置动作（不通过 ActionManager）
    BUILTIN_ACTIONS = {"reply", "do_nothing"}
    
    def __init__(self, stream_id: str):
        self.stream_id = stream_id
        self._action_manager = ChatterActionManager()
        self._available_actions: dict = {}
        
        # 统计
        self._stats = {
            "total_executed": 0,
            "successful": 0,
            "failed": 0,
        }
    
    async def load_actions(self) -> dict:
        """加载可用动作"""
        await self._action_manager.load_actions(self.stream_id)
        self._available_actions = self._action_manager.get_using_actions()
        logger.debug(f"[ActionExecutor] 加载了 {len(self._available_actions)} 个动作")
        return self._available_actions
    
    def get_available_actions(self) -> dict:
        """获取可用动作"""
        return self._available_actions.copy()
    
    async def execute(
        self,
        response: LLMResponse,
        chat_stream: Optional["ChatStream"],
    ) -> dict[str, Any]:
        """
        执行动作列表
        
        Args:
            response: LLM 响应
            chat_stream: 聊天流
            
        Returns:
            执行结果
        """
        results = []
        has_reply = False
        reply_content = ""
        
        for action in response.actions:
            try:
                result = await self._execute_action(action, chat_stream)
                results.append(result)
                
                if result.get("success"):
                    self._stats["successful"] += 1
                    if action.type in ("reply", "respond"):
                        has_reply = True
                        reply_content = action.params.get("content", "")
                else:
                    self._stats["failed"] += 1
                    
            except Exception as e:
                logger.error(f"[ActionExecutor] 执行动作失败 {action.type}: {e}")
                results.append({
                    "action_type": action.type,
                    "success": False,
                    "error": str(e),
                })
                self._stats["failed"] += 1
            
            self._stats["total_executed"] += 1
        
        return {
            "success": all(r.get("success", False) for r in results),
            "results": results,
            "has_reply": has_reply,
            "reply_content": reply_content,
        }
    
    async def _execute_action(
        self,
        action: ActionModel,
        chat_stream: Optional["ChatStream"],
    ) -> dict[str, Any]:
        """执行单个动作"""
        action_type = action.type
        
        if action_type == "reply":
            return await self._execute_reply(action, chat_stream)
        
        elif action_type == "do_nothing":
            logger.debug("[ActionExecutor] 执行 do_nothing")
            return {"action_type": "do_nothing", "success": True}
        
        elif action_type == "poke_user":
            return await self._execute_via_manager(action, chat_stream)
        
        elif action_type in self._available_actions:
            return await self._execute_via_manager(action, chat_stream)
        
        else:
            logger.warning(f"[ActionExecutor] 未知动作类型: {action_type}")
            return {
                "action_type": action_type,
                "success": False,
                "error": f"未知动作类型: {action_type}",
            }
    
    async def _execute_reply(
        self,
        action: ActionModel,
        chat_stream: Optional["ChatStream"],
    ) -> dict[str, Any]:
        """执行回复动作"""
        content = action.params.get("content", "")
        
        if not content:
            return {
                "action_type": "reply",
                "success": False,
                "error": "回复内容为空",
            }
        
        try:
            # 消息后处理（分割、错别字等）
            processed_messages = await self._process_reply_content(content)
            
            all_success = True
            for msg in processed_messages:
                success = await send_api.text_to_stream(
                    text=msg,
                    stream_id=self.stream_id,
                    typing=True,
                )
                if not success:
                    all_success = False
            
            return {
                "action_type": "reply",
                "success": all_success,
                "reply_text": content,
            }
            
        except Exception as e:
            logger.error(f"[ActionExecutor] 发送回复失败: {e}")
            return {
                "action_type": "reply",
                "success": False,
                "error": str(e),
            }
    
    async def _process_reply_content(self, content: str) -> list[str]:
        """处理回复内容（分割、错别字等）"""
        try:
            # 复用 v1 的后处理器
            from src.plugins.built_in.kokoro_flow_chatter.response_post_processor import (
                process_reply_content,
            )
            
            messages = await process_reply_content(content)
            return messages if messages else [content]
            
        except Exception as e:
            logger.warning(f"[ActionExecutor] 消息处理失败，使用原始内容: {e}")
            return [content]
    
    async def _execute_via_manager(
        self,
        action: ActionModel,
        chat_stream: Optional["ChatStream"],
    ) -> dict[str, Any]:
        """通过 ActionManager 执行动作"""
        try:
            result = await self._action_manager.execute_action(
                action_name=action.type,
                chat_id=self.stream_id,
                target_message=None,
                reasoning=f"KFC决策: {action.type}",
                action_data=action.params,
                thinking_id=None,
                log_prefix="[KFC V2]",
            )
            
            return {
                "action_type": action.type,
                "success": result.get("success", False),
                "result": result,
            }
            
        except Exception as e:
            logger.error(f"[ActionExecutor] ActionManager 执行失败: {e}")
            return {
                "action_type": action.type,
                "success": False,
                "error": str(e),
            }
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return self._stats.copy()
