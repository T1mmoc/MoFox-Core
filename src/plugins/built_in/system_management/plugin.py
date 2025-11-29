"""
统一系统管理插件

提供权限、插件和定时任务的统一管理命令。
"""

import json
import re
from typing import ClassVar

from src.chat.utils.prompt_component_manager import prompt_component_manager
from src.chat.utils.prompt_params import PromptParameters
from src.plugin_system.apis import (
    chat_api,
    component_state_api,
    plugin_info_api,
    plugin_manage_api,
)
from src.plugin_system.apis.logging_api import get_logger
from src.plugin_system.apis.permission_api import permission_api
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.apis.unified_scheduler import TriggerType, unified_scheduler
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.command_args import CommandArgs
from src.plugin_system.base.component_types import (
    ChatType,
    ComponentType,
    PermissionNodeField,
    PlusCommandInfo,
)
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.base.plus_command import PlusCommand
from src.plugin_system.utils.permission_decorators import require_permission
from src.plugin_system.apis.permission_api import permission_api

logger = get_logger("SystemManagement")


class SystemCommand(PlusCommand):
    """系统管理命令 - 使用PlusCommand系统"""

    command_name = "system"
    command_description = "统一系统管理命令，支持权限、插件、定时任务等管理功能"
    command_aliases: ClassVar[list[str]] = ["sys", "系统管理"]
    priority = 10
    chat_type_allow = ChatType.ALL
    intercept_message = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    async def execute(self, args: CommandArgs) -> tuple[bool, str | None, bool]:
        """执行系统管理命令"""
        if not self.chat_stream.user_info:
            logger.error("chat_stream缺失用户信息,请报告开发者")
            return False, "chat_stream缺失用户信息,请报告开发者", True
        has_permission = await permission_api.check_permission(platform=self.chat_stream.platform,user_id=self.chat_stream.user_info.user_id,permission_node="access")
        if has_permission:
            logger.warning("没有权限使用此命令")
            return False, "没有权限使用此命令", True
        if args.is_empty:
            await self._show_help("all")
            return True, "显示帮助信息", True

        subcommand = args.get_first.lower()
        remaining_args = args.get_args()[1:]

        if subcommand in ["permission", "perm", "权限"]:
            await self._handle_permission_commands(remaining_args)
        elif subcommand in ["plugin", "插件"]:
            await self._handle_plugin_commands(remaining_args)
        elif subcommand in ["schedule", "定时任务"]:
            await self._handle_schedule_commands(remaining_args)
        elif subcommand in ["help", "帮助"]:
            await self._show_help("all")
        elif subcommand in ["prompt","提示词"]:
            await self._handle_prompt_commands(remaining_args)
        else:
            await self.send_text(f"❌ 未知的子命令: {subcommand}\n使用 /system help 查看帮助")

        return True, "命令执行完成", True

    async def _show_help(self, target: str):
        """显示帮助信息"""
        help_text = "帮助文档待施工..."
        if target == "all":
            help_text = """📋 系统管理命令帮助 (`/system`)
🔧 主要功能：
• `/system help` - 显示此帮助
• `/system permission` - 权限管理
• `/system plugin` - 插件与组件管理
• `/system schedule` - 定时任务管理
• `/system prompt` - 提示词注入管理
"""
        elif target == "schedule":
            help_text = """📅 定时任务管理帮助
📋 查看命令:
• `/system schedule list` - 列出所有定时任务
• `/system schedule list <类型>` - 列出指定类型的任务 (time, event, custom)
• `/system schedule info <任务ID>` - 查看任务详情

⚙️ 控制命令:
• `/system schedule pause <任务ID>` - 暂停一个任务
• `/system schedule resume <任务ID>` - 恢复一个任务
"""
        elif target == "plugin":
            help_text = """🔌 插件管理命令帮助

⚙️ 插件控制：
• `/system plugin rescan` - 重新扫描所有插件目录
• `/system plugin load <插件名>` - 加载指定插件
• `/system plugin reload <插件名>` - 重新加载指定插件
• `/system plugin reload_all` - 重新加载所有插件

🔧 全局组件控制 (需要 `system.plugin.manage` 权限):
• `/system plugin enable <组件名>` - 全局启用组件
• `/system plugin disable <组件名>` - 全局禁用组件

🎯 局部组件控制 (需要 `system.plugin.manage.local` 权限):
• `/system plugin enable_local <名称> [group <群号> | private <QQ号>]` - 在指定会话局部启用组件
• `/system plugin disable_local <名称> [group <群号> | private <QQ号>]` - 在指定会话局部禁用组件
"""
        elif target == "permission":
            help_text = """📋 权限管理命令帮助
🔐 管理命令 (需要 `system.permission.manage` 权限):
• /system permission grant <@用户|QQ号> <权限节点> - 授权
• /system permission revoke <@用户|QQ号> <权限节点> - 撤销

👀 查看命令 (需要 `system.permission.view` 权限):
• /system permission list [用户] - 查看用户权限
• /system permission check <@用户|QQ号> <权限节点> - 检查权限
• /system permission nodes [插件名] - 查看权限节点
• /system permission allnodes - 查看所有权限节点详情
"""
        elif target == "prompt":
            help_text = """📝 提示词注入管理帮助

🔎 **查询命令** (需要 `system.prompt.view` 权限):
• `/system prompt help` - 显示此帮助
• `/system prompt map` - 查看全局注入关系图
• `/system prompt targets` - 列出所有可被注入的核心提示词
• `/system prompt components` - 列出所有已注册的提示词组件
• `/system prompt info <目标名>` - 查看特定核心提示词的详细注入情况

🔧 **调试命令** (需要 `system.prompt.view` 权限):
• `/system prompt raw <目标名>` - 查看核心提示词的原始内容
• `/system prompt component_info <组件名>` - 查看组件的详细信息和其定义的规则
• `/system prompt preview <目标名> [JSON参数]` - 预览提示词在注入后的最终效果
  (示例: `/system prompt preview core_prompt '{"input": "你好"}'`)
"""
        await self.send_text(help_text)
    # =================================================================
    # Plugin Management Section
    # =================================================================
    async def _handle_plugin_commands(self, args: list[str]):
        """处理插件管理相关命令"""
        if not args:
            await self._show_help("plugin")
            return

        action = args[0].lower()
        remaining_args = args[1:]

        if action in ["help", "帮助"]:
            await self._show_help("plugin")
        elif action in ["report", "报告"]:
            await self._show_system_report()
        elif action in ["info", "详情"] and remaining_args:
            await self._show_plugin_info(remaining_args[0])
        elif action in ["list", "列表"]:
            comp_type = remaining_args[0] if remaining_args else None
            await self._list_components(comp_type)
        elif action in ["search", "搜索"] and remaining_args:
            await self._search_components(remaining_args[0])
        elif action in ["disabled", "禁用列表"]:
            await self._list_disabled_components()
        elif action in ["rescan", "重扫"]:
            await self._rescan_plugin_dirs()
        elif action in ["load", "加载"] and len(remaining_args) > 0:
            await self._load_plugin(remaining_args[0])
        elif action in ["reload", "重载"] and len(remaining_args) > 0:
            await self._reload_plugin(remaining_args[0])
        elif action in ["reload_all", "重载全部"]:
            await self._reload_all_plugins()
        elif action in ["enable", "启用"] and len(remaining_args) >= 1:
            await self._set_global_component_state(remaining_args[0], enabled=True)
        elif action in ["disable", "禁用"] and len(remaining_args) >= 1:
            await self._set_global_component_state(remaining_args[0], enabled=False)
        elif action in ["enable_local", "局部启用"] and len(remaining_args) >= 1:
            await self._set_local_component_state(remaining_args, enabled=True)
        elif action in ["disable_local", "局部禁用"] and len(remaining_args) >= 1:
            await self._set_local_component_state(remaining_args, enabled=False)
        else:
            await self.send_text("❌ 插件管理命令不合法\n使用 /system plugin help 查看帮助")


    # =================================================================
    # Schedule Management Section
    # =================================================================
    async def _handle_schedule_commands(self, args: list[str]):
        """处理定时任务管理相关命令"""
        if not args:
            await self._show_help("schedule")
            return

        action = args[0].lower()
        remaining_args = args[1:]

        if action in ["list", "列表"]:
            target_type = remaining_args[0] if remaining_args else None
            await self._list_schedules(target_type)
        elif action in ["info", "详情"] and remaining_args:
            await self._get_schedule_info(remaining_args[0])
        elif action in ["pause", "暂停"] and remaining_args:
            await self._pause_schedule(remaining_args[0])
        elif action in ["resume", "恢复"] and remaining_args:
            await self._resume_schedule(remaining_args[0])
        else:
            await self.send_text("❌ 定时任务管理命令不合法\n使用 /system schedule help 查看帮助")

    @require_permission("schedule.view", deny_message="❌ 你没有查看定时任务的权限")
    async def _list_schedules(self, trigger_type_str: str | None):
        """列出定时任务"""
        trigger_type = None
        if trigger_type_str:
            try:
                trigger_type = TriggerType(trigger_type_str.lower())
            except ValueError:
                await self.send_text(f"❌ 无效的任务类型: {trigger_type_str}")
                return

        tasks = await unified_scheduler.list_tasks(trigger_type)
        if not tasks:
            await self.send_text("📅 当前没有定时任务")
            return

        response_parts = [f"📅 定时任务列表 (共 {len(tasks)} 个):"]
        for task in tasks:
            # 使用新的 status 字段，兼容旧版本
            is_active = task.get("status") == "PENDING" if "status" in task else task.get("is_active", False)
            status = "▶️" if is_active else "⏸️"
            recurring = "🔁" if task["is_recurring"] else "➡️"
            response_parts.append(
                f"{status} `{task['task_name']}` ({task['trigger_type']}) {recurring}\n"
                f"  ID: `{task['schedule_id'][:8]}...`"
            )
        await self.send_text("\n".join(response_parts))

    @require_permission("schedule.view", deny_message="❌ 你没有查看定时任务详情的权限")
    async def _get_schedule_info(self, schedule_id: str):
        """获取任务详情"""
        task_info = await unified_scheduler.get_task_info(schedule_id)
        if not task_info:
            await self.send_text(f"❌ 找不到ID为 `{schedule_id}` 的任务")
            return

        info_str = f"📅 任务详情: `{task_info['task_name']}`\n"
        for key, value in task_info.items():
            info_str += f"  • {key}: `{value}`\n"
        await self.send_text(info_str)

    @require_permission("schedule.manage", deny_message="❌ 你没有管理定时任务的权限")
    async def _pause_schedule(self, schedule_id: str):
        """暂停任务"""
        success = await unified_scheduler.pause_schedule(schedule_id)
        if success:
            await self.send_text(f"⏸️ 已暂停任务: `{schedule_id}`")
        else:
            await self.send_text(f"❌ 暂停任务失败: `{schedule_id}`")

    @require_permission("schedule.manage", deny_message="❌ 你没有管理定时任务的权限")
    async def _resume_schedule(self, schedule_id: str):
        """恢复任务"""
        success = await unified_scheduler.resume_schedule(schedule_id)
        if success:
            await self.send_text(f"▶️ 已恢复任务: `{schedule_id}`")
        else:
            await self.send_text(f"❌ 恢复任务失败: `{schedule_id}`")

    # =================================================================
    # Prompt Management Section
    # =================================================================
    async def _handle_prompt_commands(self, args: list[str]):
        """处理提示词管理相关命令"""
        if not args or args[0].lower() in ["help", "帮助"]:
            await self._show_help("prompt")
            return

        action = args[0].lower()
        remaining_args = args[1:]

        if action in ["map", "关系图"]:
            await self._show_injection_map()
        elif action in ["targets", "目标"]:
            await self._list_core_prompts()
        elif action in ["components", "组件"]:
            await self._list_prompt_components()
        elif action in ["info", "详情"] and remaining_args:
            await self._get_prompt_injection_info(remaining_args[0])
        elif action in ["preview", "预览"] and remaining_args:
            target_name = remaining_args[0]
            params_str = " ".join(remaining_args[1:]) if len(remaining_args) > 1 else "{}"
            await self._preview_prompt(target_name, params_str)
        elif action in ["raw", "原始内容"] and remaining_args:
            await self._show_raw_prompt(remaining_args[0])
        elif action in ["component_info", "组件信息"] and remaining_args:
            await self._show_prompt_component_info(remaining_args[0])
        else:
            await self.send_text("❌ 提示词管理命令不合法\n使用 /system prompt help 查看帮助")

    @require_permission("prompt.view", deny_message="❌ 你没有查看提示词注入信息的权限")
    async def _show_injection_map(self):
        """显示全局注入关系图"""
        injection_map = await prompt_component_manager.get_injection_info()
        if not injection_map:
            await self.send_text("📊 当前没有任何提示词注入关系")
            return

        response_parts = ["📊 全局提示词注入关系图：\n"]
        for target, injections in injection_map.items():
            if injections:
                response_parts.append(f"🎯 **{target}** (注入源):")
                for inj in injections:
                    source_tag = f"({inj['source']})" if inj["source"] != "static_default" else ""
                    response_parts.append(f"  ⎿ `{inj['name']}` (优先级: {inj['priority']}) {source_tag}")
            else:
                response_parts.append(f"🎯 **{target}** (无注入)")

        await self._send_long_message("\n".join(response_parts))

    @require_permission("prompt.view", deny_message="❌ 你没有查看提示词注入信息的权限")
    async def _list_core_prompts(self):
        """列出所有可注入的核心提示词"""
        targets = prompt_component_manager.get_core_prompts()
        if not targets:
            await self.send_text("🎯 当前没有可注入的核心提示词")
            return

        response = "🎯 所有可注入的核心提示词:\n" + "\n".join([f"• `{name}`" for name in targets])
        await self.send_text(response)

    @require_permission("prompt.view", deny_message="❌ 你没有查看提示词注入信息的权限")
    async def _list_prompt_components(self):
        """列出所有已注册的提示词组件"""
        components = await prompt_component_manager.get_registered_prompt_component_info()
        if not components:
            await self.send_text("🧩 当前没有已注册的提示词组件")
            return

        response_parts = [f"🧩 已注册的提示词组件 (共 {len(components)} 个):"]
        for comp in components:
            response_parts.append(f"• `{comp.name}` (来自: `{comp.plugin_name}`)")

        await self._send_long_message("\n".join(response_parts))


    @require_permission("prompt.view", deny_message="❌ 你没有查看提示词注入信息的权限")
    async def _get_prompt_injection_info(self, target_name: str):
        """获取特定核心提示词的注入详情"""
        injection_info = await prompt_component_manager.get_injection_info(target_prompt=target_name, detailed=True)
        injections = injection_info.get(target_name, [])

        core_prompts = prompt_component_manager.get_core_prompts()
        if target_name not in core_prompts:
            await self.send_text(f"❌ 找不到核心提示词: `{target_name}`")
            return

        if not injections:
            await self.send_text(f"🎯 核心提示词 `{target_name}` 当前没有被任何组件注入。")
            return

        response_parts = [f"🔎 **核心提示词 `{target_name}` 的注入详情:**"]
        for inj in injections:
            response_parts.append(f"  • **`{inj['name']}`** (优先级: {inj['priority']})")
            response_parts.append(f"    - **来源**: `{inj['source']}`")
            response_parts.append(f"    - **类型**: `{inj['injection_type']}`")
            target_content = inj.get("target_content")
            if target_content:
                response_parts.append(f"    - **操作目标**: `{target_content}`")
        await self.send_text("\n".join(response_parts))

    @require_permission("prompt.view", deny_message="❌ 你没有预览提示词的权限")
    async def _preview_prompt(self, target_name: str, params_str: str):
        """预览核心提示词在注入后的最终效果"""
        try:
            user_params = json.loads(params_str)
            if not isinstance(user_params, dict):
                raise ValueError("参数必须是一个JSON对象。")
        except (json.JSONDecodeError, ValueError) as e:
            await self.send_text(f"❌ 参数解析失败: {e}\n请提供有效的JSON格式参数，例如: '{{\"key\": \"value\"}}'")
            return

        params = PromptParameters(
            chat_id=self.message.chat_info.stream_id,
            is_group_chat=self.message.chat_info.group_info is not None,
            sender=self.message.user_info.user_id,
        )

        for key, value in user_params.items():
            if hasattr(params, key):
                setattr(params, key, value)

        preview_content = await prompt_component_manager.preview_prompt_injections(
            target_prompt_name=target_name, params=params
        )

        response = f"🔬 **`{target_name}`** 注入预览结果:\n" f"------------------------------------\n" f"{preview_content}"
        await self._send_long_message(response)

    @require_permission("prompt.view", deny_message="❌ 你没有查看提示词原始内容的权限")
    async def _show_raw_prompt(self, target_name: str):
        """显示核心提示词的原始内容"""
        contents = prompt_component_manager.get_core_prompt_contents(prompt_name=target_name)

        if not contents:
            await self.send_text(f"❌ 找不到核心提示词: `{target_name}`")
            return

        raw_template = contents[0][1]

        response = f"📄 **`{target_name}`** 原始内容:\n" f"------------------------------------\n" f"{raw_template}"
        await self._send_long_message(response)

    @require_permission("prompt.view", deny_message="❌ 你没有查看提示词组件信息的权限")
    async def _show_prompt_component_info(self, component_name: str):
        """显示特定提示词组件的详细信息"""
        all_components = await prompt_component_manager.get_registered_prompt_component_info()

        target_component = next((comp for comp in all_components if comp.name == component_name), None)

        if not target_component:
            await self.send_text(f"❌ 找不到提示词组件: `{component_name}`")
            return

        response_parts = [
            f"🧩 **组件详情: `{target_component.name}`**",
            f"  - **来源插件**: `{target_component.plugin_name}`",
            f"  - **描述**: {target_component.description or '无'}",
            f"  - **内置组件**: {'是' if target_component.is_built_in else '否'}",
        ]

        if target_component.injection_rules:
            response_parts.append("\n  **注入规则:**")
            for rule in target_component.injection_rules:
                response_parts.append(f"    - **目标**: `{rule.target_prompt}` (优先级: {rule.priority})")
                response_parts.append(f"      - **类型**: `{rule.injection_type.value}`")
                if rule.target_content:
                    response_parts.append(f"      - **操作目标**: `{rule.target_content}`")
        else:
            response_parts.append("\n  **注入规则**: (无)")

        await self.send_text("\n".join(response_parts))

    # =================================================================
    # Permission Management Section (Plugin Stats & Info)
    # =================================================================
    @require_permission("plugin.manage", deny_message="❌ 你没有权限查看插件详情")
    async def _show_plugin_info(self, plugin_name: str):
        """显示单个插件的详细信息"""
        details = plugin_info_api.get_plugin_details(plugin_name)

        if not details:
            # 尝试模糊匹配
            all_plugins = plugin_info_api.list_plugins("loaded")
            suggestions = [p for p in all_plugins if plugin_name.lower() in p.lower()]
            if suggestions:
                await self.send_text(
                    f"❌ 未找到插件 `{plugin_name}`\n"
                    f"你可能想找的是: {', '.join([f'`{s}`' for s in suggestions[:5]])}"
                )
            else:
                await self.send_text(f"❌ 未找到插件 `{plugin_name}`")
            return

        response_parts = [
            f"🔌 **插件详情: {details['display_name']}**",
            f"  • 内部名称: `{details['name']}`",
            f"  • 版本: {details['version']}",
            f"  • 作者: {details['author']}",
            f"  • 状态: {'✅ 已启用' if details['enabled'] else '❌ 已禁用'}",
            f"  • 加载状态: {details['status']}",
        ]

        if details.get('description'):
            response_parts.append(f"  • 描述: {details['description']}")

        if details.get('license'):
            response_parts.append(f"  • 许可证: {details['license']}")

        # 组件信息
        if details['components']:
            response_parts.append(f"\n🧩 **组件列表** (共 {len(details['components'])} 个):")
            for comp in details['components']:
                status = "✅" if comp['enabled'] else "❌"
                response_parts.append(f"  {status} `{comp['name']}` ({comp['component_type']})")
                if comp.get('description'):
                    response_parts.append(f"      {comp['description'][:50]}...")

        await self._send_long_message("\n".join(response_parts))

    @require_permission("plugin.manage", deny_message="❌ 你没有权限查看组件列表")
    async def _list_components(self, comp_type_str: str | None):
        """列出指定类型的组件"""
        # 显示可用类型帮助
        available_types = [t.value for t in ComponentType]

        if comp_type_str:
            # 尝试匹配组件类型
            comp_type = None
            for t in ComponentType:
                if t.value.lower() == comp_type_str.lower() or t.name.lower() == comp_type_str.lower():
                    comp_type = t
                    break

            if not comp_type:
                await self.send_text(
                    f"❌ 未知的组件类型: `{comp_type_str}`\n"
                    f"可用类型: {', '.join([f'`{t}`' for t in available_types])}"
                )
                return

            components = plugin_info_api.list_components(comp_type, enabled_only=False)
            title = f"🧩 **{comp_type.value} 组件列表** (共 {len(components)} 个)"
        else:
            # 列出所有类型的统计
            response_parts = ["🧩 **组件类型概览**", ""]
            for t in ComponentType:
                comps = plugin_info_api.list_components(t, enabled_only=False)
                enabled = sum(1 for c in comps if c['enabled'])
                if comps:
                    response_parts.append(f"• **{t.value}**: {enabled}/{len(comps)} 启用")

            response_parts.append(f"\n💡 使用 `/system plugin list <类型>` 查看详情")
            response_parts.append(f"可用类型: {', '.join([f'`{t}`' for t in available_types])}")
            await self.send_text("\n".join(response_parts))
            return

        if not components:
            await self.send_text(f"📭 没有找到 {comp_type.value} 类型的组件")
            return

        response_parts = [title, ""]
        for comp in components:
            status = "✅" if comp['enabled'] else "❌"
            response_parts.append(f"{status} `{comp['name']}` (来自: `{comp['plugin_name']}`)")

        await self._send_long_message("\n".join(response_parts))

    @require_permission("plugin.manage", deny_message="❌ 你没有权限搜索组件")
    async def _search_components(self, keyword: str):
        """搜索组件"""
        results = plugin_info_api.search_components_by_name(keyword, case_sensitive=False)

        if not results:
            await self.send_text(f"🔍 未找到包含 `{keyword}` 的组件")
            return

        response_parts = [f"🔍 **搜索结果** (关键词: `{keyword}`, 共 {len(results)} 个)", ""]

        for comp in results:
            status = "✅" if comp['enabled'] else "❌"
            response_parts.append(
                f"{status} `{comp['name']}` ({comp['component_type']})\n"
                f"   来自: `{comp['plugin_name']}`"
            )

        await self._send_long_message("\n".join(response_parts))

    @require_permission("plugin.manage", deny_message="❌ 你没有权限查看禁用组件")
    async def _list_disabled_components(self):
        """列出所有禁用的组件"""
        disabled = component_state_api.get_disabled_components()

        if not disabled:
            await self.send_text("✅ 当前没有被禁用的组件")
            return

        response_parts = [f"🚫 **禁用组件列表** (共 {len(disabled)} 个)", ""]

        # 按插件分组
        by_plugin: dict[str, list] = {}
        for comp in disabled:
            plugin_name = comp.plugin_name
            if plugin_name not in by_plugin:
                by_plugin[plugin_name] = []
            by_plugin[plugin_name].append(comp)

        for plugin_name, comps in by_plugin.items():
            response_parts.append(f"🔌 **{plugin_name}**:")
            for comp in comps:
                response_parts.append(f"  ❌ `{comp.name}` ({comp.component_type.value})")

        await self._send_long_message("\n".join(response_parts))

    @require_permission("plugin.manage", deny_message="❌ 你没有权限管理组件状态")
    async def _set_global_component_state(self, comp_name: str, enabled: bool):
        """全局启用或禁用组件"""
        # 搜索组件
        found_components = plugin_info_api.search_components_by_name(comp_name, exact_match=True)

        if not found_components:
            # 尝试模糊搜索给出建议
            fuzzy_results = plugin_info_api.search_components_by_name(comp_name, exact_match=False)
            if fuzzy_results:
                suggestions = ", ".join([f"`{c['name']}`" for c in fuzzy_results[:5]])
                await self.send_text(f"❌ 未找到名为 `{comp_name}` 的组件\n你可能想找的是: {suggestions}")
            else:
                await self.send_text(f"❌ 未找到名为 `{comp_name}` 的组件")
            return

        if len(found_components) > 1:
            suggestions = "\n".join([f"- `{c['name']}` (类型: {c['component_type']})" for c in found_components])
            await self.send_text(f"❌ 发现多个名为 `{comp_name}` 的组件，操作已取消。\n找到的组件:\n{suggestions}")
            return

        component_info = found_components[0]
        comp_type_str = component_info["component_type"]
        component_type = ComponentType(comp_type_str)

        # 禁用保护
        if not enabled:
            protected_types = [
                ComponentType.INTEREST_CALCULATOR,
                ComponentType.PROMPT,
                ComponentType.ROUTER,
            ]
            if component_type in protected_types:
                await self.send_text(f"❌ 无法禁用核心组件 `{comp_name}` ({comp_type_str})")
                return

        # 执行操作
        success = await component_state_api.set_component_enabled(comp_name, component_type, enabled)

        action_text = "启用" if enabled else "禁用"
        if success:
            await self.send_text(f"✅ 已全局{action_text}组件 `{comp_name}` ({comp_type_str})")
        else:
            if component_type == ComponentType.CHATTER and not enabled:
                await self.send_text(f"❌ 无法禁用最后一个 Chatter 组件 `{comp_name}`")
            else:
                await self.send_text(f"❌ {action_text}组件 `{comp_name}` 失败，请检查日志")

    @require_permission("plugin.manage", deny_message="❌ 你没有权限查看插件报告")
    async def _show_system_report(self):
        """显示系统插件报告"""
        report = plugin_info_api.get_system_report()
        
        response_parts = [
            "📊 **系统插件报告**",
            f"  - 已加载插件: {report['system_info']['loaded_plugins_count']}",
            f"  - 组件总数: {report['system_info']['total_components_count']}",
        ]

        if report["plugins"]:
            response_parts.append("\n✅ **已加载插件:**")
            for name, info in report["plugins"].items():
                response_parts.append(f"  • **{info['display_name']} (`{name}`)** v{info['version']} by {info['author']}")
        
        if report["failed_plugins"]:
            response_parts.append("\n❌ **加载失败的插件:**")
            for name, error in report["failed_plugins"].items():
                response_parts.append(f"  • **`{name}`**: {error}")
        
        await self._send_long_message("\n".join(response_parts))


    @require_permission("plugin.manage", deny_message="❌ 你没有权限扫描插件")
    async def _rescan_plugin_dirs(self):
        """重新扫描插件目录"""
        await self.send_text("🔄 正在重新扫描插件目录...")
        success, fail = plugin_manage_api.rescan_and_register_plugins(load_after_register=True)
        await self.send_text(f"✅ 扫描完成！\n新增成功: {success}个, 新增失败: {fail}个。")

    @require_permission("plugin.manage", deny_message="❌ 你没有权限加载插件")
    async def _load_plugin(self, plugin_name: str):
        """加载指定插件"""
        success = plugin_manage_api.register_plugin_from_file(plugin_name, load_after_register=True)
        if success:
            await self.send_text(f"✅ 插件加载成功: `{plugin_name}`")
        else:
            await self.send_text(f"❌ 插件加载失败: `{plugin_name}`。请检查日志获取详细信息。")


    @require_permission("plugin.manage", deny_message="❌ 你没有权限重载插件")
    async def _reload_plugin(self, plugin_name: str):
        """重新加载指定插件"""
        try:
            success = await plugin_manage_api.reload_plugin(plugin_name)
            if success:
                await self.send_text(f"✅ 插件重新加载成功: `{plugin_name}`")
            else:
                await self.send_text(f"❌ 插件重新加载失败: `{plugin_name}`")
        except ValueError as e:
            await self.send_text(f"❌ 操作失败: {e}")


    @require_permission("plugin.manage", deny_message="❌ 你没有权限重载所有插件")
    async def _reload_all_plugins(self):
        """重新加载所有插件"""
        await self.send_text("🔄 正在重新加载所有插件...")
        success = await plugin_manage_api.reload_all_plugins()
        if success:
            await self.send_text("✅ 所有插件已成功重载。")
        else:
            await self.send_text("⚠️ 部分插件重载失败，请检查日志。")

    @require_permission("plugin.manage.local", deny_message="❌ 你没有局部管理插件组件的权限")
    async def _set_local_component_state(self, args: list[str], enabled: bool):
        """在局部范围内启用或禁用一个组件"""
        # 命令格式: <component_name> [group <group_id> | private <user_id>]
        if not args:
            action = "enable_local" if enabled else "disable_local"
            await self.send_text(f"❌ 用法: /system plugin {action} <名称> [group <群号> | private <QQ号>]")
            return

        comp_name = args[0]
        context_args = args[1:]
        stream_id = self.message.chat_info.stream_id  # 默认作用于当前会话

        # 1. 搜索组件
        found_components = plugin_info_api.search_components_by_name(comp_name, exact_match=True)

        if not found_components:
            await self.send_text(f"❌ 未找到名为 '{comp_name}' 的组件。")
            return
        
        if len(found_components) > 1:
            suggestions = "\n".join([f"- `{c['name']}` (类型: {c['component_type']})" for c in found_components])
            await self.send_text(f"❌ 发现多个名为 '{comp_name}' 的组件，操作已取消。\n找到的组件:\n{suggestions}")
            return

        component_info = found_components[0]
        comp_type_str = component_info["component_type"]
        component_type = ComponentType(comp_type_str)

        # 2. 增加禁用保护
        if not enabled:  # 如果是禁用操作
            # 定义不可禁用的核心组件类型
            protected_types = [
                ComponentType.INTEREST_CALCULATOR,
                ComponentType.PROMPT,
                ComponentType.ROUTER,
            ]
            if component_type in protected_types:
                await self.send_text(f"❌ 无法局部禁用核心组件 '{comp_name}' ({comp_type_str})。")
                return

        # 3. 解析上下文
        if len(context_args) >= 2:
            context_type = context_args[0].lower()
            context_id = context_args[1]
            
            target_stream = None
            if context_type == "group":
                target_stream = chat_api.get_stream_by_group_id(
                    group_id=context_id,
                    platform=self.message.chat_info.platform
                )
            elif context_type == "private":
                target_stream = chat_api.get_stream_by_user_id(
                    user_id=context_id,
                    platform=self.message.chat_info.platform
                )
            else:
                await self.send_text("❌ 无效的作用域类型，请使用 'group' 或 'private'。")
                return

            if not target_stream:
                await self.send_text(f"❌ 在当前平台找不到指定的 {context_type}: `{context_id}`。")
                return
            
            stream_id = target_stream.stream_id

        # 4. 执行操作
        success = component_state_api.set_component_enabled_local(
            stream_id=stream_id,
            name=comp_name,
            component_type=component_type,
            enabled=enabled
        )

        action_text = "启用" if enabled else "禁用"
        if success:
            await self.send_text(f"✅ 在会话 `{stream_id}` 中，已成功将组件 `{comp_name}` ({comp_type_str}) 设置为 {action_text} 状态。")
        else:
            await self.send_text(f"❌ 操作失败。可能无法禁用最后一个启用的 Chatter，或组件不存在。请检查日志。")


    # =================================================================
    # Permission Management Section
    # =================================================================
    async def _handle_permission_commands(self, args: list[str]):
        """处理权限管理相关命令"""
        if not args:
            await self._show_help("permission")
            return

        action = args[0].lower()
        remaining_args = args[1:]
        chat_info = self.message.chat_info

        if action in ["grant", "授权", "give"]:
            await self._grant_permission(chat_info, remaining_args)
        elif action in ["revoke", "撤销", "remove"]:
            await self._revoke_permission(chat_info, remaining_args)
        elif action in ["list", "列表", "ls"]:
            await self._list_permissions(chat_info, remaining_args)
        elif action in ["check", "检查"]:
            await self._check_permission(chat_info, remaining_args)
        elif action in ["nodes", "节点"]:
            await self._list_nodes(chat_info, remaining_args)
        elif action in ["allnodes", "全部节点", "all"]:
            await self._list_all_nodes_with_description(chat_info)
        else:
            await self.send_text(f"❌ 未知的权限子命令: {action}")

    @staticmethod
    def _parse_user_mention(mention: str) -> str | None:
        """解析用户提及，提取QQ号"""
        at_match = re.search(r"@<[^:]+:(\d+)>", mention)
        if at_match:
            return at_match.group(1)
        if mention.isdigit():
            return mention
        return None

    @require_permission("permission.manage", deny_message="❌ 你没有权限管理的权限")
    async def _grant_permission(self, chat_info, args: list[str]):
        """授权用户权限"""
        if len(args) < 2:
            await self.send_text("❌ 用法: /system permission grant <@用户|QQ号> <权限节点>")
            return

        user_id = self._parse_user_mention(args[0])
        if not user_id:
            await self.send_text("❌ 无效的用户格式")
            return

        permission_node = args[1]
        success = await permission_api.grant_permission(chat_info.platform, user_id, permission_node)
        if success:
            await self.send_text(f"✅ 已授权用户 {user_id} 权限节点 `{permission_node}`")
        else:
            await self.send_text("❌ 授权失败")

    @require_permission("permission.manage", deny_message="❌ 你没有权限管理的权限")
    async def _revoke_permission(self, chat_info, args: list[str]):
        """撤销用户权限"""
        if len(args) < 2:
            await self.send_text("❌ 用法: /system permission revoke <@用户|QQ号> <权限节点>")
            return

        user_id = self._parse_user_mention(args[0])
        if not user_id:
            await self.send_text("❌ 无效的用户格式")
            return

        permission_node = args[1]
        success = await permission_api.revoke_permission(chat_info.platform, user_id, permission_node)
        if success:
            await self.send_text(f"✅ 已撤销用户 {user_id} 权限节点 `{permission_node}`")
        else:
            await self.send_text("❌ 撤销失败")

    @require_permission("permission.view", deny_message="❌ 你没有查看权限的权限")
    async def _list_permissions(self, chat_info, args: list[str]):
        """列出用户权限"""
        target_user_id = None
        if args:
            target_user_id = self._parse_user_mention(args[0])
            if not target_user_id:
                await self.send_text("❌ 无效的用户格式")
                return
        else:
            target_user_id = chat_info.user_info.user_id

        is_master = await permission_api.is_master(chat_info.platform, target_user_id)
        permissions = await permission_api.get_user_permissions(chat_info.platform, target_user_id)

        if is_master:
            response = f"👑 用户 `{target_user_id}` 是Master用户，拥有所有权限"
        else:
            if permissions:
                perm_list = "\n".join([f"• `{perm}`" for perm in permissions])
                response = f"📋 用户 `{target_user_id}` 拥有的权限：\n{perm_list}"
            else:
                response = f"📋 用户 `{target_user_id}` 没有任何权限"
        await self.send_text(response)

    @require_permission("permission.view", deny_message="❌ 你没有查看权限的权限")
    async def _check_permission(self, chat_info, args: list[str]):
        """检查用户权限"""
        if len(args) < 2:
            await self.send_text("❌ 用法: /system permission check <@用户|QQ号> <权限节点>")
            return

        user_id = self._parse_user_mention(args[0])
        if not user_id:
            await self.send_text("❌ 无效的用户格式")
            return

        permission_node = args[1]
        has_permission = await permission_api.check_permission(chat_info.platform, user_id, permission_node)
        is_master = await permission_api.is_master(chat_info.platform, user_id)

        if has_permission:
            response = f"✅ 用户 `{user_id}` 拥有权限 `{permission_node}`"
            if is_master:
                response += "（Master用户）"
        else:
            response = f"❌ 用户 `{user_id}` 没有权限 `{permission_node}`"
        await self.send_text(response)

    @require_permission("permission.view", deny_message="❌ 你没有查看权限的权限")
    async def _list_nodes(self, chat_info, args: list[str]):
        """列出权限节点"""
        plugin_name = args[0] if args else None
        if plugin_name:
            nodes = await permission_api.get_plugin_permission_nodes(plugin_name)
            title = f"📋 插件 {plugin_name} 的权限节点："
        else:
            nodes = await permission_api.get_all_permission_nodes()
            title = "📋 所有权限节点："

        if not nodes:
            response = f"📋 插件 {plugin_name} 没有注册任何权限节点" if plugin_name else "📋 系统中没有任何权限节点"
        else:
            node_list = []
            for node in nodes:
                default_text = "（默认授权）" if node["default_granted"] else "（默认拒绝）"
                node_list.append(f"• {node['node_name']} {default_text}")
                node_list.append(f"  📄 {node['description']}")
                if not plugin_name:
                    node_list.append(f"  🔌 插件: {node['plugin_name']}")
                node_list.append("")
            response = title + "\n" + "\n".join(node_list)
        await self.send_text(response)

    @require_permission("permission.view", deny_message="❌ 你没有查看权限的权限")
    async def _list_all_nodes_with_description(self, chat_stream):
        """列出所有插件的权限节点（带详细描述）"""
        all_nodes = await permission_api.get_all_permission_nodes()
        if not all_nodes:
            await self.send_text("📋 系统中没有任何权限节点")
            return

        plugins_dict = {}
        for node in all_nodes:
            plugin_name = node["plugin_name"]
            if plugin_name not in plugins_dict:
                plugins_dict[plugin_name] = []
            plugins_dict[plugin_name].append(node)

        response_parts = ["📋 所有插件权限节点详情：\n"]
        for plugin_name in sorted(plugins_dict.keys()):
            nodes = plugins_dict[plugin_name]
            response_parts.append(f"🔌 **{plugin_name}** ({len(nodes)}个节点)：")
            for node in nodes:
                default_text = "✅默认授权" if node["default_granted"] else "❌默认拒绝"
                response_parts.append(f"  • `{node['node_name']}` - {default_text}")
                response_parts.append(f"    📄 {node['description']}")
            response_parts.append("")

        total_nodes = len(all_nodes)
        total_plugins = len(plugins_dict)
        response_parts.append(f"📊 统计：共 {total_plugins} 个插件，{total_nodes} 个权限节点")
        response = "\n".join(response_parts)

        if len(response) > 4000:
            await self._send_long_message(response)
        else:
            await self.send_text(response)

    async def _send_long_message(self, message: str):
        """发送长消息，自动分段"""
        lines = message.split("\n")
        current_chunk = []
        current_length = 0
        for line in lines:
            line_length = len(line) + 1
            if current_length + line_length > 3500 and current_chunk:
                await self.send_text("\n".join(current_chunk))
                current_chunk = []
                current_length = 0
            current_chunk.append(line)
            current_length += line_length
        if current_chunk:
            await self.send_text("\n".join(current_chunk))


