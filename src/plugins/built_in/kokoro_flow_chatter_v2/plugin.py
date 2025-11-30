"""
Kokoro Flow Chatter V2 - 插件注册

注册 Chatter
"""

from typing import Any, ClassVar

from src.common.logger import get_logger
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.component_types import ChatterInfo
from src.plugin_system.decorators import register_plugin

from .chatter import KokoroFlowChatterV2
from .config import get_config
from .proactive_thinker import start_proactive_thinker, stop_proactive_thinker

logger = get_logger("kfc_v2_plugin")


@register_plugin
class KokoroFlowChatterV2Plugin(BasePlugin):
    """
    Kokoro Flow Chatter V2 插件
    
    专为私聊设计的增强 Chatter：
    - 线性叙事提示词架构
    - 等待机制与心理状态演变
    - 主动思考能力
    """
    
    plugin_name: str = "kokoro_flow_chatter_v2"
    enable_plugin: bool = True
    plugin_priority: int = 50  # 高于默认 Chatter
    dependencies: ClassVar[list[str]] = []
    python_dependencies: ClassVar[list[str]] = []
    config_file_name: str = "config.toml"
    
    # 状态
    _is_started: bool = False
    
    async def on_plugin_loaded(self):
        """插件加载时"""
        config = get_config()
        
        if not config.enabled:
            logger.info("[KFC V2] 插件已禁用")
            return
        
        logger.info("[KFC V2] 插件已加载")
        
        # 启动主动思考器
        if config.proactive.enabled:
            try:
                await start_proactive_thinker()
                logger.info("[KFC V2] 主动思考器已启动")
                self._is_started = True
            except Exception as e:
                logger.error(f"[KFC V2] 启动主动思考器失败: {e}")
    
    async def on_plugin_unloaded(self):
        """插件卸载时"""
        try:
            await stop_proactive_thinker()
            logger.info("[KFC V2] 主动思考器已停止")
            self._is_started = False
        except Exception as e:
            logger.warning(f"[KFC V2] 停止主动思考器失败: {e}")
    
    def get_plugin_components(self):
        """返回组件列表"""
        config = get_config()
        
        if not config.enabled:
            return []
        
        components = []
        
        try:
            # 注册 Chatter
            components.append((
                KokoroFlowChatterV2.get_chatter_info(),
                KokoroFlowChatterV2,
            ))
            logger.debug("[KFC V2] 成功加载 KokoroFlowChatterV2 组件")
        except Exception as e:
            logger.error(f"[KFC V2] 加载组件失败: {e}")
        
        return components
    
    def get_plugin_info(self) -> dict[str, Any]:
        """获取插件信息"""
        return {
            "name": self.plugin_name,
            "display_name": "Kokoro Flow Chatter V2",
            "version": "2.0.0",
            "author": "MoFox",
            "description": "专为私聊设计的增强 Chatter",
            "features": [
                "线性叙事提示词架构",
                "心理活动流记录",
                "等待机制与超时处理",
                "主动思考能力",
            ],
        }
