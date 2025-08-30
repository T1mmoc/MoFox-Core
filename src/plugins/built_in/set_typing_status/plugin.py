from typing import List, Tuple, Type
import logging

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    ComponentInfo,
    BaseEventHandler,
    EventType,
)
from src.plugin_system.base.base_event import HandlerResult
from src.plugin_system.apis import send_api

logger = logging.getLogger(__name__)


class SetTypingStatusHandler(BaseEventHandler):
    """在LLM处理私聊消息后设置“正在输入”状态的事件处理器。"""

    handler_name = "set_typing_status_handler"
    handler_description = "在LLM生成回复后，将用户的聊天状态设置为“正在输入”。"
    init_subscribe = [EventType.POST_LLM]

    async def execute(self, params: dict) -> HandlerResult:
        message = params.get("message")
        if not message or not message.is_private_message:
            return HandlerResult(success=True, continue_process=True)

        user_id = message.message_info.user_info.user_id
        if not user_id:
            return HandlerResult(success=False, continue_process=True, message="无法获取用户ID")

        try:
            params =  {"user_id": user_id,"event_type": 1}
            await send_api.adapter_command_to_stream(
                action="set_input_status",
                params=params,
                stream_id=message.stream_id,
            )
            logger.debug(f"成功为用户 {user_id} 设置“正在输入”状态。")
            return HandlerResult(success=True, continue_process=True)
        except Exception as e:
            logger.error(f"为用户 {user_id} 设置“正在输入”状态时出错: {e}")
            return HandlerResult(success=False, continue_process=True, message=str(e))


@register_plugin
class SetTypingStatusPlugin(BasePlugin):
    """一个在LLM生成回复时设置私聊输入状态的插件。"""

    plugin_name = "set_typing_status"
    enable_plugin = True
    dependencies = []
    python_dependencies = []
    config_file_name = ""
    
    config_schema = {}

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """注册插件的功能组件。"""
        return [(SetTypingStatusHandler.get_handler_info(), SetTypingStatusHandler)]
    
    def register_plugin(self) -> bool:
        return True
