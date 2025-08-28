import sys
import asyncio
import json
import websockets as Server
from . import event_types,CONSTS

from typing import List, Tuple

from src.plugin_system import BasePlugin, BaseEventHandler, register_plugin, EventType, ConfigField, BaseAction, ActionActivationType
from src.plugin_system.base.base_event import HandlerResult
from src.plugin_system.core.event_manager import event_manager

from pathlib import Path
from src.common.logger import get_logger
logger = get_logger("napcat_adapter")

# 添加当前目录到Python路径，这样可以识别src包
current_dir = Path(__file__).parent
sys.path.insert(0, str(current_dir))

from .src.recv_handler.message_handler import message_handler
from .src.recv_handler.meta_event_handler import meta_event_handler
from .src.recv_handler.notice_handler import notice_handler
from .src.recv_handler.message_sending import message_send_instance
from .src.send_handler import send_handler
from .src.config import global_config
from .src.config.features_config import features_manager
from .src.config.migrate_features import auto_migrate_features
from .src.mmc_com_layer import mmc_start_com, mmc_stop_com, router
from .src.response_pool import put_response, check_timeout_response
from .src.websocket_manager import websocket_manager

message_queue = asyncio.Queue()

class LauchNapcatAdapterHandler(BaseEventHandler):
    """自动启动Adapter"""

    handler_name: str = "launch_napcat_adapter_handler"
    handler_description: str = "自动启动napcat adapter"
    weight: int = 100
    intercept_message: bool = False
    init_subscribe = [EventType.ON_START]

    async def message_recv(self, server_connection: Server.ServerConnection):
        await message_handler.set_server_connection(server_connection)
        asyncio.create_task(notice_handler.set_server_connection(server_connection))
        await send_handler.set_server_connection(server_connection)
        async for raw_message in server_connection:
            logger.debug(f"{raw_message[:1500]}..." if (len(raw_message) > 1500) else raw_message)
            decoded_raw_message: dict = json.loads(raw_message)
            post_type = decoded_raw_message.get("post_type")
            if post_type in ["meta_event", "message", "notice"]:
                await message_queue.put(decoded_raw_message)
            elif post_type is None:
                await put_response(decoded_raw_message)

    async def message_process(self):
        while True:
            message = await message_queue.get()
            post_type = message.get("post_type")
            if post_type == "message":
                await message_handler.handle_raw_message(message)
            elif post_type == "meta_event":
                await meta_event_handler.handle_meta_event(message)
            elif post_type == "notice":
                await notice_handler.handle_notice(message)
            else:
                logger.warning(f"未知的post_type: {post_type}")
            message_queue.task_done()
            await asyncio.sleep(0.05)

    async def napcat_server(self):
        """启动 Napcat WebSocket 连接（支持正向和反向连接）"""
        mode = global_config.napcat_server.mode
        logger.info(f"正在启动 adapter，连接模式: {mode}")
        
        try:
            await websocket_manager.start_connection(self.message_recv)
        except Exception as e:
            logger.error(f"启动 WebSocket 连接失败: {e}")
            raise

    async def execute(self, kwargs):       
        # 执行功能配置迁移（如果需要）
        logger.info("检查功能配置迁移...")
        auto_migrate_features()
        
        # 初始化功能管理器
        logger.info("正在初始化功能管理器...")
        features_manager.load_config()
        await features_manager.start_file_watcher(check_interval=2.0)
        logger.info("功能管理器初始化完成")
        logger.info("开始启动Napcat Adapter")        
        message_send_instance.maibot_router = router
        # 创建单独的异步任务，防止阻塞主线程
        asyncio.create_task(self.napcat_server())
        asyncio.create_task(mmc_start_com())
        asyncio.create_task(self.message_process())
        asyncio.create_task(check_timeout_response())

@register_plugin
class NapcatAdapterPlugin(BasePlugin):
    plugin_name = CONSTS.PLUGIN_NAME
    enable_plugin: bool = True
    dependencies: List[str] = []  # 插件依赖列表
    python_dependencies: List[str] = []  # Python包依赖列表
    config_file_name: str = "config.toml"  # 配置文件名

    # 配置节描述
    config_section_descriptions = {"plugin": "插件基本信息"}

    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="napcat_adapter_plugin", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=False, description="是否启用插件"),
        }
    }


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        for e in event_types.NapcatEvent.ON_RECEIVED:
            event_manager.register_event(e ,allowed_triggers=[self.plugin_name])
    
    def get_plugin_components(self):
        components = []
        components.append((LauchNapcatAdapterHandler.get_handler_info(), LauchNapcatAdapterHandler))
        return components
