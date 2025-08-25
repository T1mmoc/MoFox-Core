"""
权限管理插件

提供权限系统的管理命令，包括权限授权、撤销、查询等功能。
"""

import re
from typing import List, Optional, Tuple, Type

from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.apis.permission_api import permission_api
from src.plugin_system.apis.logging_api import get_logger
from src.plugin_system.base.component_types import CommandInfo
from src.plugin_system.base.config_types import ConfigField


logger = get_logger("Permission")


class PermissionCommand(BaseCommand):
    """权限管理命令"""
    
    command_name = "permission"
    command_description = "权限管理命令"
    command_pattern = r"^/permission(?:\s|$)"
    command_help = "/permission <子命令> [参数...]"
    intercept_message = True
        
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 注册权限节点
        permission_api.register_permission_node(
            "plugin.permission.manage",
            "权限管理：可以授权和撤销其他用户的权限",
            "permission_manager",
            False
        )
        permission_api.register_permission_node(
            "plugin.permission.view",
            "权限查看：可以查看权限节点和用户权限信息",
            "permission_manager",
            True
        )
    
    def can_execute(self) -> bool:
        """检查命令是否可以执行"""
        # 基本权限检查由权限系统处理
        return True
    
    async def execute(self, args: List[str]) -> None:
        """执行权限管理命令"""
        if not args:
            await self._show_help()
            return
        
        subcommand = args[0].lower()
        remaining_args = args[1:]
        chat_stream = self.message.chat_stream
        # 检查基本查看权限
        can_view = permission_api.check_permission(
            chat_stream.platform,
            chat_stream.user_info.user_id,
            "plugin.permission.view"
        ) or permission_api.is_master(chat_stream.platform, chat_stream.user_info.user_id)

        # 检查管理权限
        can_manage = permission_api.check_permission(
            chat_stream.platform, 
            chat_stream.user_info.user_id, 
            "plugin.permission.manage"
        ) or permission_api.is_master(chat_stream.platform, chat_stream.user_info.user_id)
        
        if subcommand in ["grant", "授权", "give"]:
            if not can_manage:
                await self.send_text("❌ 你没有权限管理的权限")
                return
            await self._grant_permission(chat_stream, remaining_args)
            
        elif subcommand in ["revoke", "撤销", "remove"]:
            if not can_manage:
                await self.send_text("❌ 你没有权限管理的权限")
                return
            await self._revoke_permission(chat_stream, remaining_args)
            
        elif subcommand in ["list", "列表", "ls"]:
            if not can_view:
                await self.send_text("❌ 你没有查看权限的权限")
                return
            await self._list_permissions(chat_stream, remaining_args)
            
        elif subcommand in ["check", "检查"]:
            if not can_view:
                await self.send_text("❌ 你没有查看权限的权限")
                return
            await self._check_permission(chat_stream, remaining_args)
            
        elif subcommand in ["nodes", "节点"]:
            if not can_view:
                await self.send_text("❌ 你没有查看权限的权限")
                return
            await self._list_nodes(chat_stream, remaining_args)
            
        elif subcommand in ["help", "帮助"]:
            await self._show_help(chat_stream)
            
        else:
            await self.send_text(f"❌ 未知的子命令: {subcommand}\n使用 /permission help 查看帮助")

    async def _show_help(self):
        """显示帮助信息"""
        help_text = """📋 权限管理命令帮助

🔐 管理命令（需要管理权限）：
• /permission grant <@用户|QQ号> <权限节点> - 授权用户权限
• /permission revoke <@用户|QQ号> <权限节点> - 撤销用户权限

👀 查看命令（需要查看权限）：
• /permission list [用户] - 查看用户权限列表
• /permission check <@用户|QQ号> <权限节点> - 检查用户是否拥有权限
• /permission nodes [插件名] - 查看权限节点列表

❓ 其他：
• /permission help - 显示此帮助

📝 示例：
• /permission grant @张三 plugin.example.command
• /permission list 123456789
• /permission nodes example_plugin"""
        
        await self.send_text(help_text)
    
    def _parse_user_mention(self, mention: str) -> Optional[str]:
        """解析用户提及，提取QQ号"""
        # 匹配 @用户 格式，提取QQ号
        at_match = re.search(r'\[CQ:at,qq=(\d+)\]', mention)
        if at_match:
            return at_match.group(1)
        
        # 直接是数字
        if mention.isdigit():
            return mention
        
        return None

    async def _grant_permission(self, chat_stream , args: List[str]):
        """授权用户权限"""
        if len(args) < 2:
            await self.send_text("❌ 用法: /permission grant <@用户|QQ号> <权限节点>")
            return
        
        user_mention = args[0]
        permission_node = args[1]
        
        # 解析用户ID
        user_id = self._parse_user_mention(user_mention)
        if not user_id:
            await self.send_text("❌ 无效的用户格式，请使用 @用户 或直接输入QQ号")
            return
        
        # 执行授权
        success = permission_api.grant_permission(chat_stream.platform, user_id, permission_node)
        
        if success:
            await self.send_text(f"✅ 已授权用户 {user_id} 权限节点 {permission_node}")
        else:
            await self.send_text("❌ 授权失败，请检查权限节点是否存在")

    async def _revoke_permission(self, chat_stream, args: List[str]):
        """撤销用户权限"""
        if len(args) < 2:
            await self.send_text("❌ 用法: /permission revoke <@用户|QQ号> <权限节点>")
            return
        
        user_mention = args[0]
        permission_node = args[1]
        
        # 解析用户ID
        user_id = self._parse_user_mention(user_mention)
        if not user_id:
            await self.send_text("❌ 无效的用户格式，请使用 @用户 或直接输入QQ号")
            return
        
        # 执行撤销
        success = permission_api.revoke_permission(chat_stream.platform, user_id, permission_node)
        
        if success:
            await self.send_text(f"✅ 已撤销用户 {user_id} 权限节点 {permission_node}")
        else:
            await self.send_text("❌ 撤销失败，请检查权限节点是否存在")
    
    async def _list_permissions(self, chat_stream, args: List[str]):
        """列出用户权限"""
        target_user_id = None
        
        if args:
            # 指定了用户
            user_mention = args[0]
            target_user_id = self._parse_user_mention(user_mention)
            if not target_user_id:
                await self.send_text("❌ 无效的用户格式，请使用 @用户 或直接输入QQ号")
                return
        else:
            # 查看自己的权限
            target_user_id = chat_stream.user_info.user_id
        
        # 检查是否为Master用户
        is_master = permission_api.is_master(chat_stream.platform, target_user_id)
        
        # 获取用户权限
        permissions = permission_api.get_user_permissions(chat_stream.platform, target_user_id)
        
        if is_master:
            response = f"👑 用户 {target_user_id} 是Master用户，拥有所有权限"
        else:
            if permissions:
                perm_list = "\n".join([f"• {perm}" for perm in permissions])
                response = f"📋 用户 {target_user_id} 拥有的权限：\n{perm_list}"
            else:
                response = f"📋 用户 {target_user_id} 没有任何权限"
        
        await self.send_text(response)

    async def _check_permission(self, chat_stream, args: List[str]):
        """检查用户权限"""
        if len(args) < 2:
            await self.send_text("❌ 用法: /permission check <@用户|QQ号> <权限节点>")
            return
        
        user_mention = args[0]
        permission_node = args[1]
        
        # 解析用户ID
        user_id = self._parse_user_mention(user_mention)
        if not user_id:
            await self.send_text("❌ 无效的用户格式，请使用 @用户 或直接输入QQ号")
            return
        
        # 检查权限
        has_permission = permission_api.check_permission(chat_stream.platform, user_id, permission_node)
        is_master = permission_api.is_master(chat_stream.platform, user_id)
        
        if has_permission:
            if is_master:
                response = f"✅ 用户 {user_id} 拥有权限 {permission_node}（Master用户）"
            else:
                response = f"✅ 用户 {user_id} 拥有权限 {permission_node}"
        else:
            response = f"❌ 用户 {user_id} 没有权限 {permission_node}"
        
        await self.send_text(response)

    async def _list_nodes(self, chat_stream, args: List[str]):
        """列出权限节点"""
        plugin_name = args[0] if args else None
        
        if plugin_name:
            # 获取指定插件的权限节点
            nodes = permission_api.get_plugin_permission_nodes(plugin_name)
            title = f"📋 插件 {plugin_name} 的权限节点："
        else:
            # 获取所有权限节点
            nodes = permission_api.get_all_permission_nodes()
            title = "📋 所有权限节点："
        
        if not nodes:
            if plugin_name:
                response = f"📋 插件 {plugin_name} 没有注册任何权限节点"
            else:
                response = "📋 系统中没有任何权限节点"
        else:
            node_list = []
            for node in nodes:
                default_text = "（默认授权）" if node["default_granted"] else "（默认拒绝）"
                node_list.append(f"• {node['node_name']} {default_text}")
                node_list.append(f"  📄 {node['description']}")
                if not plugin_name:
                    node_list.append(f"  🔌 插件: {node['plugin_name']}")
                node_list.append("")  # 空行分隔
            
            response = title + "\n" + "\n".join(node_list)
        
        await self.send_text(response)


@register_plugin
class PermissionManagerPlugin(BasePlugin):
    plugin_name: str = "permission_manager_plugin"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = "config.toml"
    config_schema: dict = {
        "plugin": {
            "enabled": ConfigField(bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.1.0", description="配置文件版本")
        }
    }

    def get_plugin_components(self) -> List[Tuple[CommandInfo, Type[BaseCommand]]]:
        return [(PermissionCommand.get_command_info(), PermissionCommand)]