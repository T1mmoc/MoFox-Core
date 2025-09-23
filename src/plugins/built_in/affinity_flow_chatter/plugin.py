"""
亲和力聊天处理器插件
"""

from typing import List, Tuple, Type

from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.component_types import ComponentInfo
from src.common.logger import get_logger

logger = get_logger("affinity_chatter_plugin")


@register_plugin
class AffinityChatterPlugin(BasePlugin):
    """亲和力聊天处理器插件

    - 延迟导入 `AffinityChatter` 并通过组件注册器注册为聊天处理器
    - 提供 `get_plugin_components` 以兼容插件注册机制
    """

    plugin_name: str = "affinity_chatter"
    enable_plugin: bool = True
    dependencies: list[str] = []
    python_dependencies: list[str] = []
    config_file_name: str = ""

    # 简单的 config_schema 占位（如果将来需要配置可扩展）
    config_schema = {}

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表（ChatterInfo, AffinityChatter）

        这里采用延迟导入 AffinityChatter 来避免循环依赖和启动顺序问题。
        如果导入失败则返回空列表以让注册过程继续而不崩溃。
        """
        try:
            # 延迟导入以避免循环导入
            from .affinity_chatter import AffinityChatter

            return [(AffinityChatter.get_chatter_info(), AffinityChatter)]

        except Exception as e:
            logger.error(f"加载 AffinityChatter 时出错: {e}")
            return []
