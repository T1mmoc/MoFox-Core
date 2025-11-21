import os

from mofox_bus import MessageServer

from src.common.logger import get_logger
from src.common.server import get_global_server
from src.config.config import global_config

global_api: MessageServer | None = None


def get_global_api() -> MessageServer:
    """
    获取全局 MessageServer 单例。
    """

    global global_api
    if global_api is not None:
        return global_api

    bus_config = global_config.message_bus
    host = os.getenv("HOST", "127.0.0.1")
    port_str = os.getenv("PORT", "8000")

    try:
        port = int(port_str)
    except ValueError:
        port = 8000

    kwargs: dict[str, object] = {
        "host": host,
        "port": port,
        "app": get_global_server().get_app(),
    }

    if bus_config.use_custom:
        kwargs["host"] = bus_config.host
        kwargs["port"] = bus_config.port
        kwargs.pop("app", None)
        if bus_config.use_wss:
            if bus_config.cert_file:
                kwargs["ssl_certfile"] = bus_config.cert_file
            if bus_config.key_file:
                kwargs["ssl_keyfile"] = bus_config.key_file

    if bus_config.auth_token:
        kwargs["enable_token"] = True
        kwargs["custom_logger"] = get_logger("mofox_bus")

    global_api = MessageServer(**kwargs)
    for token in bus_config.auth_token:
        global_api.add_valid_token(token)
    return global_api
