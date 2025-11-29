"""
沙盒执行环境

提供受限的Python执行环境，用于隔离不受信任的插件代码
"""
import asyncio
import resource
import signal
import sys
import time
from contextlib import contextmanager
from typing import Any, Dict, Optional

from src.common.logger import get_logger

logger = get_logger("sandbox")


class SandboxConfig:
    """沙盒配置"""

    def __init__(
        self,
        max_execution_time: float = 30.0,  # 最大执行时间（秒）
        max_memory_mb: int = 256,  # 最大内存（MB）
        max_cpu_time: float = 10.0,  # 最大CPU时间（秒）
        allow_network: bool = False,  # 是否允许网络访问
        allow_file_read: bool = False,  # 是否允许文件读取
        allow_file_write: bool = False,  # 是否允许文件写入
        allowed_modules: Optional[list[str]] = None,  # 允许导入的模块白名单
    ):
        self.max_execution_time = max_execution_time
        self.max_memory_mb = max_memory_mb
        self.max_cpu_time = max_cpu_time
        self.allow_network = allow_network
        self.allow_file_read = allow_file_read
        self.allow_file_write = allow_file_write
        self.allowed_modules = allowed_modules or [
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
        ]


class SandboxTimeoutError(Exception):
    """沙盒执行超时异常"""

    pass


class SandboxMemoryError(Exception):
    """沙盒内存超限异常"""

    pass


class SandboxSecurityError(Exception):
    """沙盒安全违规异常"""

    pass


class RestrictedImporter:
    """受限的导入器，只允许导入白名单中的模块"""

    def __init__(self, allowed_modules: list[str]):
        self.allowed_modules = set(allowed_modules)
        self.original_import = __builtins__.__import__

    def __call__(self, name: str, *args, **kwargs):
        # 检查模块是否在白名单中
        base_module = name.split(".")[0]
        if base_module not in self.allowed_modules:
            raise SandboxSecurityError(f"模块 '{name}' 不在允许的导入列表中")
        return self.original_import(name, *args, **kwargs)


