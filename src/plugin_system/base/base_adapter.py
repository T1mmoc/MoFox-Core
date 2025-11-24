"""
插件系统 Adapter 基类

提供插件化的适配器支持，包装 mofox_bus.AdapterBase，
添加插件生命周期、配置管理、自动启动等特性。
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from mofox_bus import AdapterBase as MoFoxAdapterBase, CoreSink, MessageEnvelope, ProcessCoreSink

if TYPE_CHECKING:
    from src.plugin_system.base.base_plugin import BasePlugin

from src.common.logger import get_logger

logger = get_logger("plugin.adapter")


class BaseAdapter(MoFoxAdapterBase, ABC):
    """
    插件系统的 Adapter 基类
    
    相比 mofox_bus.AdapterBase，增加了以下特性：
    1. 插件生命周期管理 (on_adapter_loaded, on_adapter_unloaded)
    2. 配置管理集成
    3. 自动重连与健康检查
    4. 子进程启动支持
    """

    # 适配器元数据
    adapter_name: str = "unknown_adapter"
    adapter_version: str = "0.0.1"
    adapter_author: str = "Unknown"
    adapter_description: str = "No description"
    
    # 是否在子进程中运行
    run_in_subprocess: bool = True
    
    # 子进程启动脚本路径（相对于插件目录）
    subprocess_entry: Optional[str] = None

    def __init__(
        self,
        core_sink: CoreSink,
        plugin: Optional[BasePlugin] = None,
        **kwargs
    ):
        """
        Args:
            core_sink: 核心消息接收器
            plugin: 所属插件实例（可选）
            **kwargs: 传递给 AdapterBase 的其他参数
        """
        super().__init__(core_sink, **kwargs)
        self.plugin = plugin
        self._config: Dict[str, Any] = {}
        self._health_check_task: Optional[asyncio.Task] = None
        self._running = False
        # 标记是否在子进程中运行（由核心管理器传入 ProcessCoreSink 时自动生效）
        self._is_subprocess = isinstance(core_sink, ProcessCoreSink)

    @classmethod
    def from_process_queues(
        cls,
        to_core_queue,
        from_core_queue,
        plugin: Optional["BasePlugin"] = None,
        **kwargs: Any,
    ) -> "BaseAdapter":
        """
        子进程入口便捷构造：使用 multiprocessing.Queue 与核心建立 ProcessCoreSink 通讯。

        Args:
            to_core_queue: 发往核心的 multiprocessing.Queue
            from_core_queue: 核心回传的 multiprocessing.Queue
            plugin: 可选插件实例
            **kwargs: 透传给适配器构造函数
        """
        sink = ProcessCoreSink(to_core_queue=to_core_queue, from_core_queue=from_core_queue)
        return cls(core_sink=sink, plugin=plugin, **kwargs)

    @property
    def config(self) -> Dict[str, Any]:
        """获取适配器配置"""
        if self.plugin and hasattr(self.plugin, "config"):
            return self.plugin.config
        return self._config

    @config.setter
    def config(self, value: Dict[str, Any]) -> None:
        """设置适配器配置"""
        self._config = value

    def get_config(self, key: str, default: Any = None) -> Any:
        """获取适配器配置，优先使用插件配置，其次使用内部配置。"""
        current = self.config or {}
        for part in key.split("."):
            if isinstance(current, dict) and part in current:
                current = current[part]
            else:
                return default
        return current


    async def start(self) -> None:
        """启动适配器"""
        logger.info(f"启动适配器: {self.adapter_name} v{self.adapter_version}")
        
        # 调用生命周期钩子
        await self.on_adapter_loaded()
        
        # 调用父类启动
        await super().start()
        
        # 启动健康检查
        if self.config.get("enable_health_check", False):
            self._health_check_task = asyncio.create_task(self._health_check_loop())
        
        self._running = True
        logger.info(f"适配器 {self.adapter_name} 启动成功")

    async def stop(self) -> None:
        """停止适配器"""
        logger.info(f"停止适配器: {self.adapter_name}")
        
        self._running = False
        
        # 停止健康检查
        if self._health_check_task and not self._health_check_task.done():
            self._health_check_task.cancel()
            try:
                await self._health_check_task
            except asyncio.CancelledError:
                pass
        
        # 调用父类停止
        await super().stop()
        
        # 调用生命周期钩子
        await self.on_adapter_unloaded()
        
        logger.info(f"适配器 {self.adapter_name} 已停止")

    async def on_adapter_loaded(self) -> None:
        """
        适配器加载时的钩子
        子类可重写以执行初始化逻辑
        """
        pass

    async def on_adapter_unloaded(self) -> None:
        """
        适配器卸载时的钩子
        子类可重写以执行清理逻辑
        """
        pass

    async def _health_check_loop(self) -> None:
        """健康检查循环"""
        interval = self.config.get("health_check_interval", 30)
        
        while self._running:
            try:
                await asyncio.sleep(interval)
                
                # 执行健康检查
                is_healthy = await self.health_check()
                
                if not is_healthy:
                    logger.warning(f"适配器 {self.adapter_name} 健康检查失败，尝试重连...")
                    await self.reconnect()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"适配器 {self.adapter_name} 健康检查异常: {e}", exc_info=True)

    async def health_check(self) -> bool:
        """
        健康检查
        子类可重写以实现自定义检查逻辑
        
        Returns:
            bool: 是否健康
        """
        # 默认检查 WebSocket 连接状态
        if self._ws and not self._ws.closed:
            return True
        return False

    async def reconnect(self) -> None:
        """
        重新连接
        子类可重写以实现自定义重连逻辑
        """
        try:
            await self.stop()
            await asyncio.sleep(2)  # 等待一段时间再重连
            await self.start()
        except Exception as e:
            logger.error(f"适配器 {self.adapter_name} 重连失败: {e}", exc_info=True)

    def get_subprocess_entry_path(self) -> Optional[Path]:
        """
        获取子进程启动脚本的完整路径
        
        Returns:
            Path | None: 脚本路径，如果不存在则返回 None
        """
        if not self.subprocess_entry:
            return None
        
        if not self.plugin:
            return None
        
        # 获取插件目录
        plugin_dir = Path(self.plugin.__file__).parent
        entry_path = plugin_dir / self.subprocess_entry
        
        if entry_path.exists():
            return entry_path
        
        logger.warning(f"子进程入口脚本不存在: {entry_path}")
        return None

    @classmethod
    def get_adapter_info(cls) -> "AdapterInfo":
        """获取适配器的信息
        
        Returns:
            AdapterInfo: 适配器组件信息
        """
        from src.plugin_system.base.component_types import AdapterInfo
        
        return AdapterInfo(
            name=getattr(cls, "adapter_name", cls.__name__.lower().replace("adapter", "")),
            version=getattr(cls, "adapter_version", "1.0.0"),
            platform=getattr(cls, "platform", "unknown"),
            description=getattr(cls, "adapter_description", ""),
            enabled=True,
            run_in_subprocess=getattr(cls, "run_in_subprocess", False),
            subprocess_entry=getattr(cls, "subprocess_entry", None),
        )

    @abstractmethod
    async def from_platform_message(self, raw: Any) -> MessageEnvelope:
        """
        将平台原始消息转换为 MessageEnvelope
        
        子类必须实现此方法
        
        Args:
            raw: 平台原始消息
            
        Returns:
            MessageEnvelope: 统一的消息信封
        """
        raise NotImplementedError

    async def _send_platform_message(self, envelope: MessageEnvelope) -> None:
        """
        发送消息到平台
        
        如果使用了 WebSocketAdapterOptions 或 HttpAdapterOptions，
        此方法会自动处理。否则子类需要重写此方法。
        
        Args:
            envelope: 要发送的消息信封
        """
        # 如果配置了自动传输，调用父类方法
        if self._transport_config:
            await super()._send_platform_message(envelope)
        else:
            raise NotImplementedError(
                f"适配器 {self.adapter_name} 未配置自动传输，必须重写 _send_platform_message 方法"
            )


__all__ = ["BaseAdapter"]
