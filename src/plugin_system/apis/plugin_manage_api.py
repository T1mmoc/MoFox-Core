# -*- coding: utf-8 -*-
"""
Plugin Manage API
=================

该模块提供了用于管理插件和组件生命周期、状态和信息查询的核心API。
功能包括插件的加载、重载、注册、扫描，组件的启用/禁用，以及系统状态报告的生成。
所有函数都设计为异步或同步，以适应不同的调用上下文。
"""

import os
from typing import Any, Literal

from src.common.logger import get_logger
from src.plugin_system.base.component_types import ComponentType
from src.plugin_system.core.component_registry import ComponentInfo, component_registry
from src.plugin_system.core.plugin_manager import plugin_manager

# 初始化日志记录器
logger = get_logger("plugin_manage_api")


# --------------------------------------------------------------------------------
# Section 1: 插件生命周期管理 (Plugin Lifecycle Management)
# --------------------------------------------------------------------------------
# 该部分包含控制插件加载、重载、注册和发现的核心功能。


async def reload_all_plugins() -> bool:
    """
    重新加载所有当前已成功加载的插件。

    此操作会遍历所有已加载的插件，逐一进行卸载和重新加载。
    如果任何一个插件重载失败，整个过程会继续，但最终返回 False。

    Returns:
        bool: 如果所有插件都成功重载，则为 True，否则为 False。
    """
    logger.info("开始重新加载所有插件...")
    # 使用 list() 创建一个当前已加载插件列表的副本，以避免在迭代过程中修改原始列表
    loaded_plugins = list(plugin_manager.list_loaded_plugins())
    all_success = True

    # 遍历副本列表中的每个插件进行重载
    for plugin_name in loaded_plugins:
        try:
            success = await reload_plugin(plugin_name)
            if not success:
                all_success = False
                logger.error(f"重载插件 {plugin_name} 失败。")
        except Exception as e:
            all_success = False
            logger.error(f"重载插件 {plugin_name} 时发生未知异常: {e}", exc_info=True)

    logger.info("所有插件重载完毕。")
    return all_success


async def reload_plugin(name: str) -> bool:
    """
    重新加载指定的单个插件。

    该函数首先检查插件是否已注册，然后调用插件管理器执行重载操作。

    Args:
        name (str): 要重载的插件的名称。

    Returns:
        bool: 如果插件成功重载，则为 True。

    Raises:
        ValueError: 如果插件未在插件管理器中注册。
    """
    # 验证插件是否存在于注册列表中
    if name not in plugin_manager.list_registered_plugins():
        raise ValueError(f"插件 '{name}' 未注册，无法重载。")
    # 调用插件管理器的核心重载方法
    return await plugin_manager.reload_registered_plugin(name)


def rescan_and_register_plugins(load_after_register: bool = True) -> tuple[int, int]:
    """
    重新扫描所有插件目录，以发现并注册新插件。

    此函数会触发插件管理器扫描其配置的所有插件目录。
    可以选择在注册新发现的插件后立即加载它们。

    Args:
        load_after_register (bool): 如果为 True，新发现的插件将在注册后立即被加载。默认为 True。

    Returns:
        tuple[int, int]: 一个元组，包含 (成功加载的插件数量, 加载失败的插件数量)。
    """
    # 扫描插件目录，获取新注册成功和失败的数量
    success_count, fail_count = plugin_manager.rescan_plugin_directory()

    # 如果不需要在注册后加载，则直接返回扫描结果
    if not load_after_register:
        return success_count, fail_count

    # 找出新注册但尚未加载的插件
    newly_registered = [
        p for p in plugin_manager.list_registered_plugins() if p not in plugin_manager.list_loaded_plugins()
    ]

    loaded_success_count = 0
    # 尝试加载所有新注册的插件
    for plugin_name in newly_registered:
        status, _ = plugin_manager.load_registered_plugin_classes(plugin_name)
        if status:
            loaded_success_count += 1

    # 计算总的成功和失败数量
    total_failed = fail_count + (len(newly_registered) - loaded_success_count)
    return loaded_success_count, total_failed