class SandboxEnvironment:
    """沙盒执行环境"""

    def __init__(self, config: Optional[SandboxConfig] = None):
        self.config = config or SandboxConfig()
        self._start_time: Optional[float] = None
        self._original_import = None

    @contextmanager
    def _timeout_context(self, timeout: float):
        """超时上下文管理器"""

        def timeout_handler(signum, frame):
            raise SandboxTimeoutError(f"执行超时（{timeout}秒）")

        # 仅在支持 signal.SIGALRM 的系统上设置
        if hasattr(signal, "SIGALRM"):
            old_handler = signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(int(timeout))
            try:
                yield
            finally:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, old_handler)
        else:
            # Windows 不支持 SIGALRM，使用时间检查
            start_time = time.time()
            yield
            if time.time() - start_time > timeout:
                raise SandboxTimeoutError(f"执行超时（{timeout}秒）")

    def _set_resource_limits(self):
        """设置资源限制（仅Unix/Linux系统）"""
        if not hasattr(resource, "RLIMIT_AS"):
            logger.warning("当前系统不支持内存限制")
            return

        try:
            # 设置内存限制
            max_memory_bytes = self.config.max_memory_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (max_memory_bytes, max_memory_bytes))

            # 设置CPU时间限制
            resource.setrlimit(resource.RLIMIT_CPU, (int(self.config.max_cpu_time), int(self.config.max_cpu_time)))

            logger.debug(f"已设置资源限制: 内存={self.config.max_memory_mb}MB, CPU时间={self.config.max_cpu_time}s")
        except Exception as e:
            logger.warning(f"设置资源限制失败: {e}")

    def _create_restricted_globals(self) -> Dict[str, Any]:
        """创建受限的全局命名空间"""
        # 基础安全的内置函数
        safe_builtins = {
            "abs": abs,
            "all": all,
            "any": any,
            "bool": bool,
            "dict": dict,
            "enumerate": enumerate,
            "filter": filter,
            "float": float,
            "int": int,
            "len": len,
            "list": list,
            "map": map,
            "max": max,
            "min": min,
            "print": print,  # 允许打印（输出会被捕获）
            "range": range,
            "reversed": reversed,
            "round": round,
            "set": set,
            "sorted": sorted,
            "str": str,
            "sum": sum,
            "tuple": tuple,
            "zip": zip,
        }

        # 危险函数黑名单
        dangerous_builtins = [
            "eval",  # 动态执行代码
            "exec",  # 动态执行代码
            "compile",  # 编译代码
            "open",  # 文件操作
            "__import__",  # 动态导入
            "input",  # 用户输入
            "breakpoint",  # 调试器
            "exit",  # 退出程序
            "quit",  # 退出程序
            "help",  # 帮助系统
            "dir",  # 对象属性查看
            "vars",  # 变量查看
            "globals",  # 全局变量
            "locals",  # 局部变量
            "delattr",  # 删除属性
            "setattr",  # 设置属性
            "getattr",  # 获取属性
        ]

        restricted_globals = {
            "__builtins__": safe_builtins,
            "__name__": "__sandbox__",
            "__doc__": None,
        }

        # 添加受限的导入功能
        if self.config.allowed_modules:
            restricted_globals["__builtins__"]["__import__"] = RestrictedImporter(self.config.allowed_modules)

        return restricted_globals

    async def execute_async(
        self,
        code: str,
        context: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """异步执行代码

        Args:
            code: 要执行的Python代码
            context: 执行上下文（变量字典）
            timeout: 超时时间（秒），如果为None则使用配置的默认值

        Returns:
            Dict[str, Any]: 执行结果，包含 'success', 'result', 'error', 'output' 等字段
        """
        timeout = timeout or self.config.max_execution_time

        try:
            # 在单独的executor中执行以避免阻塞
            loop = asyncio.get_event_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(None, self._execute_sync, code, context),
                timeout=timeout,
            )
            return result

        except asyncio.TimeoutError:
            return {
                "success": False,
                "error": f"执行超时（{timeout}秒）",
                "error_type": "SandboxTimeoutError",
            }
        except Exception as e:
            logger.error(f"沙盒异步执行失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
            }

    def _execute_sync(
        self,
        code: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """同步执行代码（内部方法）

        Args:
            code: 要执行的Python代码
            context: 执行上下文（变量字典）

        Returns:
            Dict[str, Any]: 执行结果
        """
        import io
        import sys

        # 创建受限的全局命名空间
        restricted_globals = self._create_restricted_globals()

        # 合并用户提供的上下文
        if context:
            for key, value in context.items():
                if not key.startswith("_"):  # 不允许访问私有变量
                    restricted_globals[key] = value

        # 捕获输出
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        output_buffer = io.StringIO()
        error_buffer = io.StringIO()

        try:
            sys.stdout = output_buffer
            sys.stderr = error_buffer

            # 设置资源限制（仅Unix/Linux）
            self._set_resource_limits()

            # 编译代码
            compiled_code = compile(code, "<sandbox>", "exec")

            # 执行代码
            self._start_time = time.time()
            exec(compiled_code, restricted_globals)

            # 获取执行结果（查找返回值）
            result_value = restricted_globals.get("__result__", None)

            return {
                "success": True,
                "result": result_value,
                "output": output_buffer.getvalue(),
                "globals": {
                    k: v
                    for k, v in restricted_globals.items()
                    if not k.startswith("__") and k not in {"print", "range"}
                },
            }

        except SandboxTimeoutError as e:
            return {
                "success": False,
                "error": str(e),
                "error_type": "SandboxTimeoutError",
                "output": output_buffer.getvalue(),
            }

        except SandboxMemoryError as e:
            return {
                "success": False,
                "error": str(e),
                "error_type": "SandboxMemoryError",
                "output": output_buffer.getvalue(),
            }

        except SandboxSecurityError as e:
            return {
                "success": False,
                "error": str(e),
                "error_type": "SandboxSecurityError",
                "output": output_buffer.getvalue(),
            }

        except Exception as e:
            error_output = error_buffer.getvalue()
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
                "error_traceback": error_output,
                "output": output_buffer.getvalue(),
            }

        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


# 全局沙盒环境实例
_default_sandbox: Optional[SandboxEnvironment] = None


def get_sandbox_environment(config: Optional[SandboxConfig] = None) -> SandboxEnvironment:
    """获取全局沙盒环境实例

    Args:
        config: 沙盒配置，如果为None则使用默认配置

    Returns:
        SandboxEnvironment: 沙盒环境实例
    """
    global _default_sandbox
    if _default_sandbox is None or config is not None:
        _default_sandbox = SandboxEnvironment(config)
    return _default_sandbox
