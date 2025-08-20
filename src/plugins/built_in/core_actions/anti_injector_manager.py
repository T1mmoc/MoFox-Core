"""
反注入系统管理命令插件

提供管理和监控反注入系统的命令接口，包括：
- 系统状态查看
- 配置修改
- 统计信息查看
- 测试功能
"""


from src.plugin_system.base import BaseCommand
from src.chat.antipromptinjector import get_anti_injector
from src.chat.antipromptinjector.processors.command_skip_list import (
    get_skip_patterns_info, 
    skip_list_manager
)
from src.common.logger import get_logger

logger = get_logger("anti_injector.commands")


class AntiInjectorStatusCommand(BaseCommand):
    """反注入系统状态查看命令"""
    
    command_name = "反注入状态"  # 命令名称，作为唯一标识符
    command_description = "查看反注入系统状态和统计信息"  # 命令描述
    command_pattern = r"^/反注入状态$"  # 命令匹配的正则表达式

    async def execute(self) -> tuple[bool, str, bool]:
        try:
            anti_injector = get_anti_injector()
            stats = await anti_injector.get_stats()
            
            # 检查反注入系统是否禁用
            if stats.get("status") == "disabled":
                await self.send_text("❌ 反注入系统未启用\n\n💡 请在配置文件中启用反注入功能后重试")
                return True, "反注入系统未启用", True
            
            if stats.get("error"):
                await self.send_text(f"❌ 获取状态失败: {stats['error']}")
                return False, f"获取状态失败: {stats['error']}", True
            
            status_text = f"""🛡️ 反注入系统状态报告

📊 运行统计:
• 运行时间: {stats['uptime']}
• 处理消息总数: {stats['total_messages']}
• 检测到注入: {stats['detected_injections']}
• 阻止消息: {stats['blocked_messages']}
• 加盾消息: {stats['shielded_messages']}

📈 性能指标:
• 检测率: {stats['detection_rate']}
• 平均处理时间: {stats['average_processing_time']}
• 最后处理时间: {stats['last_processing_time']}

⚠️ 错误计数: {stats['error_count']}"""
            await self.send_text(status_text)
            return True, status_text, True
            
        except Exception as e:
            logger.error(f"获取反注入系统状态失败: {e}")
            await self.send_text(f"获取状态失败: {str(e)}")
            return False, f"获取状态失败: {str(e)}", True


