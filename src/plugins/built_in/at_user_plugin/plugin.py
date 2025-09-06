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
from src.plugin_system.base.component_types import ChatType,PlannerType

logger = get_logger(__name__)


class AtAction(BaseAction):
    """发送艾特消息"""

    # === 基本信息（必须填写）===
    action_name = "at_user"
    action_description = "发送艾特消息"
    activation_type = ActionActivationType.LLM_JUDGE  # 消息接收时激活(?)
    parallel_action = False
    chat_type_allow = ChatType.GROUP
    planner_type = PlannerType.BIG_BRAIN

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
        await self.send_command(
            "SEND_AT_MESSAGE",
            args={"qq_id": user_info.get("user_id"), "text": at_message},
            display_message=f"艾特用户 {user_name} 并发送消息: {at_message}",
        )
        await self.store_action_info(
            action_build_into_prompt=True,
            action_prompt_display=f"执行了艾特用户动作：艾特用户 {user_name} 并发送消息: {at_message}",
            action_done=True,
        )

        logger.info("艾特用户的动作已触发，但具体实现待完成。")
        return True, "艾特用户的动作已触发，但具体实现待完成。"


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
