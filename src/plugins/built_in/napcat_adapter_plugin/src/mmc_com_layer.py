from mofox_bus import RouteConfig, Router, TargetConfig

from src.common.logger import get_logger
from src.common.server import get_global_server
from src.plugin_system.apis import config_api

from .send_handler import send_handler

logger = get_logger("napcat_adapter")

router = None


def create_router(plugin_config: dict):
    """创建路由器实例"""
    global router
    platform_name = config_api.get_plugin_config(plugin_config, "maibot_server.platform_name", "qq")

    # 优先从插件配置读取 host 和 port，如果不存在则回退到全局配置
    config_host = config_api.get_plugin_config(plugin_config, "maibot_server.host", "")
    config_port = config_api.get_plugin_config(plugin_config, "maibot_server.port", 0)

    if config_host and config_port > 0:
        # 使用插件配置
        host = config_host
        port = config_port
        logger.debug(f"初始化MoFox-Bot连接，使用插件配置地址：{host}:{port}")
    else:
        # 回退到全局配置
        server = get_global_server()
        host = server.host
        port = server.port
        logger.debug(f"初始化MoFox-Bot连接，使用全局配置地址：{host}:{port}")

    route_config = RouteConfig(
        route_config={
            platform_name: TargetConfig(
                url=f"ws://{host}:{port}/ws",
                token=None,
            )
        }
    )
    router = Router(route_config)
    return router


async def mmc_start_com(plugin_config: dict | None = None):
    """启动MoFox-Bot连接"""
    logger.debug("正在连接MoFox-Bot")
    if plugin_config:
        create_router(plugin_config)

    if router:
        router.register_class_handler(send_handler.handle_message)
        await router.run()


async def mmc_stop_com():
    """停止MoFox-Bot连接"""
    if router:
        await router.stop()
