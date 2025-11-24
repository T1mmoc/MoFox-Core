from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Optional

from .api import MessageClient
from .types import MessageEnvelope

logger = logging.getLogger("mofox_bus.router")


@dataclass
class TargetConfig:
    """路由目标配置，包含连接信息和认证配置"""
    url: str
    token: str | None = None
    ssl_verify: str | None = None

    def to_dict(self) -> Dict[str, str | None]:
        """转换为字典格式"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, str | None]) -> "TargetConfig":
        """从字典创建配置对象"""
        return cls(
            url=data.get("url", ""),
            token=data.get("token"),
            ssl_verify=data.get("ssl_verify"),
        )


@dataclass
class RouteConfig:
    """路由配置，包含多个平台的路由目标"""
    route_config: Dict[str, TargetConfig]

    def to_dict(self) -> Dict[str, Dict[str, str | None]]:
        """转换为字典格式"""
        return {"route_config": {k: v.to_dict() for k, v in self.route_config.items()}}

    @classmethod
    def from_dict(cls, data: Dict[str, Dict[str, str | None]]) -> "RouteConfig":
        """从字典创建路由配置对象"""
        cfg = {
            platform: TargetConfig.from_dict(target)
            for platform, target in data.get("route_config", {}).items()
        }
        return cls(route_config=cfg)


class Router:
    """消息路由器，负责管理多个平台的消息客户端连接"""

    def __init__(self, config: RouteConfig, custom_logger: logging.Logger | None = None) -> None:
        """
        初始化路由器

        Args:
            config: 路由配置
            custom_logger: 自定义日志记录器
        """
        if custom_logger:
            logger.handlers = custom_logger.handlers
        self.config = config
        self.clients: Dict[str, MessageClient] = {}
        self.handlers: list[Callable[[Dict], None]] = []
        self._running = False
        self._client_tasks: Dict[str, asyncio.Task] = {}
        self._stop_event: asyncio.Event | None = None

    async def connect(self, platform: str) -> None:
        """
        连接到指定平台

        Args:
            platform: 平台标识

        Raises:
            ValueError: 未知平台
            NotImplementedError: 不支持的模式
        """
        if platform not in self.config.route_config:
            raise ValueError(f"未知平台: {platform}")
        target = self.config.route_config[platform]
        mode = "tcp" if target.url.startswith(("tcp://", "tcps://")) else "ws"
        if mode != "ws":
            raise NotImplementedError("TCP 模式暂未实现")
        client = MessageClient(mode="ws")
        client.set_disconnect_callback(self._handle_client_disconnect)
        await client.connect(
            url=target.url,
            platform=platform,
            token=target.token,
            ssl_verify=target.ssl_verify,
        )
        for handler in self.handlers:
            client.register_message_handler(handler)
        self.clients[platform] = client
        if self._running:
            self._start_client_task(platform, client)

    def register_class_handler(self, handler: Callable[[Dict], None]) -> None:
        self.handlers.append(handler)
        for client in self.clients.values():
            client.register_message_handler(handler)

    async def run(self) -> None:
        """启动路由器，连接所有配置的平台并开始运行"""
        self._running = True
        self._stop_event = asyncio.Event()
        for platform in self.config.route_config:
            if platform not in self.clients:
                await self.connect(platform)
        for platform, client in self.clients.items():
            if platform not in self._client_tasks:
                self._start_client_task(platform, client)
        try:
            await self._stop_event.wait()
        except asyncio.CancelledError:  # pragma: no cover
            raise

    async def remove_platform(self, platform: str) -> None:
        """
        移除指定平台的连接

        Args:
            platform: 平台标识
        """
        if platform in self._client_tasks:
            task = self._client_tasks.pop(platform)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        client = self.clients.pop(platform, None)
        if client:
            await client.stop()

    async def _handle_client_disconnect(self, platform: str, reason: str) -> None:
        """
        处理客户端断开连接

        Args:
            platform: 平台标识
            reason: 断开原因
        """
        logger.info(f"平台 {platform} 的客户端断开连接: {reason} (客户端将自动重连)")
        task = self._client_tasks.get(platform)
        if task is not None and not task.done():
            return
        client = self.clients.get(platform)
        if client and self._running:
            self._start_client_task(platform, client)

    async def stop(self) -> None:
        """停止路由器，关闭所有连接"""
        self._running = False
        if self._stop_event:
            self._stop_event.set()
        for platform in list(self.clients.keys()):
            await self.remove_platform(platform)
        self.clients.clear()

    def _start_client_task(self, platform: str, client: MessageClient) -> None:
        """
        启动客户端任务

        Args:
            platform: 平台标识
            client: 消息客户端
        """
        task = asyncio.create_task(client.run())
        task.add_done_callback(lambda t, plat=platform: asyncio.create_task(self._restart_if_needed(plat, t)))
        self._client_tasks[platform] = task

    async def _restart_if_needed(self, platform: str, task: asyncio.Task) -> None:
        """
        必要时重启客户端任务

        Args:
            platform: 平台标识
            task: 已完成的任务
        """
        if not self._running:
            return
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.warning(f"平台 {platform} 的客户端任务异常结束: {exc}")
        client = self.clients.get(platform)
        if client:
            self._start_client_task(platform, client)

    def get_target_url(self, message: MessageEnvelope) -> Optional[str]:
        """
        根据消息获取目标 URL

        Args:
            message: 消息信封

        Returns:
            目标 URL 或 None
        """
        platform = message.get("message_info", {}).get("platform")
        if not platform:
            return None
        target = self.config.route_config.get(platform)
        return target.url if target else None

    async def send_message(self, message: MessageEnvelope):
        """
        发送消息到指定平台

        Args:
            message: 消息信封

        Raises:
            ValueError: 缺少平台信息
            RuntimeError: 未找到对应平台的客户端
        """
        platform = message.get("message_info", {}).get("platform")
        if not platform:
            raise ValueError("消息中缺少必需的 message_info.platform 字段")
        client = self.clients.get(platform)
        if client is None:
            raise RuntimeError(f"平台 {platform} 没有已连接的客户端")
        return await client.send_message(message)

    async def update_config(self, config_data: Dict[str, Dict[str, str | None]]) -> None:
        """
        更新路由配置

        Args:
            config_data: 新的配置数据
        """
        new_config = RouteConfig.from_dict(config_data)
        await self._adjust_connections(new_config)
        self.config = new_config

    async def _adjust_connections(self, new_config: RouteConfig) -> None:
        """
        调整连接以匹配新配置

        Args:
            new_config: 新的路由配置
        """
        current = set(self.config.route_config.keys())
        updated = set(new_config.route_config.keys())
        # 移除不再存在的平台
        for platform in current - updated:
            await self.remove_platform(platform)
        # 添加或更新平台
        for platform in updated:
            if platform not in current:
                await self.connect(platform)
            else:
                old = self.config.route_config[platform]
                new = new_config.route_config[platform]
                if old.url != new.url or old.token != new.token:
                    await self.remove_platform(platform)
                    await self.connect(platform)
