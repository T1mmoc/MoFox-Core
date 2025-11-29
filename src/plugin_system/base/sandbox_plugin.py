"""
沙盒插件基类

为不受信任的第三方插件提供安全隔离的执行环境
"""
from typing import Any, ClassVar, Dict, Optional

from src.common.logger import get_logger
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.core.sandbox_environment import SandboxConfig, SandboxEnvironment, get_sandbox_environment

logger = get_logger("sandbox_plugin")


class SandboxPlugin(BasePlugin):
    """沙盒插件基类

    继承自 BasePlugin，但在沙盒环境中执行插件代码
    适用于不受信任的第三方插件

    特性：
    - 受限的模块导入（只能导入白名单模块）
    - 资源限制（CPU、内存、执行时间）
    - 受限的API访问（通过代理提供）
    - 无法访问文件系统（除非明确授权）
    - 无法进行网络请求（除非明确授权）
    """

    # 沙盒配置（子类可以覆盖）
    sandbox_config: ClassVar[SandboxConfig] = SandboxConfig(
        max_execution_time=30.0,
        max_memory_mb=256,
        max_cpu_time=10.0,
        allow_network=False,
        allow_file_read=False,
        allow_file_write=False,
        allowed_modules=[
            "json",
            "re",
            "datetime",
            "time",
            "math",
            "random",
            "collections",
            "itertools",
            "functools",
            "typing",
        ],
    )

    # 是否为沙盒插件（标识字段）
    is_sandboxed: bool = True

    def __init__(self, plugin_dir: str, metadata: Any):
        """初始化沙盒插件

        Args:
            plugin_dir: 插件目录路径
            metadata: 插件元数据
        """
        super().__init__(plugin_dir, metadata)

        # 创建沙盒环境
        self._sandbox = get_sandbox_environment(self.sandbox_config)

        logger.info(
            f"{self.log_prefix} 沙盒插件初始化完成 "
            f"(最大执行时间: {self.sandbox_config.max_execution_time}s, "
            f"最大内存: {self.sandbox_config.max_memory_mb}MB)"
        )

    async def execute_in_sandbox(
        self,
        code: str,
        context: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """在沙盒环境中执行代码

        Args:
            code: 要执行的Python代码
            context: 执行上下文（变量字典）
            timeout: 超时时间（秒）

        Returns:
            Dict[str, Any]: 执行结果，包含 'success', 'result', 'error' 等字段
        """
        logger.debug(f"{self.log_prefix} 在沙盒中执行代码")

        try:
            result = await self._sandbox.execute_async(
                code=code,
                context=context or {},
                timeout=timeout,
            )

            if not result.get("success"):
                logger.warning(
                    f"{self.log_prefix} 沙盒执行失败: {result.get('error_type')} - {result.get('error')}"
                )
            else:
                logger.debug(f"{self.log_prefix} 沙盒执行成功")

            return result

        except Exception as e:
            logger.error(f"{self.log_prefix} 沙盒执行异常: {e}")
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    def get_sandbox_safe_api(self) -> Dict[str, Any]:
        """获取沙盒安全的API接口

        返回一个字典，包含插件可以安全使用的API函数
        子类可以覆盖此方法来提供自定义的API

        Returns:
            Dict[str, Any]: API函数字典
        """
        from src.plugin_system.apis import logging_api

        # 默认提供的安全API
        safe_api = {
            # 日志API（只读，安全）
            "log_info": lambda msg: logging_api.get_logger(self.plugin_name).info(msg),
            "log_warning": lambda msg: logging_api.get_logger(self.plugin_name).warning(msg),
            "log_error": lambda msg: logging_api.get_logger(self.plugin_name).error(msg),
            "log_debug": lambda msg: logging_api.get_logger(self.plugin_name).debug(msg),
            # 插件信息（只读，安全）
            "get_plugin_name": lambda: self.plugin_name,
            "get_plugin_version": lambda: self.plugin_version,
            "get_plugin_config": lambda key, default=None: self.config.get(key, default),
        }

        return safe_api

    async def on_plugin_loaded(self):
        """插件加载时的钩子

        在沙盒插件中，此方法也会受到一定限制
        """
        logger.info(f"{self.log_prefix} 沙盒插件已加载")

        # 子类可以覆盖此方法来执行自定义初始化
        # 但需要注意：此方法也在沙盒环境中执行

    async def on_plugin_unloaded(self):
        """插件卸载时的钩子

        在沙盒插件中，此方法也会受到一定限制
        """
        logger.info(f"{self.log_prefix} 沙盒插件正在卸载")

        # 子类可以覆盖此方法来执行自定义清理
        # 但需要注意：此方法也在沙盒环境中执行


class SandboxPluginInfo:
    """沙盒插件信息

    用于描述沙盒插件的安全级别和限制
    """

    def __init__(
        self,
        plugin_name: str,
        trust_level: str = "untrusted",  # 信任级别：untrusted, limited, trusted
        max_execution_time: float = 30.0,
        max_memory_mb: int = 256,
        allow_network: bool = False,
        allow_file_access: bool = False,
        allowed_apis: Optional[list[str]] = None,
    ):
        self.plugin_name = plugin_name
        self.trust_level = trust_level
        self.max_execution_time = max_execution_time
        self.max_memory_mb = max_memory_mb
        self.allow_network = allow_network
        self.allow_file_access = allow_file_access
        self.allowed_apis = allowed_apis or []

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            "plugin_name": self.plugin_name,
            "trust_level": self.trust_level,
            "max_execution_time": self.max_execution_time,
            "max_memory_mb": self.max_memory_mb,
            "allow_network": self.allow_network,
            "allow_file_access": self.allow_file_access,
            "allowed_apis": self.allowed_apis,
        }