def register_plugin_from_file(plugin_name: str, load_after_register: bool = True) -> bool:
    """
    从插件目录中查找、注册并选择性地加载一个指定的插件。

    如果插件已经加载，此函数将直接返回 True。
    如果插件未注册，它会遍历所有插件目录以查找匹配的插件文件夹。

    Args:
        plugin_name (str): 插件的名称（通常是其目录名）。
        load_after_register (bool): 注册成功后是否立即加载该插件。默认为 True。

    Returns:
        bool: 如果插件成功注册（并且根据参数成功加载），则为 True。
    """
    # 如果插件已经加载，无需执行任何操作
    if plugin_name in plugin_manager.list_loaded_plugins():
        logger.warning(f"插件 '{plugin_name}' 已经加载，无需重复注册。")
        return True

    # 如果插件尚未注册，则开始搜索流程
    if plugin_name not in plugin_manager.list_registered_plugins():
        logger.info(f"插件 '{plugin_name}' 未注册，开始在插件目录中搜索...")
        found_path = None

        # 遍历所有配置的插件目录
        for directory in plugin_manager.plugin_directories:
            potential_path = os.path.join(directory, plugin_name)
            # 检查是否存在与插件同名的目录
            if os.path.isdir(potential_path):
                found_path = potential_path
                break

        # 如果未找到插件目录，则报告错误
        if not found_path:
            logger.error(f"在所有插件目录中都未找到名为 '{plugin_name}' 的插件。")
            return False

        # 检查插件的核心 'plugin.py' 文件是否存在
        plugin_file = os.path.join(found_path, "plugin.py")
        if not os.path.exists(plugin_file):
            logger.error(f"在插件目录 '{found_path}' 中未找到核心的 plugin.py 文件。")
            return False

        # 尝试从文件加载插件模块
        module = plugin_manager._load_plugin_module_file(plugin_file)
        if not module:
            logger.error(f"从文件 '{plugin_file}' 加载插件模块失败。")
            return False

        # 验证模块加载后，插件是否已成功注册
        if plugin_name not in plugin_manager.list_registered_plugins():
            logger.error(f"插件 '{plugin_name}' 在加载模块后依然未能成功注册。请检查插件定义。")
            return False

        logger.info(f"插件 '{plugin_name}' 已成功发现并注册。")

    # 根据参数决定是否在注册后立即加载插件
    if load_after_register:
        logger.info(f"正在加载插件 '{plugin_name}'...")
        status, _ = plugin_manager.load_registered_plugin_classes(plugin_name)
        return status

    return True


# --------------------------------------------------------------------------------
# Section 2: 组件状态管理 (Component State Management)
# --------------------------------------------------------------------------------
# 这部分 API 负责控制单个组件的启用和禁用状态，支持全局和局部（临时）范围。


async def set_component_enabled(name: str, component_type: ComponentType, enabled: bool) -> bool:
    """
    在全局范围内启用或禁用一个组件。

    此更改会直接修改组件在注册表中的状态，但此状态是临时的，不会持久化到配置文件中。
    包含一个保护机制，防止禁用最后一个已启用的 Chatter 组件。

    Args:
        name (str): 要操作的组件的名称。
        component_type (ComponentType): 组件的类型。
        enabled (bool): True 表示启用, False 表示禁用。

    Returns:
        bool: 如果操作成功，则为 True。
    """
    # 特殊保护：确保系统中至少有一个 Chatter 组件处于启用状态
    if component_type == ComponentType.CHATTER and not enabled:
        enabled_chatters = component_registry.get_enabled_components_by_type(ComponentType.CHATTER)
        # 如果当前启用的 Chatter 少于等于1个，并且要禁用的就是它，则阻止操作
        if len(enabled_chatters) <= 1 and name in enabled_chatters:
            logger.warning(f"操作被阻止：不能禁用最后一个启用的 Chatter 组件 ('{name}')。")
            return False

    # 获取组件信息
    component_info = component_registry.get_component_info(name, component_type)
    if not component_info:
        logger.error(f"未找到组件 {name} ({component_type.value})，无法更改其状态。")
        return False

    # 直接修改组件实例的 enabled 状态
    component_info.enabled = enabled
    logger.info(f"组件 {name} ({component_type.value}) 的全局状态已设置为: {enabled}")
    return True


def set_component_enabled_local(stream_id: str, name: str, component_type: ComponentType, enabled: bool) -> bool:
    """
    在一个特定的 stream_id 上下文中临时启用或禁用组件。

    此状态仅存在于内存中，并且只对指定的 stream_id 有效，不影响全局组件状态。
    同样包含对 Chatter 组件的保护机制。

    Args:
        stream_id (str): 唯一的上下文标识符，例如一个会话ID。
        name (str): 组件名称。
        component_type (ComponentType): 组件类型。
        enabled (bool): True 为启用, False 为禁用。

    Returns:
        bool: 如果操作成功，则为 True。
    """
    # 首先，验证组件是否存在
    component_info = component_registry.get_component_info(name, component_type)
    if not component_info:
        logger.error(f"尝试设置局部状态失败：未找到组件 {name} ({component_type.value})。")
        return False

    # Chatter 唯一性保护（在 stream_id 上下文中）
    if component_type == ComponentType.CHATTER and not enabled:
        # 检查当前 stream_id 上下文中启用的 Chatter
        enabled_chatters = component_registry.get_enabled_components_by_type(ComponentType.CHATTER, stream_id=stream_id)
        if len(enabled_chatters) <= 1 and name in enabled_chatters:
            logger.warning(f"操作被阻止：在 stream '{stream_id}' 中，不能禁用最后一个启用的 Chatter 组件 ('{name}')。")
            return False

    # 设置局部状态
    component_registry.set_local_component_state(stream_id, name, component_type, enabled)
    logger.info(f"在 stream '{stream_id}' 中，组件 {name} ({component_type.value}) 的局部状态已设置为: {enabled}")
    return True


