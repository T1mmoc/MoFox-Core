"""
Napcat 适配器（基于 MoFox-Bus 完全重写版）

核心流程：
1. Napcat WebSocket 连接 → 接收 OneBot 格式消息
2. from_platform_message: OneBot dict → MessageEnvelope
3. CoreSink → 推送到 MoFox-Bot 核心
4. 核心回复 → _send_platform_message: MessageEnvelope → OneBot API 调用
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, ClassVar, Dict, List, Optional

import orjson
import websockets

from mofox_bus import CoreSink, MessageEnvelope, WebSocketAdapterOptions
from src.common.logger import get_logger
from src.plugin_system import register_plugin
from src.plugin_system.base import BaseAdapter, BasePlugin
from src.plugin_system.apis import config_api

from .src.handlers import utils as handler_utils
from .src.handlers.to_core.message_handler import MessageHandler
from .src.handlers.to_core.notice_handler import NoticeHandler
from .src.handlers.to_core.meta_event_handler import MetaEventHandler
from .src.handlers.to_napcat.send_handler import SendHandler

logger = get_logger("napcat_adapter")


class NapcatAdapter(BaseAdapter):
    """Napcat 适配器 - 完全基于 mofox-bus 架构"""

    adapter_name = "napcat_adapter"
    adapter_version = "2.0.0"
    adapter_author = "MoFox Team"
    adapter_description = "基于 MoFox-Bus 的 Napcat/OneBot 11 适配器"
    platform = "qq"

    run_in_subprocess = False

    def __init__(self, core_sink: CoreSink, plugin: Optional[BasePlugin] = None):
        """初始化 Napcat 适配器"""
        # 从插件配置读取 WebSocket URL
        if plugin:
            host = config_api.get_plugin_config(plugin.config, "napcat_server.host", "localhost")
            port = config_api.get_plugin_config(plugin.config, "napcat_server.port", 8095)
            access_token = config_api.get_plugin_config(plugin.config, "napcat_server.access_token", "")

            ws_url = f"ws://{host}:{port}"
            
            headers = {}
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
        else:
            ws_url = "ws://127.0.0.1:8095"
            headers = {}

        # 配置 WebSocket 传输
        transport = WebSocketAdapterOptions(
            url=ws_url,
            headers=headers if headers else None,
        )

        super().__init__(core_sink, plugin=plugin, transport=transport)

        # 初始化处理器
        self.message_handler = MessageHandler(self)
        self.notice_handler = NoticeHandler(self)
        self.meta_event_handler = MetaEventHandler(self)
        self.send_handler = SendHandler(self)

        # 响应池：用于存储等待的 API 响应
        self._response_pool: Dict[str, asyncio.Future] = {}
        self._response_timeout = 30.0

        # WebSocket 连接（用于发送 API 请求）
        # 注意：_ws 继承自 BaseAdapter，是 WebSocketLike 协议类型
        self._napcat_ws = None  # 可选的额外连接引用

        # 注册 utils 内部使用的适配器实例，便于工具方法自动获取 WS
        handler_utils.register_adapter(self)

    async def on_adapter_loaded(self) -> None:
        """适配器加载时的初始化"""
        logger.info("Napcat 适配器正在启动...")
        
        # 设置处理器配置
        if self.plugin:
            self.message_handler.set_plugin_config(self.plugin.config)
            self.notice_handler.set_plugin_config(self.plugin.config)
            self.meta_event_handler.set_plugin_config(self.plugin.config)
            self.send_handler.set_plugin_config(self.plugin.config)

        logger.info("Napcat 适配器已加载")

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载时的清理"""
        logger.info("Napcat 适配器正在关闭...")
        
        # 清理响应池
        for future in self._response_pool.values():
            if not future.done():
                future.cancel()
        self._response_pool.clear()

        logger.info("Napcat 适配器已关闭")

    async def from_platform_message(self, raw: Dict[str, Any]) -> MessageEnvelope:  # type: ignore[override]
        """
        将 Napcat/OneBot 原始消息转换为 MessageEnvelope
        
        这是核心转换方法，处理：
        - message 事件 → 消息
        - notice 事件 → 通知（戳一戳、表情回复等）
        - meta_event 事件 → 元事件（心跳、生命周期）
        - API 响应 → 存入响应池
        """
        post_type = raw.get("post_type")

        # API 响应（没有 post_type，有 echo）
        if post_type is None and "echo" in raw:
            echo = raw.get("echo")
            if echo and echo in self._response_pool:
                future = self._response_pool[echo]
                if not future.done():
                    future.set_result(raw)

        # 消息事件
        if post_type == "message":
            return await self.message_handler.handle_raw_message(raw)  # type: ignore[return-value]

        # 通知事件
        elif post_type == "notice":
            return await self.notice_handler.handle_notice(raw)  # type: ignore[return-value]

        # 元事件
        elif post_type == "meta_event":
            return await self.meta_event_handler.handle_meta_event(raw)  # type: ignore[return-value]

        # 未知事件类型
        else:
            logger.warning(f"未知的事件类型: {post_type}")

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:  # type: ignore[override]
        """
        将 MessageEnvelope 转换并发送到 Napcat
        
        这里不直接通过 WebSocket 发送 envelope，
        而是调用 Napcat API（send_group_msg, send_private_msg 等）
        """
        await self.send_handler.handle_message(envelope)

    async def send_napcat_api(self, action: str, params: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        """
        发送 Napcat API 请求并等待响应
        
        Args:
            action: API 动作名称（如 send_group_msg）
            params: API 参数
            timeout: 超时时间（秒）
        
        Returns:
            API 响应数据
        """
        if not self._ws:
            raise RuntimeError("WebSocket 连接未建立")

        # 生成唯一的 echo ID
        echo = str(uuid.uuid4())

        # 创建 Future 用于等待响应
        future = asyncio.Future()
        self._response_pool[echo] = future

        # 构造请求
        request = orjson.dumps({
            "action": action,
            "params": params,
            "echo": echo,
        })

        try:
            # 发送请求
            await self._ws.send(request)

            # 等待响应
            response = await asyncio.wait_for(future, timeout=timeout)
            return response

        except asyncio.TimeoutError:
            logger.error(f"API 请求超时: {action}")
            raise
        except Exception as e:
            logger.error(f"API 请求失败: {action}, 错误: {e}")
            raise
        finally:
            # 清理响应池
            self._response_pool.pop(echo, None)

    def get_ws_connection(self):
        """获取 WebSocket 连接（用于发送 API 请求）"""
        if not self._ws:
            raise RuntimeError("WebSocket 连接未建立")
        return self._ws


@register_plugin
class NapcatAdapterPlugin(BasePlugin):
    """Napcat 适配器插件"""

    plugin_name = "napcat_adapter_plugin"
    enable_plugin = True
    plugin_version = "2.0.0"
    plugin_author = "MoFox Team"
    plugin_description = "Napcat/OneBot 11 适配器（基于 MoFox-Bus 重写）"

    # 配置 Schema
    config_schema: ClassVar[dict] = {
        "plugin": {
            "name": {"type": str, "default": "napcat_adapter_plugin"},
            "version": {"type": str, "default": "1.0.0"},
            "enabled": {"type": bool, "default": True},
        },
        "napcat_server": {
            "host": {"type": str, "default": "localhost"},
            "port": {"type": int, "default": 8095},
            "access_token": {"type": str, "default": ""},
        },
        "features": {
            "group_list_type": {"type": str, "default": "blacklist"},
            "group_list": {"type": list, "default": []},
            "private_list_type": {"type": str, "default": "blacklist"},
            "private_list": {"type": list, "default": []},
            "ban_user_id": {"type": list, "default": []},
            "ban_qq_bot": {"type": bool, "default": False},
        },
    }

    def __init__(self):
        self._adapter: Optional[NapcatAdapter] = None

    async def on_plugin_loaded(self):
        """插件加载时启动适配器"""
        logger.info("Napcat 适配器插件正在加载...")

        # 从 CoreSinkManager 获取 InProcessCoreSink
        from src.common.core_sink_manager import get_core_sink_manager
        
        core_sink_manager = get_core_sink_manager()
        core_sink = core_sink_manager.get_in_process_sink()

        # 创建并启动适配器
        self._adapter = NapcatAdapter(core_sink, plugin=self)
        await self._adapter.start()

        logger.info("Napcat 适配器插件已加载")

    async def on_plugin_unloaded(self):
        """插件卸载时停止适配器"""
        if self._adapter:
            await self._adapter.stop()
        logger.info("Napcat 适配器插件已卸载")

    def get_plugin_components(self) -> list:
        """返回适配器组件"""
        return [(NapcatAdapter.get_adapter_info(), NapcatAdapter)]
