from __future__ import annotations

# --- 标准库导入 ---
import re
from pathlib import Path
from re import Pattern
from typing import Any, cast

# --- 第三方库导入 ---
import toml
from fastapi import Depends

# --- 项目内模块导入 ---
from src.common.logger import get_logger
from src.config.config import global_config as bot_config
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.base_adapter import BaseAdapter
from src.plugin_system.base.base_chatter import BaseChatter
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.base_events_handler import BaseEventHandler
from src.plugin_system.base.base_http_component import BaseRouterComponent
from src.plugin_system.base.base_interest_calculator import BaseInterestCalculator
from src.plugin_system.base.base_prompt import BasePrompt
from src.plugin_system.base.base_tool import BaseTool
from src.plugin_system.base.component_types import (
    ActionInfo,
    AdapterInfo,
    ChatterInfo,
    CommandInfo,
    ComponentInfo,
    ComponentType,
    EventHandlerInfo,
    InterestCalculatorInfo,
    PluginInfo,
    PlusCommandInfo,
    PromptInfo,
    RouterInfo,
    ToolInfo,
)
from src.plugin_system.base.plus_command import PlusCommand, create_legacy_command_adapter

# --- 日志记录器 ---
logger = get_logger("component_registry")

# --- 类型别名 ---
# 统一的组件类类型别名，方便类型提示
ComponentClassType = (
    type[BaseCommand]
    | type[BaseAction]
    | type[BaseTool]
    | type[BaseEventHandler]
    | type[PlusCommand]
    | type[BaseChatter]
    | type[BaseInterestCalculator]
    | type[BasePrompt]
    | type[BaseRouterComponent]
    | type[BaseAdapter]
)


def _assign_plugin_attrs(cls: Any, plugin_name: str, plugin_config: dict) -> None:
    """
    为组件类动态赋予插件相关属性。

    这是一个辅助函数，用于避免在各个注册函数中重复编写相同的属性设置代码。

    Args:
        cls (Any): 需要设置属性的组件类。
        plugin_name (str): 插件的名称。
        plugin_config (dict): 插件的配置信息。
    """
    setattr(cls, "plugin_name", plugin_name)
    setattr(cls, "plugin_config", plugin_config)


