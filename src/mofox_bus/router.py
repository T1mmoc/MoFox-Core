from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import asdict, dataclass
from typing import Callable, Dict, Optional

from .api import MessageClient
from .message_models import MessageBase

logger = logging.getLogger("mofox_bus.router")


@dataclass
class TargetConfig:
    url: str
    token: str | None = None
    ssl_verify: str | None = None

    def to_dict(self) -> Dict[str, str | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, str | None]) -> "TargetConfig":
        return cls(
            url=data.get("url", ""),
            token=data.get("token"),
            ssl_verify=data.get("ssl_verify"),
        )


@dataclass
class RouteConfig:
    route_config: Dict[str, TargetConfig]

    def to_dict(self) -> Dict[str, Dict[str, str | None]]:
        return {"route_config": {k: v.to_dict() for k, v in self.route_config.items()}}

    @classmethod
    def from_dict(cls, data: Dict[str, Dict[str, str | None]]) -> "RouteConfig":
        cfg = {
            platform: TargetConfig.from_dict(target)
            for platform, target in data.get("route_config", {}).items()
        }
        return cls(route_config=cfg)


class Router:
    def __init__(self, config: RouteConfig, custom_logger: logging.Logger | None = None) -> None:
        if custom_logger:
            logger.handlers = custom_logger.handlers
        self.config = config
        self.clients: Dict[str, MessageClient] = {}
        self.handlers: list[Callable[[Dict], None]] = []
        self._running = False
        self._client_tasks: Dict[str, asyncio.Task] = {}
        self._monitor_task: asyncio.Task | None = None

    async def connect(self, platform: str) -> None:
        if platform not in self.config.route_config:
            raise ValueError(f"Unknown platform {platform}")
        target = self.config.route_config[platform]
        mode = "tcp" if target.url.startswith(("tcp://", "tcps://")) else "ws"
        if mode != "ws":
            raise NotImplementedError("TCP mode is not implemented yet")
        client = MessageClient(mode="ws")
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
            self._client_tasks[platform] = asyncio.create_task(client.run())

    def register_class_handler(self, handler: Callable[[Dict], None]) -> None:
        self.handlers.append(handler)
        for client in self.clients.values():
            client.register_message_handler(handler)

    async def run(self) -> None:
        self._running = True
        for platform in self.config.route_config:
            if platform not in self.clients:
                await self.connect(platform)
        for platform, client in self.clients.items():
            if platform not in self._client_tasks:
                self._client_tasks[platform] = asyncio.create_task(client.run())
        self._monitor_task = asyncio.create_task(self._monitor_connections())
        try:
            while self._running:
                await asyncio.sleep(1)
        except asyncio.CancelledError:  # pragma: no cover
            raise

    async def _monitor_connections(self) -> None:
        await asyncio.sleep(3)
        while self._running:
            for platform in list(self.clients.keys()):
                client = self.clients.get(platform)
                if client is None:
                    continue
                if not client.is_connected():
                    logger.info("Detected disconnect from %s, attempting reconnect", platform)
                    await self._reconnect_platform(platform)
            await asyncio.sleep(5)

    async def _reconnect_platform(self, platform: str) -> None:
        await self.remove_platform(platform)
        if platform in self.config.route_config:
            await self.connect(platform)

    async def remove_platform(self, platform: str) -> None:
        if platform in self._client_tasks:
            task = self._client_tasks.pop(platform)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        client = self.clients.pop(platform, None)
        if client:
            await client.stop()

    async def stop(self) -> None:
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._monitor_task
            self._monitor_task = None
        for platform in list(self.clients.keys()):
            await self.remove_platform(platform)
        self.clients.clear()

    def get_target_url(self, message: MessageBase) -> Optional[str]:
        platform = message.message_info.platform
        if not platform:
            return None
        target = self.config.route_config.get(platform)
        return target.url if target else None

    async def send_message(self, message: MessageBase):
        platform = message.message_info.platform
        if not platform:
            raise ValueError("message_info.platform is required")
        client = self.clients.get(platform)
        if client is None:
            raise RuntimeError(f"No client connected for platform {platform}")
        return await client.send_message(message.to_dict())

    async def update_config(self, config_data: Dict[str, Dict[str, str | None]]) -> None:
        new_config = RouteConfig.from_dict(config_data)
        await self._adjust_connections(new_config)
        self.config = new_config

    async def _adjust_connections(self, new_config: RouteConfig) -> None:
        current = set(self.config.route_config.keys())
        updated = set(new_config.route_config.keys())
        for platform in current - updated:
            await self.remove_platform(platform)
        for platform in updated:
            if platform not in current:
                await self.connect(platform)
            else:
                old = self.config.route_config[platform]
                new = new_config.route_config[platform]
                if old.url != new.url or old.token != new.token:
                    await self.remove_platform(platform)
                    await self.connect(platform)
