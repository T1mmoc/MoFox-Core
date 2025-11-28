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

from mofox_wire import CoreSink, MessageEnvelope, WebSocketAdapterOptions
from src.common.logger import get_logger
from src.plugin_system import ConfigField, register_plugin
from src.plugin_system.base import BaseAdapter, BasePlugin
from src.plugin_system.apis import config_api

from .src.handlers import utils as handler_utils
from .src.handlers.to_core.message_handler import MessageHandler
from .src.handlers.to_core.notice_handler import NoticeHandler
from .src.handlers.to_core.meta_event_handler import MetaEventHandler
from .src.handlers.to_napcat.send_handler import SendHandler

logger = get_logger("napcat_adapter")


class NapcatAdapter(BaseAdapter):
    """Napcat 适配器 - 完全基于 mofox-wire 架构"""

    adapter_name = "napcat_adapter"
    adapter_version = "2.0.0"
    adapter_author = "MoFox Team"
    adapter_description = "基于 MoFox-Bus 的 Napcat/OneBot 11 适配器"
    platform = "qq"

    run_in_subprocess = True

    def __init__(self, core_sink: CoreSink, plugin: Optional[BasePlugin] = None, **kwargs):
        """初始化 Napcat 适配器"""
        # 从插件配置读取 WebSocket URL
        if plugin:
            host = config_api.get_plugin_config(plugin.config, "napcat_server.host", "localhost")
            port = config_api.get_plugin_config(plugin.config, "napcat_server.port", 8095)
            access_token = config_api.get_plugin_config(plugin.config, "napcat_server.access_token", "")
            mode_str = config_api.get_plugin_config(plugin.config, "napcat_server.mode", "reverse")
            ws_mode = "client" if mode_str == "direct" else "server"

            ws_url = f"ws://{host}:{port}"
            headers = {}
            if access_token:
                headers["Authorization"] = f"Bearer {access_token}"
        else:
            ws_url = "ws://127.0.0.1:8095"
            headers = {}
            ws_mode = "server"

        # 配置 WebSocket 传输
        transport = WebSocketAdapterOptions(
            mode=ws_mode,
            url=ws_url,
            headers=headers if headers else None,
        )

        super().__init__(core_sink, plugin=plugin, transport=transport, **kwargs)

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

        # 注册 notice 事件到 event manager
        await self._register_notice_events()

        logger.info("Napcat 适配器已加载")

    async def _register_notice_events(self) -> None:
        """注册 notice 相关事件到 event manager"""
        from src.plugin_system.core.event_manager import event_manager
        from .src.event_types import NapcatEvent

        # 定义所有 notice 事件类型
        notice_events = [
            NapcatEvent.ON_RECEIVED.POKE,
            NapcatEvent.ON_RECEIVED.EMOJI_LIEK,
            NapcatEvent.ON_RECEIVED.GROUP_UPLOAD,
            NapcatEvent.ON_RECEIVED.GROUP_BAN,
            NapcatEvent.ON_RECEIVED.GROUP_LIFT_BAN,
            NapcatEvent.ON_RECEIVED.FRIEND_RECALL,
            NapcatEvent.ON_RECEIVED.GROUP_RECALL,
            NapcatEvent.ON_RECEIVED.FRIEND_INPUT,
        ]

        # 注册所有事件
        registered_count = 0
        for event_type in notice_events:
            try:
                # 使用同步的 register_event 方法注册事件
                success = event_manager.register_event(
                    event_name=event_type,
                    allowed_triggers=["napcat_adapter_plugin"],  # 只允许此插件触发
                )
                if success:
                    registered_count += 1
                    logger.debug(f"已注册 notice 事件: {event_type}")
                else:
                    logger.debug(f"notice 事件已存在: {event_type}")
            except Exception as e:
                logger.warning(f"注册 notice 事件失败: {event_type}, 错误: {e}")

        logger.info(f"已注册 {registered_count} 个新 notice 事件类型（共 {len(notice_events)} 个）")

    async def on_adapter_unloaded(self) -> None:
        """适配器卸载时的清理"""
        logger.info("Napcat 适配器正在关闭...")
        
        # 清理响应池
        for future in self._response_pool.values():
            if not future.done():
                future.cancel()
        self._response_pool.clear()

        logger.info("Napcat 适配器已关闭")

    async def from_platform_message(self, raw: Dict[str, Any]) -> MessageEnvelope | None:  # type: ignore[override]
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

        try:
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
                return None
        except ValueError as ve:
            logger.warning(f"处理 Napcat 事件时数据无效: {ve}")
            return None
        except Exception as e:
            logger.error(f"处理 Napcat 事件失败: {e}, 原始数据: {raw}")
            return None
    
    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:  # type: ignore[override]
        """
        将 MessageEnvelope 转换并发送到 Napcat
        
        这里不直接通过 WebSocket 发送 envelope，
        而是调用 Napcat API（send_group_msg, send_private_msg 等）
        """
        try:
            await self.send_handler.handle_message(envelope)
        except Exception as e:
            logger.error(f"发送 Napcat 消息失败: {e}")

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
        # Napcat expects JSON text frames; orjson.dumps returns bytes so decode to str
        request = orjson.dumps(
            {
                "action": action,
                "params": params,
                "echo": echo,
            }
        ).decode()

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
    config_file_name = "config.toml"
    enable_plugin = True
    plugin_version = "2.0.0"
    plugin_author = "MoFox Team"
    plugin_description = "Napcat/OneBot 11 适配器（基于 MoFox-Bus 重写）"

    config_section_descriptions: ClassVar = {
        "plugin": "插件开关",
        "napcat_server": "Napcat WebSocket 连接设置",
        "features": "过滤和名单配置",
    }

    config_schema: ClassVar[dict] = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用 Napcat 适配器"),
            "config_version": ConfigField(type=str, default="2.0.0", description="配置文件版本"),
        },
        "napcat_server": {
            "mode": ConfigField(
                type=str,
                default="reverse",
                description="ws 连接模式: reverse/direct",
                choices=["reverse", "direct"],
            ),
            "host": ConfigField(type=str, default="localhost", description="Napcat WebSocket 服务地址"),
            "port": ConfigField(type=int, default=8095, description="Napcat WebSocket 服务端口"),
            "access_token": ConfigField(type=str, default="", description="Napcat API 访问令牌（可选）"),
        },
        "features": {
            "group_list_type": ConfigField(
                type=str,
                default="blacklist",
                description="群聊名单模式: blacklist/whitelist",
                choices=["blacklist", "whitelist"],
            ),
            "group_list": ConfigField(type=list, default=[], description="群聊名单；根据名单模式过滤"),
            "private_list_type": ConfigField(
                type=str,
                default="blacklist",
                description="私聊名单模式: blacklist/whitelist",
                choices=["blacklist", "whitelist"],
            ),
            "private_list": ConfigField(type=list, default=[], description="私聊名单；根据名单模式过滤"),
            "ban_user_id": ConfigField(type=list, default=[], description="全局封禁的用户 ID 列表"),
            "ban_qq_bot": ConfigField(type=bool, default=False, description="是否屏蔽其他 QQ 机器人消息"),
            "enable_poke": ConfigField(type=bool, default=True, description="是否启用戳一戳消息处理"),
            "ignore_non_self_poke": ConfigField(type=bool, default=False, description="是否忽略不是针对自己的戳一戳消息"),
            "poke_debounce_seconds": ConfigField(type=float, default=2.0, description="戳一戳防抖时间（秒）"),
            "enable_emoji_like": ConfigField(type=bool, default=True, description="是否启用群聊表情回复处理"),
            "enable_reply_at": ConfigField(type=bool, default=True, description="是否在回复时自动@原消息发送者"),
            "reply_at_rate": ConfigField(type=float, default=0.5, description="回复时@的概率（0.0-1.0）"),
            "enable_video_processing": ConfigField(type=bool, default=True, description="是否启用视频消息处理（下载和解析）"),
        },
    }

    def get_plugin_components(self) -> list:
        """返回适配器组件"""
        return [(NapcatAdapter.get_adapter_info(), NapcatAdapter)]