class AntiInjectorSkipListCommand(BaseCommand):
    """反注入跳过列表管理命令"""
    
    command_name = "反注入跳过列表"
    command_description = "管理反注入系统的命令跳过列表"
    command_pattern = r"^/反注入跳过列表(?:\s+(?P<subcommand>.+))?$"

    async def execute(self) -> tuple[bool, str, bool]:
        try:
            # 从正则匹配中获取参数
            subcommand_raw = None
            if self.matched_groups and "subcommand" in self.matched_groups:
                subcommand_raw = self.matched_groups.get("subcommand")
            
            subcommand = subcommand_raw.strip() if subcommand_raw and subcommand_raw.strip() else ""
            
            if not subcommand:
                return await self._show_status()
            
            # 处理子命令
            subcommand_parts = subcommand.split()
            main_cmd = subcommand_parts[0].lower()
            
            if main_cmd == "状态" or main_cmd == "status":
                return await self._show_status()
            elif main_cmd == "刷新" or main_cmd == "refresh":
                return await self._refresh_commands()
            elif main_cmd == "列表" or main_cmd == "list":
                list_type = subcommand_parts[1] if len(subcommand_parts) > 1 else "all"
                return await self._show_patterns(list_type)
            elif main_cmd == "添加" or main_cmd == "add":
                await self.send_text("暂不支持权限管理系统，该命令不可用")
                return False, "功能受限", True
            elif main_cmd == "帮助" or main_cmd == "help":
                return await self._show_help()
            else:
                await self.send_text(f"未知的子命令: {main_cmd}")
                return await self._show_help()
                
        except Exception as e:
            logger.error(f"执行反注入跳过列表命令失败: {e}")
            await self.send_text(f"命令执行失败: {str(e)}")
            return False, f"命令执行失败: {str(e)}", True
    
    async def _show_help(self) -> tuple[bool, str, bool]:
        """显示帮助信息"""
        help_text = """🛡️ 反注入跳过列表管理

📋 可用命令:
• /反注入跳过列表 状态 - 查看跳过列表状态
• /反注入跳过列表 列表 [类型] - 查看跳过模式列表
  - 类型: all(所有), system(系统), plugin(插件), manual(手动)
• /反注入跳过列表 刷新 - 刷新插件命令列表
• /反注入跳过列表 添加 <模式> - 临时添加跳过模式
• /反注入跳过列表 帮助 - 显示此帮助信息

💡 示例:
• /反注入跳过列表 列表 plugin
• /反注入跳过列表 添加 ^/test\\b"""
        
        await self.send_text(help_text)
        return True, "帮助信息已发送", True
    
    async def _show_status(self) -> tuple[bool, str, bool]:
        """显示跳过列表状态"""
        # 强制刷新插件命令，确保获取最新的插件列表
        patterns_info = get_skip_patterns_info()
        
        system_count = len(patterns_info.get("system", []))
        plugin_count = len(patterns_info.get("plugin", []))
        manual_count = len(patterns_info.get("manual", []))
        temp_count = len([p for p in skip_list_manager._skip_patterns.values() if p.source == "temporary"])
        total_count = system_count + plugin_count + manual_count + temp_count
        
        from src.config.config import global_config
        config = global_config.anti_prompt_injection
        
        status_text = f"""🛡️ 反注入跳过列表状态

📊 模式统计:
• 系统命令模式: {system_count} 个
• 插件命令模式: {plugin_count} 个  
• 手动配置模式: {manual_count} 个
• 临时添加模式: {temp_count} 个
• 总计: {total_count} 个

⚙️ 配置状态:
• 跳过列表启用: {'✅' if config.enable_command_skip_list else '❌'}
• 自动收集插件命令: {'✅' if config.auto_collect_plugin_commands else '❌'}
• 跳过系统命令: {'✅' if config.skip_system_commands else '❌'}

💡 使用 "/反注入跳过列表 列表" 查看详细模式"""
        
        await self.send_text(status_text)
        return True, status_text, True
    
    async def _show_patterns(self, pattern_type: str = "all") -> tuple[bool, str, bool]:
        """显示跳过模式列表"""
        # 强制刷新插件命令，确保获取最新的插件列表
        patterns_info = get_skip_patterns_info()
        
        if pattern_type == "all":
            # 显示所有模式
            result_text = "🛡️ 所有跳过模式列表\n\n"
            
            for source_type, patterns in patterns_info.items():
                if patterns:
                    type_name = {
                        "system": "📱 系统命令",
                        "plugin": "🔌 插件命令", 
                        "manual": "✋ 手动配置"
                    }.get(source_type, source_type)
                    
                    result_text += f"{type_name} ({len(patterns)} 个):\n"
                    for i, pattern in enumerate(patterns[:10], 1):  # 限制显示前10个
                        result_text += f"  {i}. {pattern['pattern']}\n"
                        if pattern['description']:
                            result_text += f"     说明: {pattern['description']}\n"
                    
                    if len(patterns) > 10:
                        result_text += f"  ... 还有 {len(patterns) - 10} 个模式\n"
                    result_text += "\n"
            
            # 添加临时模式
            temp_patterns = [p for p in skip_list_manager._skip_patterns.values() if p.source == "temporary"]
            if temp_patterns:
                result_text += f"⏱️ 临时模式 ({len(temp_patterns)} 个):\n"
                for i, pattern in enumerate(temp_patterns[:5], 1):
                    result_text += f"  {i}. {pattern.pattern}\n"
                if len(temp_patterns) > 5:
                    result_text += f"  ... 还有 {len(temp_patterns) - 5} 个临时模式\n"
        
        else:
            # 显示特定类型的模式
            if pattern_type not in patterns_info:
                await self.send_text(f"未知的模式类型: {pattern_type}")
                return False, "未知模式类型", True
            
            patterns = patterns_info[pattern_type]
            type_name = {
                "system": "📱 系统命令模式",
                "plugin": "🔌 插件命令模式", 
                "manual": "✋ 手动配置模式"
            }.get(pattern_type, pattern_type)
            
            result_text = f"🛡️ {type_name} ({len(patterns)} 个)\n\n"
            
            if not patterns:
                result_text += "暂无此类型的跳过模式"
            else:
                for i, pattern in enumerate(patterns, 1):
                    result_text += f"{i}. {pattern['pattern']}\n"
                    if pattern['description']:
                        result_text += f"   说明: {pattern['description']}\n"
                    result_text += "\n"
        
        await self.send_text(result_text)
        return True, result_text, True
    
    async def _refresh_commands(self) -> tuple[bool, str, bool]:
        """刷新插件命令列表"""
        try:
            patterns_info = get_skip_patterns_info()
            plugin_count = len(patterns_info.get("plugin", []))
            
            result_text = f"✅ 插件命令列表已刷新\n\n当前收集到 {plugin_count} 个插件命令模式"
            await self.send_text(result_text)
            return True, result_text, True
            
        except Exception as e:
            logger.error(f"刷新插件命令列表失败: {e}")
            await self.send_text(f"刷新失败: {str(e)}")
            return False, f"刷新失败: {str(e)}", True
    
    async def _add_temporary_pattern(self, pattern: str) -> tuple[bool, str, bool]:
        """添加临时跳过模式"""
        try:
            success = skip_list_manager.add_temporary_skip_pattern(pattern, "用户临时添加")
            
            if success:
                result_text = f"✅ 临时跳过模式已添加: {pattern}\n\n⚠️ 此模式仅在当前运行期间有效，重启后会失效"
                await self.send_text(result_text)
                return True, result_text, True
            else:
                result_text = f"❌ 添加临时跳过模式失败: {pattern}\n\n可能是无效的正则表达式"
                await self.send_text(result_text)
                return False, result_text, True
                
        except Exception as e:
            logger.error(f"添加临时跳过模式失败: {e}")
            await self.send_text(f"添加失败: {str(e)}")
            return False, f"添加失败: {str(e)}", True