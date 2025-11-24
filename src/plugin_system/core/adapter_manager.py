"""
Adapter 管理器

负责管理所有注册的适配器，支持子进程自动启动和生命周期管理。
"""

from __future__ import annotations

import asyncio
import importlib
import multiprocessing as mp
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from src.plugin_system.base.base_adapter import BaseAdapter

from mofox_bus import ProcessCoreSinkServer
from src.common.core_sink import get_core_sink
from src.common.logger import get_logger

logger = get_logger("adapter_manager")




def _load_class(module_name: str, class_name: str):
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def _adapter_process_entry(
    adapter_path: tuple[str, str],
    plugin_info: dict | None,
    incoming_queue: mp.Queue,
    outgoing_queue: mp.Queue,
):
    import asyncio
    import contextlib
    from mofox_bus import ProcessCoreSink

    async def _run() -> None:
        adapter_cls = _load_class(*adapter_path)
        plugin_instance = None
        if plugin_info:
            plugin_cls = _load_class(plugin_info["module"], plugin_info["class"])
            plugin_instance = plugin_cls(plugin_info["plugin_dir"], plugin_info["metadata"])
        core_sink = ProcessCoreSink(to_core_queue=incoming_queue, from_core_queue=outgoing_queue)
        adapter = adapter_cls(core_sink, plugin=plugin_instance)
        await adapter.start()
        try:
            while not getattr(core_sink, "_closed", False):
                await asyncio.sleep(0.2)
        finally:
            with contextlib.suppress(Exception):
                await adapter.stop()
            with contextlib.suppress(Exception):
                await core_sink.close()

    asyncio.run(_run())



class AdapterProcess:
    """适配器子进程封装：管理子进程的生命周期与通信桥接"""

    def __init__(self, adapter_cls: "type[BaseAdapter]", plugin, core_sink) -> None:
        self.adapter_cls = adapter_cls
        self.adapter_name = adapter_cls.adapter_name
        self.plugin = plugin
        self.process: mp.Process | None = None
        self._ctx = mp.get_context("spawn")
        self._incoming_queue: mp.Queue = self._ctx.Queue()
        self._outgoing_queue: mp.Queue = self._ctx.Queue()
        self._bridge: ProcessCoreSinkServer | None = None
        self._core_sink = core_sink
        self._adapter_path: tuple[str, str] = (adapter_cls.__module__, adapter_cls.__name__)
        self._plugin_info = self._extract_plugin_info(plugin)
        self._outgoing_handler = None

    @staticmethod
    def _extract_plugin_info(plugin) -> dict | None:
        if plugin is None:
            return None
        return {
            "module": plugin.__class__.__module__,
            "class": plugin.__class__.__name__,
            "plugin_dir": getattr(plugin, "plugin_dir", ""),
            "metadata": getattr(plugin, "plugin_meta", None),
        }

    def _make_outgoing_handler(self):
        async def _handler(envelope):
            if self._bridge:
                await self._bridge.push_outgoing(envelope)
        return _handler

    async def start(self) -> bool:
        """启动适配器子进程"""
        try:
            logger.info(f"启动适配器子进程: {self.adapter_name}")
            self._bridge = ProcessCoreSinkServer(
                incoming_queue=self._incoming_queue,
                outgoing_queue=self._outgoing_queue,
                core_handler=self._core_sink.send,
                name=self.adapter_name,
            )
            self._bridge.start()
            if hasattr(self._core_sink, "set_outgoing_handler"):
                self._outgoing_handler = self._make_outgoing_handler()
                try:
                    self._core_sink.set_outgoing_handler(self._outgoing_handler)
                except Exception:
                    logger.exception("Failed to register outgoing bridge for %s", self.adapter_name)
            self.process = self._ctx.Process(
                target=_adapter_process_entry,
                args=(self._adapter_path, self._plugin_info, self._incoming_queue, self._outgoing_queue),
                name=f"{self.adapter_name}-proc",
            )
            self.process.start()
            logger.info(f"启动适配器子进程 {self.adapter_name} (PID: {self.process.pid})")
            return True
        except Exception as e:
            logger.error(f"启动适配器子进程 {self.adapter_name} 失败: {e}", exc_info=True)
            return False

    async def stop(self) -> None:
        """停止适配器子进程"""
        if not self.process:
            return
        logger.info(f"停止适配器子进程: {self.adapter_name} (PID: {self.process.pid})")
        try:
            remover = getattr(self._core_sink, "remove_outgoing_handler", None)
            if callable(remover) and self._outgoing_handler:
                try:
                    remover(self._outgoing_handler)
                except Exception:
                    logger.exception(f"移除 {self.adapter_name} 的 outgoing bridge 失败")
            if self._bridge:
                await self._bridge.close()
            if self.process.is_alive():
                self.process.join(timeout=5.0)
            if self.process.is_alive():
                logger.warning(f"适配器 {self.adapter_name} 未能及时停止，强制终止中")
                self.process.terminate()
                self.process.join()
        except Exception as e:
            logger.error(f"停止适配器子进程 {self.adapter_name} 时发生错误: {e}", exc_info=True)
        finally:
            self.process = None

    def is_running(self) -> bool:
        """适配器是否正在运行"""
        if not self.process:
            return False
        return self.process.is_alive()