class ComponentRegistry:
    """
    统一的组件注册中心。

    该类是插件系统的核心，负责管理所有插件组件的注册、发现、状态管理和生命周期。
    它为不同类型的组件（如 Command, Action, Tool 等）提供了统一的注册接口和专门的查询方法。

    主要职责:
    - 注册和取消注册插件与组件。
    - 按类型和名称存储和索引所有组件。
    - 管理组件的全局启用/禁用状态。
    - 支持会话级别的组件局部（临时）启用/禁用状态。
    - 提供按类型、名称或状态查询组件的方法。
    - 处理组件之间的依赖和冲突。
    - 动态加载和管理 MCP (Model-Copilot-Plugin) 工具。
    """

    def __init__(self):
        """初始化组件注册中心，创建所有必要的注册表。"""
        # --- 通用注册表 ---
        # 核心注册表，存储所有组件的信息，使用命名空间式键 f"{component_type}.{component_name}"
        self._components: dict[str, "ComponentInfo"] = {}
        # 按类型分类的组件注册表，方便按类型快速查找
        self._components_by_type: dict["ComponentType", dict[str, "ComponentInfo"]] = {
            types: {} for types in ComponentType
        }
        # 存储组件类本身，用于实例化
        self._components_classes: dict[str, ComponentClassType] = {}

        # --- 插件注册表 ---
        self._plugins: dict[str, "PluginInfo"] = {}

        # --- 特定类型组件的专用注册表 ---

        # Action
        self._action_registry: dict[str, type["BaseAction"]] = {}
        self._default_actions: dict[str, "ActionInfo"] = {}  # 存储全局启用的Action

        # Command (旧版)
        self._command_registry: dict[str, type["BaseCommand"]] = {}
        self._command_patterns: dict[Pattern, str] = {}  # 编译后的正则表达式 -> command名

        # PlusCommand (新版)
        self._plus_command_registry: dict[str, type[PlusCommand]] = {}

        # Tool
        self._tool_registry: dict[str, type["BaseTool"]] = {}
        self._llm_available_tools: dict[str, type["BaseTool"]] = {}  # 存储全局启用的Tool

        # EventHandler
        self._event_handler_registry: dict[str, type["BaseEventHandler"]] = {}
        self._enabled_event_handlers: dict[str, type["BaseEventHandler"]] = {}

        # Chatter
        self._chatter_registry: dict[str, type["BaseChatter"]] = {}
        self._enabled_chatter_registry: dict[str, type["BaseChatter"]] = {}

        # InterestCalculator
        self._interest_calculator_registry: dict[str, type["BaseInterestCalculator"]] = {}
        self._enabled_interest_calculator_registry: dict[str, type["BaseInterestCalculator"]] = {}

        # Prompt
        self._prompt_registry: dict[str, type[BasePrompt]] = {}
        self._enabled_prompt_registry: dict[str, type[BasePrompt]] = {}

        # MCP (Model-Copilot-Plugin) Tools
        self._mcp_tools: list[Any] = []  # 存储 MCP 工具适配器实例
        self._mcp_tools_loaded = False  # 标记 MCP 工具是否已加载

        # --- 状态管理 ---
        # 局部组件状态管理器，用于在特定会话中临时覆盖全局状态
        self._local_component_states: dict[str, dict[tuple[str, ComponentType], bool]] = {}
        # 定义不支持局部状态管理的组件类型集合
        self._no_local_state_types: set[ComponentType] = {
            ComponentType.ROUTER,
            ComponentType.EVENT_HANDLER,
            ComponentType.PROMPT,
            # 根据设计，COMMAND 和 PLUS_COMMAND 也不应支持局部状态
        }

        logger.info("组件注册中心初始化完成")

        self._interest_calculator_registry: dict[str, type["BaseInterestCalculator"]] = {}
        """兴趣计算器名 -> 兴趣计算器类"""
        self._enabled_interest_calculator_registry: dict[str, type["BaseInterestCalculator"]] = {}
        """启用的兴趣计算器名 -> 兴趣计算器类"""

        self._prompt_registry: dict[str, type["BasePrompt"]] = {}
        """提示词组件名 -> 提示词组件类"""
        self._enabled_prompt_registry: dict[str, type["BasePrompt"]] = {}
        """启用的提示词组件名 -> 提示词组件类"""
        
        self._adapter_registry: dict[str, type["BaseAdapter"]] = {}
        """适配器组件名 -> 适配器组件类"""
        self._enabled_adapter_registry: dict[str, type["BaseAdapter"]] = {}
        """启用的适配器组件名 -> 适配器组件类"""
        
    # == 注册方法 ==

    def register_plugin(self, plugin_info: PluginInfo) -> bool:
        """
        注册一个插件。

        Args:
            plugin_info (PluginInfo): 包含插件元数据的信息对象。

        Returns:
            bool: 如果插件是新的并成功注册，则返回 True；如果插件已存在，则返回 False。
        """
        plugin_name = plugin_info.name

        if plugin_name in self._plugins:
            logger.warning(f"插件 {plugin_name} 已存在，跳过注册")
            return False

        self._plugins[plugin_name] = plugin_info
        logger.debug(f"已注册插件: {plugin_name} (组件数量: {len(plugin_info.components)})")
        return True

    def register_component(
        self, self_component_info: ComponentInfo, component_class: ComponentClassType
    ) -> bool:
        """
        注册一个组件。

        这是所有组件注册的统一入口点。它会验证组件信息，然后根据组件类型分发到
        特定的内部注册方法。

        Args:
            component_info (ComponentInfo): 组件的元数据信息。
            component_class (ComponentClassType): 组件的类定义。

        Returns:
            bool: 注册成功返回 True，否则返回 False。
        """
        component_info = self_component_info  # 创建局部别名以缩短行长
        component_name = component_info.name
        component_type = component_info.component_type
        plugin_name = getattr(component_info, "plugin_name", "unknown")

        # --- 名称合法性检查 ---
        if "." in component_name:
            logger.error(f"组件名称 '{component_name}' 包含非法字符 '.'，请使用下划线替代")
            return False
        if "." in plugin_name:
            logger.error(f"插件名称 '{plugin_name}' 包含非法字符 '.'，请使用下划线替代")
            return False

        # --- 冲突检查 ---
        namespaced_name = f"{component_type.value}.{component_name}"
        if namespaced_name in self._components:
            existing_info = self._components[namespaced_name]
            existing_plugin = getattr(existing_info, "plugin_name", "unknown")
            logger.warning(
                f"组件名冲突: '{plugin_name}' 插件的 {component_type} 类型组件 '{component_name}' "
                f"已被插件 '{existing_plugin}' 注册，跳过此组件注册"
            )
            return False

        # --- 通用注册 ---
        self._components[namespaced_name] = component_info
        self._components_by_type[component_type][component_name] = component_info
        self._components_classes[namespaced_name] = component_class

        # --- 按类型分发到特定注册方法 ---
        ret = False  # 初始化返回值为 False
        match component_type:
            case ComponentType.ACTION:
                assert isinstance(component_info, ActionInfo) and issubclass(component_class, BaseAction)
                ret = self._register_action_component(component_info, component_class)
            case ComponentType.COMMAND:
                assert isinstance(component_info, CommandInfo) and issubclass(component_class, BaseCommand)
                ret = self._register_command_component(component_info, component_class)
            case ComponentType.PLUS_COMMAND:
                assert isinstance(component_info, PlusCommandInfo) and issubclass(component_class, PlusCommand)
                ret = self._register_plus_command_component(component_info, component_class)
            case ComponentType.TOOL:
                assert isinstance(component_info, ToolInfo) and issubclass(component_class, BaseTool)
                ret = self._register_tool_component(component_info, component_class)
            case ComponentType.EVENT_HANDLER:
                assert isinstance(component_info, EventHandlerInfo) and issubclass(component_class, BaseEventHandler)
                ret = self._register_event_handler_component(component_info, component_class)
            case ComponentType.CHATTER:
                assert isinstance(component_info, ChatterInfo) and issubclass(component_class, BaseChatter)
                ret = self._register_chatter_component(component_info, component_class)
            case ComponentType.INTEREST_CALCULATOR:
                assert isinstance(component_info, InterestCalculatorInfo) and issubclass(
                    component_class, BaseInterestCalculator
                )
                ret = self._register_interest_calculator_component(component_info, component_class)
            case ComponentType.PROMPT:
                assert isinstance(component_info, PromptInfo) and issubclass(component_class, BasePrompt)
                ret = self._register_prompt_component(component_info, component_class)
            case ComponentType.ROUTER:
                assert isinstance(component_info, RouterInfo) and issubclass(component_class, BaseRouterComponent)
                ret = self._register_router_component(component_info, component_class)
            case ComponentType.ADAPTER:
                assert isinstance(component_info, AdapterInfo)
                assert issubclass(component_class, BaseAdapter)
                ret = self._register_adapter_component(component_info, component_class)
            case _:
                logger.warning(f"未知组件类型: {component_type}")
                ret = False

        if not ret:
            return False
        logger.debug(
            f"已注册{component_type}组件: '{component_name}' -> '{namespaced_name}' ({component_class.__name__}) [插件: {plugin_name}]"
        )
        return True

    def _register_action_component(self, action_info: ActionInfo, action_class: type[BaseAction]) -> bool:
        """注册Action组件到Action特定注册表"""
        if not (action_name := action_info.name):
            logger.error(f"Action组件 {action_class.__name__} 必须指定名称")
            return False
        if not isinstance(action_info, ActionInfo) or not issubclass(action_class, BaseAction):
            logger.error(f"注册失败: {action_name} 不是有效的Action")
            return False
        _assign_plugin_attrs(action_class, action_info.plugin_name, self.get_plugin_config(action_info.plugin_name) or {})
        self._action_registry[action_name] = action_class
        if action_info.enabled:
            self._default_actions[action_name] = action_info
        return True

    def _register_command_component(self, command_info: CommandInfo, command_class: type[BaseCommand]) -> bool:
        """注册Command组件到Command特定注册表"""
        logger.warning(
                f"检测到旧版Command组件 '{command_class.command_name}' (来自插件: {command_info.plugin_name})。"
                "它将通过兼容层运行，但建议尽快迁移到PlusCommand以获得更好的性能和功能。"
            )
        # 使用适配器将其转换为PlusCommand
        adapted_class = create_legacy_command_adapter(command_class)
        plus_command_info = adapted_class.get_plus_command_info()
        plus_command_info.plugin_name = command_info.plugin_name  # 继承插件名

        return self._register_plus_command_component(plus_command_info, adapted_class)

    def _register_plus_command_component(
        self, plus_command_info: PlusCommandInfo, plus_command_class: type[PlusCommand]
    ) -> bool:
        """注册PlusCommand组件到特定注册表"""
        plus_command_name = plus_command_info.name

        if not plus_command_name:
            logger.error(f"PlusCommand组件 {plus_command_class.__name__} 必须指定名称")
            return False
        if not isinstance(plus_command_info, PlusCommandInfo) or not issubclass(plus_command_class, PlusCommand):
            logger.error(f"注册失败: {plus_command_name} 不是有效的PlusCommand")
            return False

        # 创建专门的PlusCommand注册表（如果还没有）
        if not hasattr(self, "_plus_command_registry"):
            self._plus_command_registry: dict[str, type[PlusCommand]] = {}
        _assign_plugin_attrs(
            plus_command_class,
            plus_command_info.plugin_name,
            self.get_plugin_config(plus_command_info.plugin_name) or {},
        )
        self._plus_command_registry[plus_command_name] = plus_command_class
        logger.debug(f"已注册PlusCommand组件: {plus_command_name}")
        return True

    def _register_tool_component(self, tool_info: ToolInfo, tool_class: type[BaseTool]) -> bool:
        """注册Tool组件到Tool特定注册表"""
        tool_name = tool_info.name
        _assign_plugin_attrs(tool_class, tool_info.plugin_name, self.get_plugin_config(tool_info.plugin_name) or {})
        self._tool_registry[tool_name] = tool_class
        if tool_info.enabled:
            self._llm_available_tools[tool_name] = tool_class
        return True

    def _register_event_handler_component(
        self, handler_info: EventHandlerInfo, handler_class: type[BaseEventHandler]
    ) -> bool:
        if not (handler_name := handler_info.name):
            logger.error(f"EventHandler组件 {handler_class.__name__} 必须指定名称")
            return False
        if not isinstance(handler_info, EventHandlerInfo) or not issubclass(handler_class, BaseEventHandler):
            logger.error(f"注册失败: {handler_name} 不是有效的EventHandler")
            return False
        _assign_plugin_attrs(
            handler_class, handler_info.plugin_name, self.get_plugin_config(handler_info.plugin_name) or {}
        )
        self._event_handler_registry[handler_name] = handler_class
        if not handler_info.enabled:
            logger.warning(f"EventHandler组件 {handler_name} 未启用")
            return True  # 未启用，但是也是注册成功
        from src.plugin_system.core.event_manager import event_manager
        return event_manager.register_event_handler(
            handler_class, self.get_plugin_config(handler_info.plugin_name) or {}
        )

    def _register_chatter_component(self, chatter_info: ChatterInfo, chatter_class: type[BaseChatter]) -> bool:
        """注册Chatter组件到Chatter特定注册表"""
        chatter_name = chatter_info.name

        if not chatter_name:
            logger.error(f"Chatter组件 {chatter_class.__name__} 必须指定名称")
            return False
        if not isinstance(chatter_info, ChatterInfo) or not issubclass(chatter_class, BaseChatter):
            logger.error(f"注册失败: {chatter_name} 不是有效的Chatter")
            return False
        _assign_plugin_attrs(
            chatter_class, chatter_info.plugin_name, self.get_plugin_config(chatter_info.plugin_name) or {}
        )
        self._chatter_registry[chatter_name] = chatter_class
        if not chatter_info.enabled:
            logger.warning(f"Chatter组件 {chatter_name} 未启用")
            return True  # 未启用，但是也是注册成功
        self._enabled_chatter_registry[chatter_name] = chatter_class
        logger.debug(f"已注册Chatter组件: {chatter_name}")
        return True

    def _register_interest_calculator_component(
        self,
        interest_calculator_info: "InterestCalculatorInfo",
        interest_calculator_class: type["BaseInterestCalculator"],
    ) -> bool:
        """注册InterestCalculator组件到特定注册表"""
        calculator_name = interest_calculator_info.name

        if not calculator_name:
            logger.error(f"InterestCalculator组件 {interest_calculator_class.__name__} 必须指定名称")
            return False
        if not isinstance(interest_calculator_info, InterestCalculatorInfo) or not issubclass(
            interest_calculator_class, BaseInterestCalculator
        ):
            logger.error(f"注册失败: {calculator_name} 不是有效的InterestCalculator")
            return False

        _assign_plugin_attrs(
            interest_calculator_class,
            interest_calculator_info.plugin_name,
            self.get_plugin_config(interest_calculator_info.plugin_name) or {},
        )
        self._interest_calculator_registry[calculator_name] = interest_calculator_class

        if not interest_calculator_info.enabled:
            logger.warning(f"InterestCalculator组件 {calculator_name} 未启用")
            return True  # 未启用，但是也是注册成功
        self._enabled_interest_calculator_registry[calculator_name] = interest_calculator_class

        logger.debug(f"已注册InterestCalculator组件: {calculator_name}")
        return True

    def _register_prompt_component(
        self, prompt_info: PromptInfo, prompt_class: "ComponentClassType"
    ) -> bool:
        """注册Prompt组件到Prompt特定注册表"""
        prompt_name = prompt_info.name
        if not prompt_name:
            logger.error(f"Prompt组件 {prompt_class.__name__} 必须指定名称")
            return False

        _assign_plugin_attrs(
            prompt_class, prompt_info.plugin_name, self.get_plugin_config(prompt_info.plugin_name) or {}
        )
        self._prompt_registry[prompt_name] = prompt_class  # type: ignore

        if prompt_info.enabled:
            self._enabled_prompt_registry[prompt_name] = prompt_class  # type: ignore

        logger.debug(f"已注册Prompt组件: {prompt_name}")
        return True

    def _register_router_component(self, router_info: RouterInfo, router_class: type[BaseRouterComponent]) -> bool:
        """注册Router组件并将其端点挂载到主服务器"""
        # 1. 检查总开关是否开启
        if not bot_config.plugin_http_system.enable_plugin_http_endpoints:
            logger.info("插件HTTP端点功能已禁用，跳过路由注册")
            return True
        try:
            from src.common.server import get_global_server

            router_name = router_info.name
            plugin_name = router_info.plugin_name
            plugin_config = self.get_plugin_config(plugin_name) or {}
            _assign_plugin_attrs(router_class, plugin_name, plugin_config)

            # 2. 实例化组件以触发其 __init__ 和 register_endpoints
            component_instance = router_class()

            # 3. 获取配置好的 APIRouter
            plugin_router = component_instance.router

            # 4. 获取全局服务器实例
            server = get_global_server()

            # 5. 生成唯一的URL前缀
            prefix = f"/plugins/{plugin_name}"

            # 6. 注册路由，并使用插件名作为API文档的分组标签
            # 移除了dependencies参数，因为现在由每个端点自行决定是否需要验证
            server.app.include_router(
                plugin_router, prefix=prefix, tags=[plugin_name]
            )

            logger.debug(f"成功将插件 '{plugin_name}' 的路由组件 '{router_name}' 挂载到: {prefix}")
            return True

        except Exception as e:
            logger.error(f"注册路由组件 '{router_info.name}' 时出错: {e}")
            return False

    def _register_adapter_component(self, adapter_info: AdapterInfo, adapter_class: type[BaseAdapter]) -> bool:
        """将Adapter组件注册到Adapter特定注册表"""

        adapter_name = adapter_info.name
        if not adapter_name:
            logger.error(f"Adapter组件 {adapter_class.__name__} 必须指定名称")
            return False
        if not isinstance(adapter_info, AdapterInfo) or not issubclass(adapter_class, BaseAdapter):
            logger.error(f"注册失败: {adapter_name} 不是有效的Adapter")
            return False

        _assign_plugin_attrs(
            adapter_class, adapter_info.plugin_name, self.get_plugin_config(adapter_info.plugin_name) or {}
        )
        if not hasattr(self, "_adapter_registry"):
            self._adapter_registry: dict[str, type[BaseAdapter]] = {}

        self._adapter_registry[adapter_name] = adapter_class

        if not adapter_info.enabled:
            logger.warning(f"Adapter {adapter_name} 未启用")
        return True

    # === 组件移除相关 ===

    async def remove_component(self, component_name: str, component_type: ComponentType, plugin_name: str) -> bool:
        target_component_class = self.get_component_class(component_name, component_type)
        if not target_component_class:
            logger.warning(f"组件 {component_name} 未注册，无法移除")
            return False
        try:
            # 根据组件类型进行特定的清理操作
            match component_type:
                case ComponentType.ACTION:
                    # 移除Action注册
                    self._action_registry.pop(component_name, None)
                    self._default_actions.pop(component_name, None)
                    logger.debug(f"已移除Action组件: {component_name}")

                case ComponentType.COMMAND:
                    # 移除Command注册和模式
                    self._command_registry.pop(component_name, None)
                    keys_to_remove = [k for k, v in self._command_patterns.items() if v == component_name]
                    for key in keys_to_remove:
                        self._command_patterns.pop(key, None)
                    logger.debug(f"已移除Command组件: {component_name} (清理了 {len(keys_to_remove)} 个模式)")

                case ComponentType.PLUS_COMMAND:
                    # 移除PlusCommand注册
                    if hasattr(self, "_plus_command_registry"):
                        self._plus_command_registry.pop(component_name, None)
                    logger.debug(f"已移除PlusCommand组件: {component_name}")

                case ComponentType.TOOL:
                    # 移除Tool注册
                    self._tool_registry.pop(component_name, None)
                    self._llm_available_tools.pop(component_name, None)
                    logger.debug(f"已移除Tool组件: {component_name}")

                case ComponentType.EVENT_HANDLER:
                    # 移除EventHandler注册和事件订阅
                    from .event_manager import event_manager  # 延迟导入防止循环导入问题

                    self._event_handler_registry.pop(component_name, None)
                    self._enabled_event_handlers.pop(component_name, None)
                    try:
                        handler = event_manager.get_event_handler(component_name)
                        # 事件处理器可能未找到或未声明 subscribed_events，需判空
                        if handler and hasattr(handler, "subscribed_events"):
                            for event in getattr(handler, "subscribed_events"):
                                # 假设 unsubscribe_handler_from_event 是协程；若不是则移除 await
                                result = event_manager.unsubscribe_handler_from_event(event, component_name)
                                if hasattr(result, "__await__"):
                                    await result  # type: ignore[func-returns-value]
                        logger.debug(f"已移除EventHandler组件: {component_name}")
                        logger.debug(f"已移除EventHandler组件: {component_name}")
                    except Exception as e:
                        logger.warning(f"移除EventHandler事件订阅时出错: {e}")

                case ComponentType.CHATTER:
                    # 移除Chatter注册
                    if hasattr(self, "_chatter_registry"):
                        self._chatter_registry.pop(component_name, None)
                    logger.debug(f"已移除Chatter组件: {component_name}")

                case _:
                    logger.warning(f"未知的组件类型: {component_type}")
                    return False

            # 移除通用注册信息
            namespaced_name = f"{component_type.value}.{component_name}"
            self._components.pop(namespaced_name, None)
            self._components_by_type[component_type].pop(component_name, None)
            self._components_classes.pop(namespaced_name, None)
            return False

        logger.debug(
            f"已注册{component_type}组件: '{component_name}' -> '{namespaced_name}' "
            f"({component_class.__name__}) [插件: {plugin_name}]"
        )
        return True

    # === 组件全局启用/禁用方法 ===

    def enable_component(self, component_name: str, component_type: ComponentType) -> bool:
        """全局的启用某个组件
        Parameters:
            component_name: 组件名称
            component_type: 组件类型
        Returns:
            bool: 启用成功返回True，失败返回False
        """
        target_component_class = self.get_component_class(component_name, component_type)
        target_component_info = self.get_component_info(component_name, component_type)
        if not target_component_class or not target_component_info:
            logger.warning(f"组件 {component_name} 未注册，无法启用")
            return False
        target_component_info.enabled = True
        match component_type:
            case ComponentType.ACTION:
                assert isinstance(target_component_info, ActionInfo)
                self._default_actions[component_name] = target_component_info
            case ComponentType.COMMAND:
                assert isinstance(target_component_info, CommandInfo)
                pattern = target_component_info.command_pattern
                self._command_patterns[re.compile(pattern)] = component_name
            case ComponentType.TOOL:
                assert isinstance(target_component_info, ToolInfo)
                assert issubclass(target_component_class, BaseTool)
                self._llm_available_tools[component_name] = target_component_class
            case ComponentType.EVENT_HANDLER:
                assert isinstance(target_component_info, EventHandlerInfo)
                assert issubclass(target_component_class, BaseEventHandler)
                self._enabled_event_handlers[component_name] = target_component_class
                from .event_manager import event_manager  # 延迟导入防止循环导入问题

                # 重新注册事件处理器（启用）使用类而不是名称
                cfg = self.get_plugin_config(target_component_info.plugin_name) or {}
                event_manager.register_event_handler(target_component_class, cfg)  # type: ignore[arg-type]
        namespaced_name = f"{component_type.value}.{component_name}"
        self._components[namespaced_name].enabled = True
        self._components_by_type[component_type][component_name].enabled = True
        logger.info(f"组件 {component_name} 已启用")
        return True

    async def disable_component(self, component_name: str, component_type: ComponentType) -> bool:
        """全局的禁用某个组件
        Parameters:
            component_name: 组件名称
            component_type: 组件类型
        Returns:
            bool: 禁用成功返回True，失败返回False
        """
        target_component_class = self.get_component_class(component_name, component_type)
        target_component_info = self.get_component_info(component_name, component_type)
        if not target_component_class or not target_component_info:
            logger.warning(f"组件 {component_name} 未注册，无法禁用")
            return False
        target_component_info.enabled = False
        try:
            match component_type:
                case ComponentType.ACTION:
                    self._default_actions.pop(component_name)
                case ComponentType.COMMAND:
                    self._command_patterns = {k: v for k, v in self._command_patterns.items() if v != component_name}
                case ComponentType.TOOL:
                    self._llm_available_tools.pop(component_name)
                case ComponentType.EVENT_HANDLER:
                    self._enabled_event_handlers.pop(component_name)
                    from .event_manager import event_manager  # 延迟导入防止循环导入问题

                    handler = event_manager.get_event_handler(component_name)
                    if handler and hasattr(handler, "subscribed_events"):
                        for event in getattr(handler, "subscribed_events"):
                            result = event_manager.unsubscribe_handler_from_event(event, component_name)
                            if hasattr(result, "__await__"):
                                await result  # type: ignore[func-returns-value]

            # 组件主注册表使用命名空间 key
            namespaced_name = f"{component_type.value}.{component_name}"
            if namespaced_name in self._components:
                self._components[namespaced_name].enabled = False
            self._components_by_type[component_type][component_name].enabled = False
            logger.info(f"组件 {component_name} 已禁用")
            return True
        except KeyError as e:
            logger.warning(f"禁用组件时未找到组件或已禁用: {component_name}, 发生错误: {e}")
            return False
        except Exception as e:
            logger.error(f"禁用组件 {component_name} 时发生错误: {e}")
            return False

    # === 组件查询方法 ===
    def get_component_info(
        self, component_name: str, component_type: ComponentType | None = None
    ) -> ComponentInfo | None:
        # sourcery skip: class-extract-method
        """获取组件信息，支持自动命名空间解析

        Args:
            component_name: 组件名称，可以是原始名称或命名空间化的名称
            component_type: 组件类型，如果提供则优先在该类型中查找

        Returns:
            Optional[ComponentInfo]: 组件信息或None
        """
        # 1. 如果已经是命名空间化的名称，直接查找
        if "." in component_name:
            return self._components.get(component_name)

        # 2. 如果指定了组件类型，构造命名空间化的名称查找
        if component_type:
            namespaced_name = f"{component_type.value}.{component_name}"
            return self._components.get(namespaced_name)

        # 3. 如果没有指定类型，尝试在所有命名空间中查找
        candidates = []
        for namespace_prefix in [types.value for types in ComponentType]:
            namespaced_name = f"{namespace_prefix}.{component_name}"
            if component_info := self._components.get(namespaced_name):
                candidates.append((namespace_prefix, namespaced_name, component_info))

        if len(candidates) == 1:
            # 只有一个匹配，直接返回
            return candidates[0][2]
        elif len(candidates) > 1:
            # 多个匹配，记录警告并返回第一个
            namespaces = [ns for ns, _, _ in candidates]
            logger.warning(
                f"组件名称 '{component_name}' 在多个命名空间中存在: {namespaces}，使用第一个匹配项: {candidates[0][1]}"
            )
            return candidates[0][2]

        # 4. 都没找到
        return None

    def get_component_class(
        self,
        component_name: str,
        component_type: ComponentType | None = None,
    ) -> (
        type[
            BaseCommand
            | BaseAction
            | BaseEventHandler
            | BaseTool
            | PlusCommand
            | BaseChatter
            | BaseInterestCalculator
            | BasePrompt
            | BaseRouterComponent
            | BaseAdapter
        ]
        | None
    ):
        """获取组件类，支持自动命名空间解析

        Args:
            component_name: 组件名称，可以是原始名称或命名空间化的名称
            component_type: 组件类型，如果提供则优先在该类型中查找

        Returns:
            Optional[Union[BaseCommand, BaseAction]]: 组件类或None
        """
        # 1. 如果已经是命名空间化的名称，直接查找
        if "." in component_name:
            return self._components_classes.get(component_name)

        # 2. 如果指定了组件类型，构造命名空间化的名称查找
        if component_type:
            namespaced_name = f"{component_type.value}.{component_name}"
            return cast(
                type[BaseCommand]
                | type[BaseAction]
                | type[BaseEventHandler]
                | type[BaseTool]
                | type[PlusCommand]
                | type[BaseChatter]
                | type[BaseInterestCalculator]
                | type[BasePrompt]
                | type[BaseRouterComponent]
                | type[BaseAdapter]
                | None,
                self._components_classes.get(namespaced_name),
            )

        # 3. 如果没有指定类型，尝试在所有命名空间中查找
        candidates = []
        for namespace_prefix in [types.value for types in ComponentType]:
            namespaced_name = f"{namespace_prefix}.{component_name}"
            if component_class := self._components_classes.get(namespaced_name):
                candidates.append((namespace_prefix, namespaced_name, component_class))

        if len(candidates) == 1:
            # 只有一个匹配，直接返回
            _, full_name, cls = candidates[0]
            logger.debug(f"自动解析组件: '{component_name}' -> '{full_name}'")
            return cls
        elif len(candidates) > 1:
            # 多个匹配，记录警告并返回第一个
            namespaces = [ns for ns, _, _ in candidates]
            logger.warning(
                f"组件名称 '{component_name}' 在多个命名空间中存在: {namespaces}，使用第一个匹配项: {candidates[0][1]}"
            )
            return candidates[0][2]

        # 4. 都没找到
        return None

    def get_components_by_type(self, component_type: ComponentType) -> dict[str, ComponentInfo]:
        """获取指定类型的所有组件"""
        return self._components_by_type.get(component_type, {}).copy()

    def get_enabled_components_by_type(self, component_type: ComponentType) -> dict[str, ComponentInfo]:
        """获取指定类型的所有启用组件"""
        components = self.get_components_by_type(component_type)
        return {name: info for name, info in components.items() if info.enabled}

    # === Action特定查询方法 ===

    def get_action_registry(self) -> dict[str, type[BaseAction]]:
        """获取Action注册表"""
        return self._action_registry.copy()

    def get_registered_action_info(self, action_name: str) -> ActionInfo | None:
        """获取Action信息"""
        info = self.get_component_info(action_name, ComponentType.ACTION)
        return info if isinstance(info, ActionInfo) else None

    def get_default_actions(self) -> dict[str, ActionInfo]:
        """获取默认动作集"""
        return self._default_actions.copy()

    # === Command特定查询方法 ===

    def get_command_registry(self) -> dict[str, type[BaseCommand]]:
        """获取Command注册表"""
        return self._command_registry.copy()

    def get_registered_command_info(self, command_name: str) -> CommandInfo | None:
        """获取Command信息"""
        info = self.get_component_info(command_name, ComponentType.COMMAND)
        return info if isinstance(info, CommandInfo) else None

    def get_command_patterns(self) -> dict[Pattern, str]:
        """获取Command模式注册表"""
        return self._command_patterns.copy()

    def find_command_by_text(self, text: str) -> tuple[type[BaseCommand], dict, CommandInfo] | None:
        # sourcery skip: use-named-expression, use-next
        """根据文本查找匹配的命令

        Args:
            text: 输入文本

        Returns:
            Tuple: (命令类, 匹配的命名组, 命令信息) 或 None
        """

        # 只查找传统的BaseCommand
        candidates = [pattern for pattern in self._command_patterns if pattern.match(text)]
        if candidates:
            if len(candidates) > 1:
                logger.warning(f"文本 '{text}' 匹配到多个命令模式: {candidates}，使用第一个匹配")
            command_name = self._command_patterns[candidates[0]]
            command_info: CommandInfo = self.get_registered_command_info(command_name)  # type: ignore
            return (
                self._command_registry[command_name],
                candidates[0].match(text).groupdict(),  # type: ignore
                command_info,
            )

        return None

    # === Tool 特定查询方法 ===
    def get_tool_registry(self) -> dict[str, type[BaseTool]]:
        """获取Tool注册表"""
        return self._tool_registry.copy()

    def get_llm_available_tools(self) -> dict[str, type[BaseTool]]:
        """获取LLM可用的Tool列表"""
        return self._llm_available_tools.copy()

    def get_registered_tool_info(self, tool_name: str) -> ToolInfo | None:
        """获取Tool信息

        Args:
            tool_name: 工具名称

        Returns:
            ToolInfo: 工具信息对象，如果工具不存在则返回 None
        """
        info = self.get_component_info(tool_name, ComponentType.TOOL)
        return info if isinstance(info, ToolInfo) else None

    # === PlusCommand 特定查询方法 ===
    def get_plus_command_registry(self) -> dict[str, type[PlusCommand]]:
        """获取PlusCommand注册表"""
        if not hasattr(self, "_plus_command_registry"):
            self._plus_command_registry: dict[str, type[PlusCommand]] = {}
        return self._plus_command_registry.copy()

    def get_registered_plus_command_info(self, command_name: str) -> PlusCommandInfo | None:
        """获取PlusCommand信息

        Args:
            command_name: 命令名称

        Returns:
            PlusCommandInfo: 命令信息对象，如果命令不存在则返回 None
        """
        info = self.get_component_info(command_name, ComponentType.PLUS_COMMAND)
        return info if isinstance(info, PlusCommandInfo) else None

    # === EventHandler 特定查询方法 ===

    def get_event_handler_registry(self) -> dict[str, type[BaseEventHandler]]:
        """获取事件处理器注册表"""
        return self._event_handler_registry.copy()

    def get_registered_event_handler_info(self, handler_name: str) -> EventHandlerInfo | None:
        """获取事件处理器信息"""
        info = self.get_component_info(handler_name, ComponentType.EVENT_HANDLER)
        return info if isinstance(info, EventHandlerInfo) else None

    def get_enabled_event_handlers(self) -> dict[str, type[BaseEventHandler]]:
        """获取启用的事件处理器"""
        return self._enabled_event_handlers.copy()

    # === Chatter 特定查询方法 ===
    def get_chatter_registry(self) -> dict[str, type[BaseChatter]]:
        """获取Chatter注册表"""
        if not hasattr(self, "_chatter_registry"):
            self._chatter_registry: dict[str, type[BaseChatter]] = {}
        return self._chatter_registry.copy()

    def get_enabled_chatter_registry(self) -> dict[str, type[BaseChatter]]:
        """获取启用的Chatter注册表"""
        if not hasattr(self, "_enabled_chatter_registry"):
            self._enabled_chatter_registry: dict[str, type[BaseChatter]] = {}
        return self._enabled_chatter_registry.copy()

    def get_registered_chatter_info(self, chatter_name: str) -> ChatterInfo | None:
        """获取Chatter信息"""
        info = self.get_component_info(chatter_name, ComponentType.CHATTER)
        return info if isinstance(info, ChatterInfo) else None

    # === 插件查询方法 ===

    def get_plugin_info(self, plugin_name: str) -> PluginInfo | None:
        """获取插件信息"""
        return self._plugins.get(plugin_name)

    def get_all_plugins(self) -> dict[str, PluginInfo]:
        """获取所有插件"""
        return self._plugins.copy()

    # def get_enabled_plugins(self) -> Dict[str, PluginInfo]:
    #     """获取所有启用的插件"""
    #     return {name: info for name, info in self._plugins.items() if info.enabled}

    def get_plugin_components(self, plugin_name: str) -> list["ComponentInfo"]:
        """获取插件的所有组件"""
        plugin_info = self.get_plugin_info(plugin_name)
        logger.info(plugin_info.components)
        return plugin_info.components if plugin_info else []

    def get_plugin_config(self, plugin_name: str) -> dict:
        """获取插件配置

        Args:
            plugin_name: 插件名称

        Returns:
            dict: 插件配置字典，如果插件实例不存在或配置为空，返回空字典
        """
        # 从插件管理器获取插件实例的配置
        from src.plugin_system.core.plugin_manager import plugin_manager

        plugin_instance = plugin_manager.get_plugin_instance(plugin_name)
        if plugin_instance and plugin_instance.config:
            return plugin_instance.config

        # 如果插件实例不存在，尝试从配置文件读取
        try:
            import toml

            config_path = Path("config") / "plugins" / plugin_name / "config.toml"
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    config_data = toml.load(f)
                    logger.debug(f"从配置文件读取插件 {plugin_name} 的配置")
                    return config_data
        except Exception as e:
            logger.debug(f"读取插件 {plugin_name} 配置文件失败: {e}")

        return {}

    def get_registry_stats(self) -> dict[str, Any]:
        """获取注册中心统计信息"""
        action_components: int = 0
        command_components: int = 0
        tool_components: int = 0
        events_handlers: int = 0
        plus_command_components: int = 0
        chatter_components: int = 0
        prompt_components: int = 0
        router_components: int = 0
        adapter_components: int = 0
        for component in self._components.values():
            if component.component_type == ComponentType.ACTION:
                action_components += 1
            elif component.component_type == ComponentType.COMMAND:
                command_components += 1
            elif component.component_type == ComponentType.TOOL:
                tool_components += 1
            elif component.component_type == ComponentType.EVENT_HANDLER:
                events_handlers += 1
            elif component.component_type == ComponentType.PLUS_COMMAND:
                plus_command_components += 1
            elif component.component_type == ComponentType.CHATTER:
                chatter_components += 1
            elif component.component_type == ComponentType.PROMPT:
                prompt_components += 1
            elif component.component_type == ComponentType.ROUTER:
                router_components += 1
            elif component.component_type == ComponentType.ADAPTER:
                adapter_components += 1
        return {
            "action_components": action_components,
            "command_components": command_components,
            "tool_components": tool_components,
            "mcp_tools": len(self._mcp_tools),
            "event_handlers": events_handlers,
            "plus_command_components": plus_command_components,
            "chatter_components": chatter_components,
            "prompt_components": prompt_components,
            "router_components": router_components,
            "adapter_components": adapter_components,
            "total_components": len(self._components),
            "total_plugins": len(self._plugins),
            "components_by_type": {
                component_type.value: len(components) for component_type, components in self._components_by_type.items()
            },
            "enabled_components": len([c for c in self._components.values() if c.enabled]),
            "enabled_plugins": len([p for p in self._plugins.values() if p.enabled]),
        }

    # === MCP 工具相关方法 ===

    async def load_mcp_tools(self) -> None:
        """加载 MCP 工具（异步方法）"""
        if self._mcp_tools_loaded:
            logger.debug("MCP 工具已加载，跳过")
            return

        try:
            from .mcp_tool_adapter import load_mcp_tools_as_adapters

            logger.info("开始加载 MCP 工具...")
            self._mcp_tools = await load_mcp_tools_as_adapters()
            self._mcp_tools_loaded = True
            logger.info(f"MCP 工具加载完成，共 {len(self._mcp_tools)} 个工具")
        except Exception as e:
            logger.error(f"加载 MCP 工具失败: {e}")
            self._mcp_tools = []
            self._mcp_tools_loaded = True  # 标记为已尝试加载，避免重复尝试

    def get_mcp_tools(self) -> list["BaseTool"]:
        """获取所有 MCP 工具适配器实例"""
        return self._mcp_tools.copy()

    def is_mcp_tool(self, tool_name: str) -> bool:
        """检查工具名是否为 MCP 工具"""
        return tool_name.startswith("mcp_")

    # === 组件移除相关 ===

    async def unregister_plugin(self, plugin_name: str) -> bool:
        """
        卸载一个插件及其所有关联的组件。

        这是一个高级操作，会依次移除插件的所有组件，然后移除插件本身的注册信息。

        Args:
            plugin_name (str): 要卸载的插件的名称。

        Returns:
            bool: 如果所有组件和插件本身都成功卸载，则返回 True；否则返回 False。
        """
        plugin_info = self.get_plugin_info(plugin_name)
        if not plugin_info:
            logger.warning(f"插件 {plugin_name} 未注册，无法卸载")
            return False

        logger.info(f"开始卸载插件: {plugin_name}")

        failed_components = []
        # 逐个移除插件的所有组件
        for component_info in plugin_info.components:
            try:
                success = await self.remove_component(
                    component_info.name,
                    component_info.component_type,
                    plugin_name,
                )
                if not success:
                    failed_components.append(f"{component_info.component_type}.{component_info.name}")
            except Exception as e:
                logger.error(f"移除组件 {component_info.name} 时发生异常: {e}")
                failed_components.append(f"{component_info.component_type}.{component_info.name}")

        # 移除插件的注册信息
        plugin_removed = self._remove_plugin_registry(plugin_name)

        if failed_components:
            logger.warning(f"插件 {plugin_name} 部分组件卸载失败: {failed_components}")
            return False
        if not plugin_removed:
            logger.error(f"插件 {plugin_name} 注册信息移除失败")
            return False

        logger.info(f"插件 {plugin_name} 卸载成功")
        return True

    async def remove_component(self, component_name: str, component_type: ComponentType, plugin_name: str) -> bool:
        """
        从注册中心移除一个指定的组件。

        Args:
            component_name (str): 要移除的组件的名称。
            component_type (ComponentType): 组件的类型。
            plugin_name (str): 组件所属的插件名称 (用于日志和验证)。

        Returns:
            bool: 移除成功返回 True，否则返回 False。
        """
        target_component_class = self.get_component_class(component_name, component_type)
        if not target_component_class:
            logger.warning(f"组件 {component_name} ({component_type.value}) 未注册，无法移除")
            return False

        try:
            # --- 特定类型的清理操作 ---
            match component_type:
                case ComponentType.ACTION:
                    self._action_registry.pop(component_name, None)
                    self._default_actions.pop(component_name, None)
                case ComponentType.COMMAND:
                    self._command_registry.pop(component_name, None)
                    keys_to_remove = [k for k, v in self._command_patterns.items() if v == component_name]
                    for key in keys_to_remove:
                        self._command_patterns.pop(key, None)
                case ComponentType.PLUS_COMMAND:
                    self._plus_command_registry.pop(component_name, None)
                case ComponentType.TOOL:
                    self._tool_registry.pop(component_name, None)
                    self._llm_available_tools.pop(component_name, None)
                case ComponentType.EVENT_HANDLER:
                    from .event_manager import event_manager  # 延迟导入
                    event_manager.remove_event_handler(component_name)
                case ComponentType.CHATTER:
                    self._chatter_registry.pop(component_name, None)
                    self._enabled_chatter_registry.pop(component_name, None)
                case ComponentType.INTEREST_CALCULATOR:
                    self._interest_calculator_registry.pop(component_name, None)
                    self._enabled_interest_calculator_registry.pop(component_name, None)
                case ComponentType.PROMPT:
                    self._prompt_registry.pop(component_name, None)
                    self._enabled_prompt_registry.pop(component_name, None)
                case ComponentType.ROUTER:
                    logger.warning(f"Router组件 '{component_name}' 的HTTP端点无法在运行时动态移除，将在下次重启后生效。")
                case _:
                    logger.warning(f"未知的组件类型: {component_type}，无法进行特定的清理操作")
                    return False

            # --- 通用注册信息的清理 ---
            namespaced_name = f"{component_type.value}.{component_name}"
            self._components.pop(namespaced_name, None)
            self._components_by_type[component_type].pop(component_name, None)
            self._components_classes.pop(namespaced_name, None)

            logger.info(f"组件 {component_name} ({component_type.value}) 已从插件 '{plugin_name}' 中完全移除")
            return True

        except Exception as e:
            logger.error(f"移除组件 {component_name} ({component_type.value}) 时发生错误: {e}", exc_info=True)
            return False

    # =================================================================
    # == 内部注册辅助方法 (_register_* Methods)
    # =================================================================

    def _register_action_component(self, action_info: ActionInfo, action_class: type[BaseAction]) -> bool:
        """注册Action组件到Action特定注册表。"""
        action_name = action_info.name
        _assign_plugin_attrs(action_class, action_info.plugin_name, self.get_plugin_config(action_info.plugin_name) or {})
        self._action_registry[action_name] = action_class
        if action_info.enabled:
            self._default_actions[action_name] = action_info
        return True

    def _register_command_component(self, command_info: CommandInfo, command_class: type[BaseCommand]) -> bool:
        """通过适配器将旧版Command注册为PlusCommand。"""
        logger.warning(
            f"检测到旧版Command组件 '{command_info.name}' (来自插件: {command_info.plugin_name})。"
            "它将通过兼容层运行，但建议尽快迁移到PlusCommand以获得更好的性能和功能。"
        )
        # 使用适配器将其转换为PlusCommand
        adapted_class = create_legacy_command_adapter(command_class)
        plus_command_info = adapted_class.get_plus_command_info()
        plus_command_info.plugin_name = command_info.plugin_name  # 继承插件名

        return self._register_plus_command_component(plus_command_info, adapted_class)

    def _register_plus_command_component(
        self, plus_command_info: PlusCommandInfo, plus_command_class: type[PlusCommand]
    ) -> bool:
        """注册PlusCommand组件到特定注册表。"""
        plus_command_name = plus_command_info.name
        _assign_plugin_attrs(
            plus_command_class,
            plus_command_info.plugin_name,
            self.get_plugin_config(plus_command_info.plugin_name) or {},
        )
        self._plus_command_registry[plus_command_name] = plus_command_class
        logger.debug(f"已注册PlusCommand组件: {plus_command_name}")
        return True

    def _register_tool_component(self, tool_info: ToolInfo, tool_class: type[BaseTool]) -> bool:
        """注册Tool组件到Tool特定注册表。"""
        tool_name = tool_info.name
        _assign_plugin_attrs(tool_class, tool_info.plugin_name, self.get_plugin_config(tool_info.plugin_name) or {})
        self._tool_registry[tool_name] = tool_class
        if tool_info.enabled:
            self._llm_available_tools[tool_name] = tool_class
        return True

    def _register_event_handler_component(
        self, handler_info: EventHandlerInfo, handler_class: type[BaseEventHandler]
    ) -> bool:
        """注册EventHandler组件并订阅事件。"""
        handler_name = handler_info.name
        _assign_plugin_attrs(
            handler_class, handler_info.plugin_name, self.get_plugin_config(handler_info.plugin_name) or {}
        )
        self._event_handler_registry[handler_name] = handler_class
        if not handler_info.enabled:
            logger.warning(f"EventHandler组件 {handler_name} 未启用，仅注册信息，不订阅事件")
            return True  # 未启用但注册成功

        # 延迟导入以避免循环依赖
        from src.plugin_system.core.event_manager import event_manager
        return event_manager.register_event_handler(
            handler_class, self.get_plugin_config(handler_info.plugin_name) or {}
        )

    def _register_chatter_component(self, chatter_info: ChatterInfo, chatter_class: type[BaseChatter]) -> bool:
        """注册Chatter组件到Chatter特定注册表。"""
        chatter_name = chatter_info.name
        _assign_plugin_attrs(
            chatter_class, chatter_info.plugin_name, self.get_plugin_config(chatter_info.plugin_name) or {}
        )
        self._chatter_registry[chatter_name] = chatter_class
        if chatter_info.enabled:
            self._enabled_chatter_registry[chatter_name] = chatter_class
        logger.debug(f"已注册Chatter组件: {chatter_name}")
        return True

    def _register_interest_calculator_component(
        self,
        interest_calculator_info: "InterestCalculatorInfo",
        interest_calculator_class: type["BaseInterestCalculator"],
    ) -> bool:
        """注册InterestCalculator组件到特定注册表。"""
        calculator_name = interest_calculator_info.name
        _assign_plugin_attrs(
            interest_calculator_class,
            interest_calculator_info.plugin_name,
            self.get_plugin_config(interest_calculator_info.plugin_name) or {},
        )
        self._interest_calculator_registry[calculator_name] = interest_calculator_class
        if interest_calculator_info.enabled:
            self._enabled_interest_calculator_registry[calculator_name] = interest_calculator_class
        logger.debug(f"已注册InterestCalculator组件: {calculator_name}")
        return True

    def _register_prompt_component(self, prompt_info: PromptInfo, prompt_class: type[BasePrompt]) -> bool:
        """注册Prompt组件到Prompt特定注册表。"""
        prompt_name = prompt_info.name
        _assign_plugin_attrs(
            prompt_class, prompt_info.plugin_name, self.get_plugin_config(prompt_info.plugin_name) or {}
        )
        self._prompt_registry[prompt_name] = prompt_class
        if prompt_info.enabled:
            self._enabled_prompt_registry[prompt_name] = prompt_class
        logger.debug(f"已注册Prompt组件: {prompt_name}")
        return True

    def _register_router_component(self, router_info: RouterInfo, router_class: type[BaseRouterComponent]) -> bool:
        """注册Router组件并将其HTTP端点挂载到主FastAPI应用。"""
        if not bot_config.plugin_http_system.enable_plugin_http_endpoints:
            logger.info("插件HTTP端点功能已禁用，跳过路由注册")
            return True

        try:
            from src.common.server import get_global_server

            router_name = router_info.name
            plugin_name = router_info.plugin_name

            # 实例化组件以获取其配置好的APIRouter实例
            component_instance = router_class()
            plugin_router = component_instance.router

            # 获取全局FastAPI应用实例
            server = get_global_server()

            # 生成唯一的URL前缀，格式为 /plugins/{plugin_name}
            prefix = f"/plugins/{plugin_name}"

            # 将插件的路由包含到主应用中
            server.app.include_router(plugin_router, prefix=prefix, tags=[plugin_name])

            logger.debug(f"成功将插件 '{plugin_name}' 的路由组件 '{router_name}' 挂载到: {prefix}")
            return True

        except Exception as e:
            logger.error(f"注册路由组件 '{router_info.name}' 时出错: {e}", exc_info=True)
            return False

    def _remove_plugin_registry(self, plugin_name: str) -> bool:
        """
        (内部方法) 仅移除插件的注册信息。

        Args:
            plugin_name (str): 插件名称。

        Returns:
            bool: 是否成功移除。
        """
        if plugin_name not in self._plugins:
            logger.warning(f"插件 {plugin_name} 未注册，无法移除其注册信息")
            return False
        del self._plugins[plugin_name]
        logger.info(f"插件 {plugin_name} 的注册信息已移除")
        return True

    # =================================================================
    # == 组件状态管理 (Component State Management)
    # =================================================================

    def enable_component(self, component_name: str, component_type: ComponentType) -> bool:
        """
        全局启用一个组件。

        Args:
            component_name (str): 组件名称。
            component_type (ComponentType): 组件类型。

        Returns:
            bool: 启用成功返回 True，失败返回 False。
        """
        target_component_class = self.get_component_class(component_name, component_type)
        target_component_info = self.get_component_info(component_name, component_type)
        if not target_component_class or not target_component_info:
            logger.warning(f"组件 {component_name} ({component_type.value}) 未注册，无法启用")
            return False

        target_component_info.enabled = True
        # 更新通用注册表中的状态
        namespaced_name = f"{component_type.value}.{component_name}"
        self._components[namespaced_name].enabled = True
        self._components_by_type[component_type][component_name].enabled = True

        # 更新特定类型的启用列表
        match component_type:
            case ComponentType.ACTION:
                assert isinstance(target_component_info, ActionInfo)
                self._default_actions[component_name] = target_component_info
            case ComponentType.COMMAND:
                # 旧版Command通过PlusCommand启用，这里无需操作
                pass
            case ComponentType.TOOL:
                assert issubclass(target_component_class, BaseTool)
                self._llm_available_tools[component_name] = target_component_class
            case ComponentType.EVENT_HANDLER:
                assert issubclass(target_component_class, BaseEventHandler)
                self._enabled_event_handlers[component_name] = target_component_class
                from .event_manager import event_manager
                cfg = self.get_plugin_config(target_component_info.plugin_name) or {}
                event_manager.register_event_handler(target_component_class, cfg)
            case ComponentType.CHATTER:
                assert issubclass(target_component_class, BaseChatter)
                self._enabled_chatter_registry[component_name] = target_component_class
            case ComponentType.INTEREST_CALCULATOR:
                assert issubclass(target_component_class, BaseInterestCalculator)
                self._enabled_interest_calculator_registry[component_name] = target_component_class
            case ComponentType.PROMPT:
                assert issubclass(target_component_class, BasePrompt)
                self._enabled_prompt_registry[component_name] = target_component_class

        logger.info(f"组件 {component_name} ({component_type.value}) 已全局启用")
        return True

    async def disable_component(self, component_name: str, component_type: ComponentType) -> bool:
        """
        全局禁用一个组件。

        Args:
            component_name (str): 组件名称。
            component_type (ComponentType): 组件类型。

        Returns:
            bool: 禁用成功返回 True，失败返回 False。
        """
        target_component_info = self.get_component_info(component_name, component_type)
        if not target_component_info:
            logger.warning(f"组件 {component_name} ({component_type.value}) 未注册，无法禁用")
            return False

        target_component_info.enabled = False
        # 更新通用注册表中的状态
        namespaced_name = f"{component_type.value}.{component_name}"
        if namespaced_name in self._components:
            self._components[namespaced_name].enabled = False
        if component_name in self._components_by_type[component_type]:
            self._components_by_type[component_type][component_name].enabled = False

        try:
            # 从特定类型的启用列表中移除
            match component_type:
                case ComponentType.ACTION:
                    self._default_actions.pop(component_name, None)
                case ComponentType.COMMAND:
                    # 旧版Command通过PlusCommand禁用，这里无需操作
                    pass
                case ComponentType.TOOL:
                    self._llm_available_tools.pop(component_name, None)
                case ComponentType.EVENT_HANDLER:
                    self._enabled_event_handlers.pop(component_name, None)
                    from .event_manager import event_manager
                    # 从事件管理器中取消订阅
                    event_manager.remove_event_handler(component_name)
                case ComponentType.CHATTER:
                    self._enabled_chatter_registry.pop(component_name, None)
                case ComponentType.INTEREST_CALCULATOR:
                    self._enabled_interest_calculator_registry.pop(component_name, None)
                case ComponentType.PROMPT:
                    self._enabled_prompt_registry.pop(component_name, None)

            logger.info(f"组件 {component_name} ({component_type.value}) 已全局禁用")
            return True
        except Exception as e:
            logger.error(f"禁用组件 {component_name} ({component_type.value}) 时发生错误: {e}", exc_info=True)
            # 即使出错，也尝试将状态标记为禁用
            return False

    # =================================================================
    # == 查询方法 (Query Methods)
    # =================================================================

    def get_component_info(
        self, component_name: str, component_type: ComponentType | None = None
    ) -> ComponentInfo | None:
        """
        获取组件信息，支持自动命名空间解析。

        如果只提供 `component_name`，它会尝试在所有类型中查找。如果找到多个同名但不同类型的
        组件，会发出警告并返回第一个找到的。

        Args:
            component_name (str): 组件名称，可以是原始名称或命名空间化的名称 (如 "action.my_action")。
            component_type (ComponentType, optional): 组件类型。如果提供，将只在该类型中查找。

        Returns:
            ComponentInfo | None: 找到的组件信息对象，或 None。
        """
        # 1. 如果已经是命名空间化的名称，直接查找
        if "." in component_name:
            return self._components.get(component_name)

        # 2. 如果指定了组件类型，构造命名空间化的名称查找
        if component_type:
            namespaced_name = f"{component_type.value}.{component_name}"
            return self._components.get(namespaced_name)

        # 3. 如果没有指定类型，遍历所有类型查找
        candidates = []
        for c_type in ComponentType:
            namespaced_name = f"{c_type.value}.{component_name}"
            if component_info := self._components.get(namespaced_name):
                candidates.append(component_info)

        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            types_found = [info.component_type.value for info in candidates]
            logger.warning(
                f"组件名称 '{component_name}' 在多个类型中存在: {types_found}。"
                f"返回第一个匹配项 (类型: {candidates[0].component_type.value})。请使用 component_type 参数指定类型以消除歧义。"
            )
            return candidates[0]

        # 4. 都没找到
        return None

    def get_component_class(
        self, component_name: str, component_type: ComponentType | None = None
    ) -> ComponentClassType | None:
        """
        获取组件的类定义，支持自动命名空间解析。

        逻辑与 `get_component_info` 类似。

        Args:
            component_name (str): 组件名称。
            component_type (ComponentType, optional): 组件类型。

        Returns:
            ComponentClassType | None: 找到的组件类，或 None。
        """
        # 1. 如果已经是命名空间化的名称，直接查找
        if "." in component_name:
            return self._components_classes.get(component_name)

        # 2. 如果指定了组件类型，构造命名空间化的名称查找
        if component_type:
            namespaced_name = f"{component_type.value}.{component_name}"
            return self._components_classes.get(namespaced_name)

        # 3. 如果没有指定类型，遍历所有类型查找
        info = self.get_component_info(component_name)  # 复用 get_component_info 的查找和消歧义逻辑
        if info:
            namespaced_name = f"{info.component_type.value}.{info.name}"
            return self._components_classes.get(namespaced_name)

        # 4. 都没找到
        return None

    def get_components_by_type(self, component_type: ComponentType) -> dict[str, ComponentInfo]:
        """获取指定类型的所有已注册组件（无论是否启用）。"""
        return self._components_by_type.get(component_type, {}).copy()

    def get_enabled_components_by_type(
        self, component_type: ComponentType, stream_id: str | None = None
    ) -> dict[str, ComponentInfo]:
        """
        获取指定类型的所有可用组件。

        这会同时考虑全局启用状态和 `stream_id` 对应的局部状态。

        Args:
            component_type (ComponentType): 要查询的组件类型。
            stream_id (str, optional): 会话ID，用于检查局部状态覆盖。

        Returns:
            dict[str, ComponentInfo]: 一个包含可用组件名称和信息的字典。
        """
        all_components = self.get_components_by_type(component_type)
        return {
            name: info
            for name, info in all_components.items()
            if self.is_component_available(name, component_type, stream_id)
        }

    # =================================================================
    # == 特定类型查询方法 (Type-Specific Query Methods)
    # =================================================================

    # --- Action ---
    def get_action_registry(self) -> dict[str, type[BaseAction]]:
        """获取所有已注册的Action类。"""
        return self._action_registry.copy()

    def get_default_actions(self, stream_id: str | None = None) -> dict[str, ActionInfo]:
        """获取所有可用的Action信息（考虑全局和局部状态）。"""
        return cast(
            dict[str, ActionInfo],
            self.get_enabled_components_by_type(ComponentType.ACTION, stream_id),
        )

    # --- PlusCommand ---
    def get_plus_command_registry(self) -> dict[str, type[PlusCommand]]:
        """获取所有已注册的PlusCommand类。"""
        return self._plus_command_registry.copy()

    def get_available_plus_commands_info(self, stream_id: str | None = None) -> dict[str, PlusCommandInfo]:
        """获取所有可用的PlusCommand信息（考虑全局和局部状态）。"""
        return cast(
            dict[str, PlusCommandInfo],
            self.get_enabled_components_by_type(ComponentType.PLUS_COMMAND, stream_id),
        )

    # --- Tool ---
    def get_tool_registry(self) -> dict[str, type[BaseTool]]:
        """获取所有已注册的Tool类。"""
        return self._tool_registry.copy()

    def get_llm_available_tools(self, stream_id: str | None = None) -> dict[str, type[BaseTool]]:
        """获取所有对LLM可用的Tool类（考虑全局和局部状态）。"""
        all_tools = self.get_tool_registry()
        available_tools = {}
        for name, tool_class in all_tools.items():
            if self.is_component_available(name, ComponentType.TOOL, stream_id):
                available_tools[name] = tool_class
        return available_tools

    # --- EventHandler ---
    def get_event_handler_registry(self) -> dict[str, type[BaseEventHandler]]:
        """获取所有已注册的EventHandler类。"""
        return self._event_handler_registry.copy()

    def get_enabled_event_handlers(self) -> dict[str, type[BaseEventHandler]]:
        """获取所有已启用的EventHandler类。"""
        return self._enabled_event_handlers.copy()

    # --- Chatter ---
    def get_chatter_registry(self) -> dict[str, type[BaseChatter]]:
        """获取所有已注册的Chatter类。"""
        return self._chatter_registry.copy()

    def get_enabled_chatter_registry(self, stream_id: str | None = None) -> dict[str, type[BaseChatter]]:
        """获取所有可用的Chatter类（考虑全局和局部状态）。"""
        all_chatters = self.get_chatter_registry()
        available_chatters = {}
        for name, chatter_class in all_chatters.items():
            if self.is_component_available(name, ComponentType.CHATTER, stream_id):
                available_chatters[name] = chatter_class
        return available_chatters

    # --- Prompt ---
    def get_prompt_registry(self) -> dict[str, type[BasePrompt]]:
        """获取所有已注册的Prompt类。"""
        return self._prompt_registry.copy()

    def get_enabled_prompt_registry(self) -> dict[str, type[BasePrompt]]:
        """获取所有已启用的Prompt类。"""
        return self._enabled_prompt_registry.copy()

    # --- 插件 ---
    def get_plugin_info(self, plugin_name: str) -> PluginInfo | None:
        """获取指定插件的信息。"""
        return self._plugins.get(plugin_name)

    def get_all_plugins(self) -> dict[str, PluginInfo]:
        """获取所有已注册的插件。"""
        return self._plugins.copy()

    def get_plugin_components(self, plugin_name: str) -> list["ComponentInfo"]:
        """获取指定插件下的所有组件信息。"""
        plugin_info = self.get_plugin_info(plugin_name)
        return plugin_info.components if plugin_info else []

    def get_plugin_config(self, plugin_name: str) -> dict | None:
        """
        获取插件的配置信息。

        它会首先尝试从已加载的插件实例中获取，如果失败，则尝试从文件系统读取。

        Args:
            plugin_name (str): 插件名称。

        Returns:
            dict | None: 插件的配置字典，如果找不到则返回 None。
        """
        # 延迟导入以避免循环依赖
        from src.plugin_system.core.plugin_manager import plugin_manager

        plugin_instance = plugin_manager.get_plugin_instance(plugin_name)
        if plugin_instance and plugin_instance.config:
            return plugin_instance.config

        # 如果插件实例不存在，尝试从配置文件读取
        try:
            config_path = Path("config") / "plugins" / plugin_name / "config.toml"
            if config_path.exists():
                with open(config_path, encoding="utf-8") as f:
                    config_data = toml.load(f)
                logger.debug(f"从配置文件延迟加载了插件 {plugin_name} 的配置")
                return config_data
        except Exception as e:
            logger.debug(f"读取插件 {plugin_name} 配置文件失败: {e}")

        return None

    # =================================================================
    # == 局部状态管理 (Local State Management)
    # =================================================================

    def set_local_component_state(
        self, stream_id: str, component_name: str, component_type: ComponentType, enabled: bool
    ) -> bool:
        """
        为指定的会话（stream_id）设置组件的局部（临时）状态。

        这允许在单个对话流中动态启用或禁用组件，而不影响全局设置。

        Args:
            stream_id (str): 唯一的会话ID。
            component_name (str): 组件名称。
            component_type (ComponentType): 组件类型。
            enabled (bool): True 表示启用，False 表示禁用。

        Returns:
            bool: 设置成功返回 True，如果组件类型不支持局部状态则返回 False。
        """
        if component_type in self._no_local_state_types:
            logger.warning(
                f"组件类型 {component_type.value} 不支持局部状态管理。"
                f"尝试为 '{component_name}' 设置局部状态的操作将被忽略。"
            )
            return False

        if stream_id not in self._local_component_states:
            self._local_component_states[stream_id] = {}

        state_key = (component_name, component_type)
        self._local_component_states[stream_id][state_key] = enabled
        logger.debug(
            f"已为 stream '{stream_id}' 设置局部状态: {component_name} ({component_type.value}) -> {'启用' if enabled else '禁用'}"
        )
        return True

    def is_component_available(
        self, component_name: str, component_type: ComponentType, stream_id: str | None = None
    ) -> bool:
        """
        检查一个组件在给定上下文中是否可用。

        检查顺序:
        1. 组件是否存在。
        2. (如果提供了 stream_id) 是否有局部状态覆盖。
        3. 全局启用状态。

        Args:
            component_name (str): 组件名称。
            component_type (ComponentType): 组件类型。
            stream_id (str, optional): 会话ID。

        Returns:
            bool: 如果组件可用，则返回 True。
        """
        component_info = self.get_component_info(component_name, component_type)

        # 1. 检查组件是否存在
        if not component_info:
            return False

        # 2. 如果组件类型不支持局部状态，直接返回其全局状态
        if component_type in self._no_local_state_types:
            return component_info.enabled

        # 3. 如果提供了 stream_id，检查是否存在局部状态覆盖
        if stream_id and stream_id in self._local_component_states:
            state_key = (component_name, component_type)
            local_state = self._local_component_states[stream_id].get(state_key)
            if local_state is not None:
                return local_state  # 局部状态存在，直接返回

        # 4. 如果没有局部状态覆盖，返回全局状态
        return component_info.enabled

    # =================================================================
    # == MCP 工具相关方法 (MCP Tool Methods)
    # =================================================================

    async def load_mcp_tools(self) -> None:
        """
        异步加载所有 MCP (Model-Copilot-Plugin) 工具。

        此方法会动态导入并实例化 MCP 工具适配器。为避免重复加载，它会检查一个标志位。
        """
        if self._mcp_tools_loaded:
            logger.debug("MCP 工具已加载，跳过")
            return

        try:
            from .mcp_tool_adapter import load_mcp_tools_as_adapters

            logger.info("开始加载 MCP 工具...")
            self._mcp_tools = await load_mcp_tools_as_adapters()
            self._mcp_tools_loaded = True
            logger.info(f"MCP 工具加载完成，共 {len(self._mcp_tools)} 个工具")
        except Exception as e:
            logger.error(f"加载 MCP 工具失败: {e}", exc_info=True)
            self._mcp_tools = []
            self._mcp_tools_loaded = True  # 标记为已尝试加载，避免重复失败

    def get_mcp_tools(self) -> list["BaseTool"]:
        """获取所有已加载的 MCP 工具适配器实例。"""
        return self._mcp_tools.copy()

    def is_mcp_tool(self, tool_name: str) -> bool:
        """检查一个工具名称是否代表一个 MCP 工具（基于命名约定）。"""
        return tool_name.startswith("mcp_")

    # =================================================================
    # == 统计与辅助方法 (Statistics and Helper Methods)
    # =================================================================

    def get_registry_stats(self) -> dict[str, Any]:
        """获取注册中心的统计信息，用于调试和监控。"""
        stats = {component_type.value: 0 for component_type in ComponentType}
        for component in self._components.values():
            stats[component.component_type.value] += 1

        return {
            "total_plugins": len(self._plugins),
            "total_components": len(self._components),
            "enabled_components": len([c for c in self._components.values() if c.enabled]),
            "mcp_tools_loaded": len(self._mcp_tools),
            "components_by_type": stats,
        }


# --- 全局实例 ---
# 创建全局唯一的组件注册中心实例，供项目各处使用
component_registry = ComponentRegistry()
