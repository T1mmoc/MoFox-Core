"""
权限系统示例插件

演示如何在插件中使用权限系统，包括权限节点注册、权限检查等功能。
"""

from typing import List

from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.apis.logging_api import get_logger
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.utils.permission_decorators import require_permission, require_master, PermissionChecker
from src.common.message import ChatStream, Message


logger = get_logger(__name__)


class ExampleAdminCommand(BaseCommand):
    """需要管理员权限的示例命令"""
    

    command_name = "admin_example"
    command_description = "管理员权限示例命令"
    command_pattern = r"^/admin_example$"
    command_help = "管理员权限示例命令"
    command_examples = ["/admin_example"]
    intercept_message = True

    def can_execute(self, message: Message, chat_stream: ChatStream) -> bool:
        """基本检查"""
        return True
    
    @require_permission("plugin.example.admin")
    async def execute(self, message: Message, chat_stream: ChatStream, args: List[str]) -> None:
        """执行管理员命令"""
        await self.send_text("✅ 你有管理员权限！这是一个管理员专用功能。")
        return True, "执行成功", True


class ExampleUserCommand(BaseCommand):
    """普通用户权限的示例命令"""
    command_name = "user_example"
    command_description = "用户权限示例命令"
    command_pattern = r"^/user_example$"
    command_help = "用户权限示例命令"
    command_examples = ["/user_example"]
    intercept_message = True

    def can_execute(self, message: Message, chat_stream: ChatStream) -> bool:
        """基本检查"""
        return True
    
    @require_permission("plugin.example.user")
    async def execute(self, message: Message, chat_stream: ChatStream, args: List[str]) -> None:
        """执行用户命令"""
        await self.send_text("✅ 你有用户权限！这是一个普通用户功能。")


class ExampleMasterCommand(BaseCommand):
    """Master专用的示例命令"""

    command_name = "master_example"
    command_description = "Master专用示例命令"
    command_pattern = r"^/master_example$"
    command_help = "Master专用示例命令"
    command_examples = ["/master_example"]
    intercept_message = True

    def can_execute(self, message: Message, chat_stream: ChatStream) -> bool:
        """基本检查"""
        return True
    
    @require_master()
    async def execute(self, message: Message, chat_stream: ChatStream, args: List[str]) -> None:
        """执行Master命令"""
        await self.send_text("👑 你是Master用户！这是Master专用功能。")

@register_plugin
class HelloWorldPlugin(BasePlugin):
    """权限系统示例插件"""

    # 插件基本信息
    plugin_name: str = "permission_example"  # 内部标识符
    enable_plugin: bool = True
    dependencies: List[str] = []  # 插件依赖列表
    python_dependencies: List[str] = []  # Python包依赖列表

    config_file_name: str = "config.toml"  # 配置文件名


    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="permission_example", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
        }
    }

    def get_plugin_components(self):
        return [(ExampleAdminCommand.get_command_info,ExampleAdminCommand),
                (ExampleUserCommand.get_command_info,ExampleUserCommand),
                (ExampleMasterCommand.get_command_info,ExampleMasterCommand)
               ]