from src.plugin_system.base.plugin_metadata import PluginMetadata

__plugin_meta__ = PluginMetadata(
    name="napcat_adapter_plugin",
    description="基于OneBot 11协议的NapCat QQ协议插件，提供完整的QQ机器人API接口，使用现有adapter连接",
    usage="该插件提供 `napcat_tool` tool。",
    version="1.0.0",
    author="Windpicker_owo",
    license="GPL-v3.0-or-later",
    repository_url="https://github.com/Windpicker-owo",
    keywords=["qq", "bot", "napcat", "onebot", "api", "websocket"],
    categories=["protocol"],
    extra={
        "is_built_in": False,
    },
)
