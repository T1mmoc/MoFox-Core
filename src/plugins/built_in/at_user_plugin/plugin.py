from typing import List, Tuple, Type
from src.plugin_system import (
    BasePlugin,
    BaseCommand,
    CommandInfo,
    register_plugin,
    BaseAction,
    ActionInfo,
    ActionActivationType,
)
from src.person_info.person_info import get_person_info_manager
from src.common.logger import get_logger
from src.plugin_system.base.component_types import ChatType

logger = get_logger(__name__)


class AtAction(BaseAction):
    """发送艾特消息"""

    # === 基本信息（必须填写）===
    action_name = "at_user"
    action_description = "发送艾特消息"
    activation_type = ActionActivationType.LLM_JUDGE  # 消息接收时激活(?)
    parallel_action = False
    chat_type_allow = ChatType.GROUP

    # === 功能描述（必须填写）===
    action_parameters = {"user_name": "需要艾特用户的名字", "at_message": "艾特用户时要发送的消,注意消息里不要有@"}
    action_require = [
        "当需要艾特某个用户时使用",
        "当你需要提醒特定用户查看消息时使用",
        "在回复中需要明确指向某个用户时使用",
    ]
    llm_judge_prompt = """
    判定是否需要使用艾特用户动作的条件：
    1. 你在对话中提到了某个具体的人，并且需要提醒他/她。
    3. 上下文明确需要你艾特一个或多个人。

    请回答"是"或"否"。
    """
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        """执行艾特用户的动作"""
        user_name = self.action_data.get("user_name")
        at_message = self.action_data.get("at_message")

        if not user_name or not at_message:
            logger.warning("艾特用户的动作缺少必要参数。")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"执行了艾特用户动作：艾特用户 {user_name} 并发送消息: {at_message},失败了,因为没有提供必要参数",
                action_done=False,
            )
            return False, "缺少必要参数"

        user_info = await get_person_info_manager().get_person_info_by_name(user_name)
        if not user_info or not user_info.get("user_id"):
            logger.info(f"找不到名为 '{user_name}' 的用户。")
            return False, "用户不存在"

        try:
            # 使用回复器生成艾特回复，而不是直接发送命令
            from src.chat.replyer.default_generator import DefaultReplyer
            from src.chat.message_receive.chat_stream import get_chat_manager
            
            # 获取当前聊天流
            chat_manager = get_chat_manager()
            chat_stream = chat_manager.get_stream(self.chat_id)
            
            if not chat_stream:
                logger.error(f"找不到聊天流: {self.stream_id}")
                return False, "聊天流不存在"
            
            # 创建回复器实例
            replyer = DefaultReplyer(chat_stream)
            
            # 构建回复对象，将艾特消息作为回复目标
            reply_to = f"{user_name}:{at_message}"
            extra_info = f"你需要艾特用户 {user_name} 并回复他们说: {at_message}"
            
            from src.plugin_system.core.event_manager import event_manager
            from src.plugin_system import EventType
            # 触发post_llm
            result = await event_manager.trigger_event(EventType.POST_LLM, permission_group="SYSTEM")
            if not result.all_continue_process():
                return False, f"被组件{result.get_summary().get("stopped_handlers","")}打断"
            
            # 使用回复器生成回复
            success, llm_response, prompt = await replyer.generate_reply_with_context(
                reply_to=reply_to,
                extra_info=extra_info,
                enable_tool=False,  # 艾特回复通常不需要工具调用
                from_plugin=True    # 标识来自插件
            )
            
            if success and llm_response:
                # 获取生成的回复内容
                reply_content = llm_response.get("content", "")
                if reply_content:
                    # 获取用户QQ号，发送真正的艾特消息
                    user_id = user_info.get("user_id")
                    
                    # 发送真正的艾特命令，使用回复器生成的智能内容
                    await self.send_command(
                        "SEND_AT_MESSAGE",
                        args={"qq_id": user_id, "text": reply_content},
                        display_message=f"艾特用户 {user_name} 并发送智能回复: {reply_content}",
                    )
                    
                    await self.store_action_info(
                        action_build_into_prompt=True,
                        action_prompt_display=f"执行了艾特用户动作：艾特用户 {user_name} 并发送智能回复: {reply_content}",
                        action_done=True,
                    )
                    
                    logger.info(f"成功通过回复器生成智能内容并发送真正的艾特消息给 {user_name}: {reply_content}")
                    return True, "智能艾特消息发送成功"
                else:
                    logger.warning("回复器生成了空内容")
                    return False, "回复内容为空"
            else:
                logger.error("回复器生成回复失败")
                return False, "回复生成失败"
                
        except Exception as e:
            logger.error(f"执行艾特用户动作时发生异常: {e}", exc_info=True)
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"执行艾特用户动作失败：{str(e)}",
                action_done=False,
            )
            return False, f"执行失败: {str(e)}"


class AtCommand(BaseCommand):
    command_name: str = "at_user"
    description: str = "通过名字艾特用户"
    command_pattern: str = r"/at\s+@?(?P<name>[\S]+)(?:\s+(?P<text>.*))?"

    async def execute(self) -> Tuple[bool, str, bool]:
        name = self.matched_groups.get("name")
        text = self.matched_groups.get("text", "")

        if not name:
            await self.send_text("请指定要艾特的用户名称。")
            return False, "缺少用户名称", True

        person_info_manager = get_person_info_manager()
        user_info = await person_info_manager.get_person_info_by_name(name)

        if not user_info or not user_info.get("user_id"):
            await self.send_text(f"找不到名为 '{name}' 的用户。")
            return False, "用户不存在", True

        user_id = user_info.get("user_id")

        await self.send_command(
            "SEND_AT_MESSAGE",
            args={"qq_id": user_id, "text": text},
            display_message=f"艾特用户 {name} 并发送消息: {text}",
        )

        return True, "艾特消息已发送", True


@register_plugin
class AtUserPlugin(BasePlugin):
    plugin_name: str = "at_user_plugin"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = "config.toml"
    config_schema: dict = {}

    def get_plugin_components(self) -> List[Tuple[CommandInfo | ActionInfo, Type[BaseCommand] | Type[BaseAction]]]:
        return [
            (AtAction.get_action_info(), AtAction),
        ]