@register_plugin
class SystemManagementPlugin(BasePlugin):
    plugin_name: str = "system_management"
    enable_plugin: bool = False
    dependencies: ClassVar[list[str]] = []  # 插件依赖列表
    python_dependencies: ClassVar[list[str]] = []  # Python包依赖列表，现在使用内置API
    config_file_name: str = "config.toml"  # 配置文件名
    config_schema: ClassVar[dict] = {
        "plugin": {
            "enabled": ConfigField(bool, default=False, description="是否启用插件"),
        }
    }

    def get_plugin_components(self) -> list[tuple[PlusCommandInfo, type[PlusCommand]]]:
        """返回插件的PlusCommand组件"""
        return [(SystemCommand.get_plus_command_info(), SystemCommand)]

    permission_nodes: ClassVar[list[PermissionNodeField]] = [
        PermissionNodeField(
            node_name="access",
            description="系统访问：可以使用系统管理命令",
        ),
        PermissionNodeField(
            node_name="permission.manage",
            description="权限管理：授权和撤销权限",
        ),
        PermissionNodeField(
            node_name="permission.view",
            description="权限查看：查看权限信息",
        ),
        PermissionNodeField(
            node_name="plugin.manage",
            description="插件管理：管理插件的加载、卸载、重载等",
        ),
        PermissionNodeField(
            node_name="schedule.view",
            description="定时任务查看：查看定时任务列表和详情",
        ),
        PermissionNodeField(
            node_name="schedule.manage",
            description="定时任务管理：暂停和恢复定时任务",
        ),
        PermissionNodeField(
            node_name="plugin.manage.local",
            description="局部插件管理：在指定会话中启用或禁用组件",
        ),
    ]
