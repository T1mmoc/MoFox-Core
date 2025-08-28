"""
Echo 示例插件

展示增强命令系统的使用方法
"""

from typing import List, Tuple, Type, Optional, Union
from src.plugin_system import (
    BasePlugin,
    PlusCommand,
    CommandArgs,
    PlusCommandInfo,
    ConfigField,
    ChatType,
    register_plugin,
)
from src.plugin_system.base.component_types import PythonDependency


class EchoCommand(PlusCommand):
    """Echo命令示例"""
    
    command_name = "echo"
    command_description = "回显命令"
    command_aliases = ["say", "repeat"]
    priority = 5
    chat_type_allow = ChatType.ALL
    intercept_message = True

    async def execute(self, args: CommandArgs) -> Tuple[bool, Optional[str], bool]:
        """执行echo命令"""
        if args.is_empty():
            await self.send_text("❓ 请提供要回显的内容\n用法: /echo <内容>")
            return True, "参数不足", True
        
        content = args.get_raw()
        
        # 检查内容长度限制
        max_length = self.get_config("commands.max_content_length", 500)
        if len(content) > max_length:
            await self.send_text(f"❌ 内容过长，最大允许 {max_length} 字符")
            return True, "内容过长", True
        
        await self.send_text(f"🔊 {content}")
        
        return True, "Echo命令执行成功", True


class HelloCommand(PlusCommand):
    """Hello命令示例"""
    
    command_name = "hello"
    command_description = "问候命令"
    command_aliases = ["hi", "greet"]
    priority = 3
    chat_type_allow = ChatType.ALL
    intercept_message = True

    async def execute(self, args: CommandArgs) -> Tuple[bool, Optional[str], bool]:
        """执行hello命令"""
        if args.is_empty():
            await self.send_text("👋 Hello! 很高兴见到你！")
        else:
            name = args.get_first()
            await self.send_text(f"👋 Hello, {name}! 很高兴见到你！")
        
        return True, "Hello命令执行成功", True


class InfoCommand(PlusCommand):
    """信息命令示例"""
    
    command_name = "info"
    command_description = "显示插件信息"
    command_aliases = ["about"]
    priority = 1
    chat_type_allow = ChatType.ALL
    intercept_message = True

    async def execute(self, args: CommandArgs) -> Tuple[bool, Optional[str], bool]:
        """执行info命令"""
        info_text = (
            "📋 Echo 示例插件信息\n"
            "版本: 1.0.0\n"
            "作者: MaiBot Team\n"
            "描述: 展示增强命令系统的使用方法\n\n"
            "🎯 可用命令:\n"
            "• /echo|/say|/repeat <内容> - 回显内容\n"
            "• /hello|/hi|/greet [名字] - 问候\n"
            "• /info|/about - 显示此信息\n"
            "• /test <子命令> [参数] - 测试各种功能"
        )
        await self.send_text(info_text)
        
        return True, "Info命令执行成功", True


class TestCommand(PlusCommand):
    """测试命令示例，展示参数解析功能"""
    
    command_name = "test"
    command_description = "测试命令，展示参数解析功能"
    command_aliases = ["t"]
    priority = 2
    chat_type_allow = ChatType.ALL
    intercept_message = True

    async def execute(self, args: CommandArgs) -> Tuple[bool, Optional[str], bool]:
        """执行test命令"""
        if args.is_empty():
            help_text = (
                "🧪 测试命令帮助\n"
                "用法: /test <子命令> [参数]\n\n"
                "可用子命令:\n"
                "• args - 显示参数解析结果\n"
                "• flags - 测试标志参数\n"
                "• count - 计算参数数量\n"
                "• join - 连接所有参数"
            )
            await self.send_text(help_text)
            return True, "显示帮助", True
        
        subcommand = args.get_first().lower()
        
        if subcommand == "args":
            result = (
                f"🔍 参数解析结果:\n"
                f"原始字符串: '{args.get_raw()}'\n"
                f"解析后参数: {args.get_args()}\n"
                f"参数数量: {args.count()}\n"
                f"第一个参数: '{args.get_first()}'\n"
                f"剩余参数: '{args.get_remaining()}'"
            )
            await self.send_text(result)
            
        elif subcommand == "flags":
            result = (
                f"🏴 标志测试结果:\n"
                f"包含 --verbose: {args.has_flag('--verbose')}\n"
                f"包含 -v: {args.has_flag('-v')}\n"
                f"--output 的值: '{args.get_flag_value('--output', '未设置')}'\n"
                f"--name 的值: '{args.get_flag_value('--name', '未设置')}'"
            )
            await self.send_text(result)
            
        elif subcommand == "count":
            count = args.count() - 1  # 减去子命令本身
            await self.send_text(f"📊 除子命令外的参数数量: {count}")
            
        elif subcommand == "join":
            remaining = args.get_remaining()
            if remaining:
                await self.send_text(f"🔗 连接结果: {remaining}")
            else:
                await self.send_text("❌ 没有可连接的参数")
                
        else:
            await self.send_text(f"❓ 未知的子命令: {subcommand}")
        
        return True, "Test命令执行成功", True


@register_plugin
class EchoExamplePlugin(BasePlugin):
    """Echo 示例插件"""
    
    plugin_name: str = "echo_example_plugin"
    enable_plugin: bool = True
    dependencies: List[str] = []
    python_dependencies: List[Union[str, "PythonDependency"]] = []
    config_file_name: str = "config.toml"
    
    config_schema = {
        "plugin": {
            "enabled": ConfigField(bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(str, default="1.0.0", description="配置文件版本"),
        },
        "commands": {
            "echo_enabled": ConfigField(bool, default=True, description="是否启用 Echo 命令"),
            "cooldown": ConfigField(int, default=0, description="命令冷却时间（秒）"),
            "max_content_length": ConfigField(int, default=500, description="最大回显内容长度"),
        },
    }
    
    config_section_descriptions = {
        "plugin": "插件基本配置",
        "commands": "命令相关配置",
    }

    def get_plugin_components(self) -> List[Tuple[PlusCommandInfo, Type]]:
        """获取插件组件"""
        components = []
        
        if self.get_config("plugin.enabled", True):
            # 添加所有命令，直接使用PlusCommand类
            if self.get_config("commands.echo_enabled", True):
                components.append((EchoCommand.get_plus_command_info(), EchoCommand))
            
            components.append((HelloCommand.get_plus_command_info(), HelloCommand))
            components.append((InfoCommand.get_plus_command_info(), InfoCommand))
            components.append((TestCommand.get_plus_command_info(), TestCommand))
        
        return components