# --------------------------------------------------------------------------------
# Section 3: 信息查询与报告 (Information Querying & Reporting)
# --------------------------------------------------------------------------------
# 这部分 API 用于获取关于插件和组件的详细信息、列表和统计数据。


def get_system_report() -> dict[str, Any]:
    """
    生成一份详细的系统状态报告。

    报告包含已加载插件、失败插件和组件的全面信息，是调试和监控系统状态的核心工具。

    Returns:
        dict[str, Any]: 包含系统、插件和组件状态的详细报告字典。
    """
    loaded_plugins_info = {}
    # 遍历所有已加载的插件实例
    for name, instance in plugin_manager.loaded_plugins.items():
        plugin_info = component_registry.get_plugin_info(name)
        if not plugin_info:
            continue

        # 收集该插件下所有组件的详细信息
        components_details = [
            {
                "name": comp_info.name,
                "component_type": comp_info.component_type.value,
                "description": comp_info.description,
                "enabled": comp_info.enabled,
            }
            for comp_info in plugin_info.components
        ]

        # 构建单个插件的信息字典
        # 元数据从 PluginInfo 获取，而启用状态(enable_plugin)从插件实例获取
        loaded_plugins_info[name] = {
            "display_name": plugin_info.display_name or name,
            "version": plugin_info.version,
            "author": plugin_info.author,
            "enabled": instance.enable_plugin,
            "components": components_details,
        }

    # 构建最终的完整报告
    report = {
        "system_info": {
            "loaded_plugins_count": len(plugin_manager.loaded_plugins),
            "total_components_count": component_registry.get_registry_stats().get("total_components", 0),
        },
        "plugins": loaded_plugins_info,
        "failed_plugins": plugin_manager.failed_plugins,
    }
    return report


def get_plugin_details(plugin_name: str) -> dict[str, Any] | None:
    """
    获取单个插件的详细报告。

    报告内容包括插件的元数据、所有组件的详细信息及其当前状态。
    这是 `get_system_report` 的单插件聚焦版本。

    Args:
        plugin_name (str): 要查询的插件名称。

    Returns:
        dict | None: 包含插件详细信息的字典，如果插件未注册则返回 None。
    """
    plugin_info = component_registry.get_plugin_info(plugin_name)
    if not plugin_info:
        logger.warning(f"尝试获取插件详情失败：未找到名为 '{plugin_name}' 的插件。")
        return None

    # 收集该插件下所有组件的信息
    components_details = [
        {
            "name": comp_info.name,
            "component_type": comp_info.component_type.value,
            "description": comp_info.description,
            "enabled": comp_info.enabled,
        }
        for comp_info in plugin_info.components
    ]

    # 获取插件实例以检查其启用状态
    plugin_instance = plugin_manager.get_plugin_instance(plugin_name)
    is_enabled = plugin_instance.enable_plugin if plugin_instance else False

    # 组装详细信息字典
    return {
        "name": plugin_info.name,
        "display_name": plugin_info.display_name or plugin_info.name,
        "version": plugin_info.version,
        "author": plugin_info.author,
        "license": plugin_info.license,
        "description": plugin_info.description,
        "enabled": is_enabled,
        "status": "loaded" if is_plugin_loaded(plugin_name) else "registered",
        "components": components_details,
    }


def list_plugins(status: Literal["loaded", "registered", "failed"]) -> list[str]:
    """
    根据指定的状态列出插件名称列表。

    提供了一种快速、便捷的方式来监控和调试插件系统，而无需解析完整的系统报告。

    Args:
        status (str): 插件状态，可选值为 'loaded', 'registered', 'failed'。

    Returns:
        list[str]: 对应状态的插件名称列表。

    Raises:
        ValueError: 如果传入了无效的状态字符串。
    """
    if status == "loaded":
        # 返回所有当前已成功加载的插件
        return plugin_manager.list_loaded_plugins()
    if status == "registered":
        # 返回所有已注册（但不一定已加载）的插件
        return plugin_manager.list_registered_plugins()
    if status == "failed":
        # 返回所有加载失败的插件的名称
        return list(plugin_manager.failed_plugins.keys())
    # 如果状态无效，则引发错误
    raise ValueError(f"无效的插件状态: '{status}'。有效选项为 'loaded', 'registered', 'failed'。")


