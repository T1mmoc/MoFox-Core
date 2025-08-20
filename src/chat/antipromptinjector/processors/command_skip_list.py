# -*- coding: utf-8 -*-
"""
命令跳过列表模块

本模块负责管理反注入系统的命令跳过列表，自动收集插件注册的命令
并提供检查机制来跳过对合法命令的反注入检测。
"""

import re
from typing import Set, List, Pattern, Optional, Dict
from dataclasses import dataclass

from src.common.logger import get_logger
from src.config.config import global_config

logger = get_logger("anti_injector.skip_list")


@dataclass
class SkipPattern:
    """跳过模式信息"""
    pattern: str
    """原始模式字符串"""
    
    compiled_pattern: Pattern[str]
    """编译后的正则表达式"""
    
    source: str
    """模式来源：plugin, manual, system"""
    
    description: str = ""
    """模式描述"""


class CommandSkipListManager:
    """命令跳过列表管理器"""
    
    def __init__(self):
        """初始化跳过列表管理器"""
        self.config = global_config.anti_prompt_injection
        self._skip_patterns: Dict[str, SkipPattern] = {}
        self._plugin_command_patterns: Set[str] = set()
        self._is_initialized = False
        
    def initialize(self):
        """初始化跳过列表"""
        if self._is_initialized:
            return
            
        logger.info("初始化反注入命令跳过列表...")
        
        # 清空现有模式
        self._skip_patterns.clear()
        self._plugin_command_patterns.clear()
        
        if not self.config.enable_command_skip_list:
            logger.info("命令跳过列表已禁用")
            return
        
        # 添加系统命令模式
        if self.config.skip_system_commands:
            self._add_system_command_patterns()
        
        # 自动收集插件命令
        if self.config.auto_collect_plugin_commands:
            self._collect_plugin_commands()
        
        # 添加手动指定的模式
        self._add_manual_patterns()
        
        self._is_initialized = True
        logger.info(f"跳过列表初始化完成，共收集 {len(self._skip_patterns)} 个模式")
        
    def _add_system_command_patterns(self):
        """添加系统内置命令模式"""
        system_patterns = [
            (r"^/pm\b", "/pm 插件管理命令"),
            (r"^/反注入统计\b", "反注入统计查询命令"),
            (r"^^/反注入跳过列表(?:\s+(.+))?$", "反注入列表管理命令"),
        ]
        
        for pattern_str, description in system_patterns:
            self._add_skip_pattern(pattern_str, "system", description)
    
    def _collect_plugin_commands(self):
        """自动收集插件注册的命令"""
        try:
            from src.plugin_system.apis import component_manage_api
            from src.plugin_system.base.component_types import ComponentType
            
            # 获取所有注册的命令组件
            command_components = component_manage_api.get_components_info_by_type(ComponentType.COMMAND)
            
            if not command_components:
                logger.debug("没有找到注册的命令组件（插件可能还未加载）")
                return
            
            collected_count = 0
            for command_name, command_info in command_components.items():
                # 获取命令的匹配模式
                if hasattr(command_info, 'command_pattern') and command_info.command_pattern:
                    pattern = command_info.command_pattern
                    description = f"插件命令: {command_name}"
                    
                    # 添加到跳过列表
                    if self._add_skip_pattern(pattern, "plugin", description):
                        self._plugin_command_patterns.add(pattern)
                        collected_count += 1
                        logger.debug(f"收集插件命令模式: {pattern} ({command_name})")
                
                # 如果没有明确的模式，尝试从命令名生成基础模式
                elif command_name:
                    # 生成基础命令模式
                    basic_patterns = [
                        f"^/{re.escape(command_name)}\\b",  # /command_name
                        f"^{re.escape(command_name)}\\b",   # command_name
                    ]
                    
                    for pattern in basic_patterns:
                        description = f"插件命令: {command_name} (自动生成)"
                        if self._add_skip_pattern(pattern, "plugin", description):
                            self._plugin_command_patterns.add(pattern)
                            collected_count += 1
            
            if collected_count > 0:
                logger.info(f"自动收集了 {collected_count} 个插件命令模式")
            else:
                logger.debug("当前没有收集到插件命令模式（插件可能还未加载）")
            
        except Exception as e:
            logger.warning(f"自动收集插件命令时出错: {e}")
    
    def _add_manual_patterns(self):
        """添加手动指定的模式"""
        manual_patterns = self.config.manual_skip_patterns or []
        
        for pattern_str in manual_patterns:
            if pattern_str.strip():
                self._add_skip_pattern(pattern_str.strip(), "manual", "手动配置的跳过模式")
    
    def _add_skip_pattern(self, pattern_str: str, source: str, description: str = "") -> bool:
        """添加跳过模式
        
        Args:
            pattern_str: 模式字符串
            source: 模式来源
            description: 模式描述
            
        Returns:
            是否成功添加
        """
        try:
            # 编译正则表达式
            compiled_pattern = re.compile(pattern_str, re.IGNORECASE | re.DOTALL)
            
            # 创建跳过模式对象
            skip_pattern = SkipPattern(
                pattern=pattern_str,
                compiled_pattern=compiled_pattern,
                source=source,
                description=description
            )
            
            # 使用模式字符串作为键，避免重复
            pattern_key = f"{source}:{pattern_str}"
            self._skip_patterns[pattern_key] = skip_pattern
            
            return True
            
        except re.error as e:
            logger.error(f"无效的正则表达式模式 '{pattern_str}': {e}")
            return False
    
    def should_skip_detection(self, message_text: str) -> tuple[bool, Optional[str]]:
        """检查消息是否应该跳过反注入检测
        
        Args:
            message_text: 待检查的消息文本
            
        Returns:
            (是否跳过, 匹配的模式描述)
        """
        if not self.config.enable_command_skip_list or not self._is_initialized:
            return False, None
        
        message_text = message_text.strip()
        if not message_text:
            return False, None
        
        # 检查所有跳过模式
        for _pattern_key, skip_pattern in self._skip_patterns.items():
            try:
                if skip_pattern.compiled_pattern.search(message_text):
                    logger.debug(f"消息匹配跳过模式: {skip_pattern.pattern} ({skip_pattern.description})")
                    return True, skip_pattern.description
            except Exception as e:
                logger.warning(f"检查跳过模式时出错 '{skip_pattern.pattern}': {e}")
        
        return False, None
    
    def refresh_plugin_commands(self):
        """刷新插件命令列表"""
        if not self.config.auto_collect_plugin_commands:
            return
        
        logger.info("刷新插件命令跳过列表...")
        
        # 移除旧的插件模式
        old_plugin_patterns = [
            key for key, pattern in self._skip_patterns.items() 
            if pattern.source == "plugin"
        ]
        
        for key in old_plugin_patterns:
            del self._skip_patterns[key]
        
        self._plugin_command_patterns.clear()
        
        # 重新收集插件命令
        self._collect_plugin_commands()
        
        logger.info(f"插件命令跳过列表已刷新，当前共有 {len(self._skip_patterns)} 个模式")
    
    def get_skip_patterns_info(self) -> Dict[str, List[Dict[str, str]]]:
        """获取跳过模式信息
        
        Returns:
            按来源分组的模式信息
        """
        result = {"system": [], "plugin": [], "manual": []}
        
        for skip_pattern in self._skip_patterns.values():
            pattern_info = {
                "pattern": skip_pattern.pattern,
                "description": skip_pattern.description
            }
            
            if skip_pattern.source in result:
                result[skip_pattern.source].append(pattern_info)
        
        return result
    
    def add_temporary_skip_pattern(self, pattern: str, description: str = "") -> bool:
        """添加临时跳过模式（运行时添加，不保存到配置）
        
        Args:
            pattern: 模式字符串
            description: 模式描述
            
        Returns:
            是否成功添加
        """
        return self._add_skip_pattern(pattern, "temporary", description or "临时跳过模式")
    
    def remove_temporary_patterns(self):
        """移除所有临时跳过模式"""
        temp_patterns = [
            key for key, pattern in self._skip_patterns.items() 
            if pattern.source == "temporary"
        ]
        
        for key in temp_patterns:
            del self._skip_patterns[key]
        
        logger.info(f"已移除 {len(temp_patterns)} 个临时跳过模式")


# 全局跳过列表管理器实例
skip_list_manager = CommandSkipListManager()


def initialize_skip_list():
    """初始化跳过列表"""
    skip_list_manager.initialize()


def should_skip_injection_detection(message_text: str) -> tuple[bool, Optional[str]]:
    """检查消息是否应该跳过反注入检测"""
    return skip_list_manager.should_skip_detection(message_text)


def refresh_plugin_commands():
    """刷新插件命令列表"""
    skip_list_manager.refresh_plugin_commands()


def get_skip_patterns_info():
    """获取跳过模式信息"""
    return skip_list_manager.get_skip_patterns_info()