class AdapterManager:
    """适配器管理器"""

    def __init__(self):
        # 注册信息：name -> (adapter class, plugin instance | None)
        self._adapter_defs: Dict[str, tuple[type[BaseAdapter], object | None]] = {}
        self._adapter_processes: Dict[str, AdapterProcess] = {}
        self._in_process_adapters: Dict[str, BaseAdapter] = {}

    def register_adapter(self, adapter_cls: type[BaseAdapter], plugin=None) -> None:
        """
        注册适配器

        Args:
            adapter_cls: 适配器类
            plugin: 可选 Plugin 实例
        """
        adapter_name = getattr(adapter_cls, 'adapter_name', adapter_cls.__name__)

        if adapter_name in self._adapter_defs:
            logger.warning(f"适配器 {adapter_name} 已注册，已覆盖")

        self._adapter_defs[adapter_name] = (adapter_cls, plugin)
        adapter_version = getattr(adapter_cls, 'adapter_version', 'unknown')
        logger.info(f"注册适配器: {adapter_name} v{adapter_version}")

    async def start_adapter(self, adapter_name: str) -> bool:
        """启动指定适配器"""
        definition = self._adapter_defs.get(adapter_name)
        if not definition:
            logger.error(f"适配器 {adapter_name} 未注册")
            return False
        adapter_cls, plugin = definition
        run_in_subprocess = getattr(adapter_cls, "run_in_subprocess", False)

        if run_in_subprocess:
            return await self._start_adapter_subprocess(adapter_name, adapter_cls, plugin)
        return await self._start_adapter_in_process(adapter_name, adapter_cls, plugin)

    async def _start_adapter_subprocess(self, adapter_name: str, adapter_cls: type[BaseAdapter], plugin) -> bool:
        """在子进程中启动适配器"""
        try:
            core_sink = get_core_sink()
        except Exception as e:
            logger.error(f"无法获取 core_sink，启动子进程 {adapter_name} 失败: {e}", exc_info=True)
            return False

        adapter_process = AdapterProcess(adapter_cls, plugin, core_sink)
        success = await adapter_process.start()

        if success:
            self._adapter_processes[adapter_name] = adapter_process

        return success

    async def _start_adapter_in_process(self, adapter_name: str, adapter_cls: type[BaseAdapter], plugin) -> bool:
        """在当前进程中启动适配器"""
        try:
            core_sink = get_core_sink()
            adapter = adapter_cls(core_sink, plugin=plugin)  # type: ignore[call-arg]
            await adapter.start()
            self._in_process_adapters[adapter_name] = adapter
            logger.info(f"适配器 {adapter_name} 已在当前进程启动")
            return True
        except Exception as e:
            logger.error(f"启动适配器 {adapter_name} 失败: {e}", exc_info=True)
            return False

    async def stop_adapter(self, adapter_name: str) -> None:
        """
        停止指定的适配器
        
        Args:
            adapter_name: 适配器名称
        """
        # 检查是否在子进程中运行
        if adapter_name in self._adapter_processes:
            adapter_process = self._adapter_processes.pop(adapter_name)
            await adapter_process.stop()

        # 检查是否在主进程中运行
        if adapter_name in self._in_process_adapters:
            adapter = self._in_process_adapters.pop(adapter_name)
            try:
                await adapter.stop()
                logger.info(f"适配器 {adapter_name} 已从主进程中停止")
            except Exception as e:
                logger.error(f"停止适配器 {adapter_name} 时出错: {e}", exc_info=True)

    async def start_all_adapters(self) -> None:
        """启动所有已注册的适配器"""
        logger.info(f"开始启动 {len(self._adapter_defs)} 个适配器...")

        for adapter_name in list(self._adapter_defs.keys()):
            await self.start_adapter(adapter_name)

    async def stop_all_adapters(self) -> None:
        """停止所有适配器"""
        logger.info("停止所有适配器...")

        # 停止所有子进程适配器
        for adapter_name in list(self._adapter_processes.keys()):
            await self.stop_adapter(adapter_name)

        # 停止所有主进程适配器
        for adapter_name in list(self._in_process_adapters.keys()):
            await self.stop_adapter(adapter_name)

        logger.info("所有适配器已停止")

    def get_adapter(self, adapter_name: str) -> Optional[BaseAdapter]:
        """
        获取适配器实例
        
        Args:
            adapter_name: 适配器名称
            
        Returns:
            BaseAdapter | None: 适配器实例，如果不存在则返回 None
        """
        # 只返回在主进程中运行的适配器
        return self._in_process_adapters.get(adapter_name)

    def list_adapters(self) -> Dict[str, Dict[str, any]]:
        """列出适配器状态"""
        result = {}

        for adapter_name, definition in self._adapter_defs.items():
            adapter_cls, _plugin = definition
            status = {
                "name": adapter_name,
                "version": getattr(adapter_cls, "adapter_version", "unknown"),
                "platform": getattr(adapter_cls, "platform", "unknown"),
                "run_in_subprocess": getattr(adapter_cls, "run_in_subprocess", False),
                "running": False,
                "location": "unknown",
            }

            if adapter_name in self._adapter_processes:
                process = self._adapter_processes[adapter_name]
                status["running"] = process.is_running()
                status["location"] = "subprocess"
                if process.process:
                    status["pid"] = process.process.pid
            elif adapter_name in self._in_process_adapters:
                status["running"] = True
                status["location"] = "in-process"

            result[adapter_name] = status

        return result


# 全局单例
_adapter_manager: Optional[AdapterManager] = None


def get_adapter_manager() -> AdapterManager:
    """获取适配器管理器单例"""
    global _adapter_manager
    if _adapter_manager is None:
        _adapter_manager = AdapterManager()
    return _adapter_manager


__all__ = ["AdapterManager", "AdapterProcess", "get_adapter_manager"]