def list_components(component_type: ComponentType, enabled_only: bool = True) -> list[dict[str, Any]]:
    """
    列出指定类型的所有组件的详细信息。

    这是查找和管理组件的核心功能，例如，获取所有可用的工具或所有注册的聊天器。

    Args:
        component_type (ComponentType): 要查询的组件类型。
        enabled_only (bool, optional): 是否只返回已启用的组件。默认为 True。

    Returns:
        list[dict[str, Any]]: 一个包含组件信息字典的列表。
    """
    # 根据 enabled_only 参数决定是获取所有组件还是仅获取已启用的组件
    if enabled_only:
        components = component_registry.get_enabled_components_by_type(component_type)
    else:
        components = component_registry.get_components_by_type(component_type)

    # 将组件信息格式化为字典列表
    return [
        {
            "name": info.name,
            "plugin_name": info.plugin_name,
            "description": info.description,
            "enabled": info.enabled,
        }
        for info in components.values()
    ]


def search_components_by_name(
    name_keyword: str,
    component_type: ComponentType | None = None,
    case_sensitive: bool = False,
    exact_match: bool = False,
) -> list[dict[str, Any]]:
    """
    根据名称关键字搜索组件，支持模糊匹配和精确匹配。

    极大地增强了组件的可发现性，用户无需知道完整名称即可找到所需组件。

    Args:
        name_keyword (str): 用于搜索的名称关键字。
        component_type (ComponentType | None, optional): 如果提供，则只在该类型中搜索。默认为 None (搜索所有类型)。
        case_sensitive (bool, optional): 是否进行大小写敏感的搜索。默认为 False。
        exact_match (bool, optional): 是否进行精确匹配。默认为 False (模糊匹配)。

    Returns:
        list[dict[str, Any]]: 匹配的组件信息字典的列表。
    """
    results = []
    # 如果未指定组件类型，则搜索所有类型
    types_to_search = [component_type] if component_type else list(ComponentType)

    # 根据是否大小写敏感，预处理搜索关键字
    compare_str = name_keyword if case_sensitive else name_keyword.lower()

    # 遍历要搜索的组件类型
    for comp_type in types_to_search:
        all_components = component_registry.get_components_by_type(comp_type)
        for name, info in all_components.items():
            # 同样地，预处理组件名称
            target_name = name if case_sensitive else name.lower()

            # 根据 exact_match 参数决定使用精确比较还是模糊包含检查
            is_match = (compare_str == target_name) if exact_match else (compare_str in target_name)

            # 如果匹配，则将组件信息添加到结果列表
            if is_match:
                results.append(
                    {
                        "name": info.name,
                        "component_type": info.component_type.value,
                        "plugin_name": info.plugin_name,
                        "description": info.description,
                        "enabled": info.enabled,
                    }
                )
    return results


def get_component_info(name: str, component_type: ComponentType) -> ComponentInfo | None:
    """
    获取任何一个已注册组件的详细信息对象。

    Args:
        name (str): 组件的唯一名称。
        component_type (ComponentType): 组件的类型。

    Returns:
        ComponentInfo | None: 包含组件完整信息的 ComponentInfo 对象，如果找不到则返回 None。
    """
    return component_registry.get_component_info(name, component_type)


def get_component_count(component_type: ComponentType, stream_id: str | None = None) -> int:
    """
    获取指定类型的已加载并启用的组件的总数。

    可以根据 `stream_id` 考虑局部状态，从而获得特定上下文中的组件数量。

    Args:
        component_type (ComponentType): 要查询的组件类型。
        stream_id (str | None): 可选的上下文ID。如果提供，将计入局部状态。

    Returns:
        int: 该类型下已启用的组件的数量。
    """
    return len(component_registry.get_enabled_components_by_type(component_type, stream_id=stream_id))


# --------------------------------------------------------------------------------
# Section 4: 工具函数 (Utility Helpers)
# --------------------------------------------------------------------------------
# 这部分提供了一些轻量级的辅助函数，用于快速检查状态。


def is_plugin_loaded(plugin_name: str) -> bool:
    """
    快速检查一个插件当前是否已成功加载。

    这是一个比 `get_plugin_details` 更轻量级的检查方法，适用于需要快速布尔值判断的场景。

    Args:
        plugin_name (str): 要检查的插件名称。

    Returns:
        bool: 如果插件已加载，则为 True，否则为 False。
    """
    return plugin_name in plugin_manager.list_loaded_plugins()


def get_component_plugin(component_name: str, component_type: ComponentType) -> str | None:
    """
    查找一个特定组件属于哪个插件。

    在调试或管理组件时，此函数能够方便地追溯其定义的源头。

    Args:
        component_name (str): 组件的名称。
        component_type (ComponentType): 组件的类型。

    Returns:
        str | None: 组件所属的插件名称，如果找不到组件则返回 None。
    """
    component_info = component_registry.get_component_info(component_name, component_type)
    return component_info.plugin_name if component_info else None
