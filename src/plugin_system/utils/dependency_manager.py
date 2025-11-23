import importlib
import importlib.util
import subprocess
import sys
from typing import Any

from packaging import version
from packaging.requirements import Requirement

from src.common.logger import get_logger
from src.plugin_system.base.component_types import PythonDependency
from src.plugin_system.utils.dependency_alias import INSTALL_NAME_TO_IMPORT_NAME

logger = get_logger("dependency_manager")


class DependencyManager:
    """Python包依赖管理器

    负责检查和自动安装插件的Python包依赖
    """

    def __init__(self, auto_install: bool = True, use_mirror: bool = False, mirror_url: str | None = None):
        """初始化依赖管理器

        Args:
            auto_install: 是否自动安装缺失的依赖
            use_mirror: 是否使用PyPI镜像源
            mirror_url: PyPI镜像源URL
        """
        # 延迟导入配置以避免循环依赖
        try:
            from src.plugin_system.utils.dependency_config import get_dependency_config

            config = get_dependency_config()

            # 优先使用配置文件中的设置，参数作为覆盖
            self.auto_install = config.auto_install if auto_install is True else auto_install
            self.use_mirror = config.use_mirror if use_mirror is False else use_mirror
            self.mirror_url = config.mirror_url if mirror_url is None else mirror_url
            self.install_timeout = config.install_timeout

        except Exception as e:
            logger.warning(f"无法加载依赖配置，使用默认设置: {e}")
            self.auto_install = auto_install
            self.use_mirror = use_mirror or False
            self.mirror_url = mirror_url or ""
            self.install_timeout = 300

    def check_dependencies(self, dependencies: Any, plugin_name: str = "") -> tuple[bool, list[str], list[str]]:
        """检查依赖包是否满足要求

        Args:
            dependencies: 依赖列表，支持字符串或PythonDependency对象
            plugin_name: 插件名称，用于日志记录

        Returns:
            Tuple[bool, List[str], List[str]]: (是否全部满足, 缺失的包, 错误信息)
        """
        missing_packages = []
        error_messages = []
        log_prefix = f"[Plugin:{plugin_name}] " if plugin_name else ""

        # 标准化依赖格式
        normalized_deps = self._normalize_dependencies(dependencies)

        for dep in normalized_deps:
            try:
                if not self._check_single_dependency(dep):
                    logger.info(f"{log_prefix}缺少依赖包: {dep.get_pip_requirement()}")
                    missing_packages.append(dep.get_pip_requirement())
            except Exception as e:
                error_msg = f"检查依赖 {dep.package_name} 时发生错误: {e!s}"
                error_messages.append(error_msg)
                logger.error(f"{log_prefix}{error_msg}")

        all_satisfied = len(missing_packages) == 0 and len(error_messages) == 0

        if all_satisfied:
            logger.debug(f"{log_prefix}所有Python依赖检查通过")
        else:
            logger.warning(
                f"{log_prefix}Python依赖检查失败: 缺失{len(missing_packages)}个包, {len(error_messages)}个错误"
            )

        return all_satisfied, missing_packages, error_messages

    def install_dependencies(self, packages: list[str], plugin_name: str = "") -> tuple[bool, list[str]]:
        """自动安装缺失的依赖包

        Args:
            packages: 要安装的包列表
            plugin_name: 插件名称，用于日志记录

        Returns:
            Tuple[bool, List[str]]: (是否全部安装成功, 失败的包列表)
        """
        if not packages:
            return True, []

        if not self.auto_install:
            logger.info(f"[Plugin:{plugin_name}] 自动安装已禁用，跳过安装: {packages}")
            return False, packages

        log_prefix = f"[Plugin:{plugin_name}] " if plugin_name else ""
        logger.info(f"{log_prefix}开始自动安装Python依赖: {packages}")

        failed_packages = []

        for package in packages:
            try:
                if self._install_single_package(package, plugin_name):
                    logger.info(f"{log_prefix} 成功安装: {package}")
                else:
                    failed_packages.append(package)
                    logger.error(f"{log_prefix} 安装失败: {package}")
            except Exception as e:
                failed_packages.append(package)
                logger.error(f"{log_prefix} 安装 {package} 时发生异常: {e!s}")

        success = len(failed_packages) == 0
        if success:
            logger.info(f"{log_prefix} 所有依赖安装完成")
        else:
            logger.error(f"{log_prefix} 部分依赖安装失败: {failed_packages}")

        return success, failed_packages

    def check_and_install_dependencies(self, dependencies: Any, plugin_name: str = "") -> tuple[bool, list[str]]:
        """检查并自动安装依赖（组合操作）

        Args:
            dependencies: 依赖列表
            plugin_name: 插件名称

        Returns:
            Tuple[bool, List[str]]: (是否全部满足, 错误信息列表)
        """
        # 第一步：检查依赖
        all_satisfied, missing_packages, check_errors = self.check_dependencies(dependencies, plugin_name)

        if all_satisfied:
            return True, []

        all_errors = check_errors.copy()

        # 第二步：尝试安装缺失的包
        if missing_packages and self.auto_install:
            install_success, failed_packages = self.install_dependencies(missing_packages, plugin_name)

            if not install_success:
                all_errors.extend([f"安装失败: {pkg}" for pkg in failed_packages])
            else:
                # 安装成功后重新检查
                recheck_satisfied, recheck_missing, recheck_errors = self.check_dependencies(dependencies, plugin_name)
                if not recheck_satisfied:
                    all_errors.extend(recheck_errors)
                    all_errors.extend([f"安装后仍缺失: {pkg}" for pkg in recheck_missing])
                else:
                    return True, []
        else:
            all_errors.extend([f"缺失依赖: {pkg}" for pkg in missing_packages])

        return False, all_errors

    @staticmethod
    def _normalize_dependencies(dependencies: Any) -> list[PythonDependency]:
        """将依赖列表标准化为PythonDependency对象"""
        normalized = []

        for dep in dependencies:
            if isinstance(dep, str):
                # 解析字符串格式的依赖
                try:
                    # 尝试解析为requirement格式 (如 "package>=1.0.0")
                    req = Requirement(dep)
                    version_spec = str(req.specifier) if req.specifier else ""

                    normalized.append(
                        PythonDependency(
                            package_name=req.name,
                            version=version_spec,
                            install_name=dep,  # 保持原始的安装名称
                        )
                    )
                except Exception:
                    # 如果解析失败，作为简单包名处理
                    normalized.append(PythonDependency(package_name=dep, install_name=dep))
            elif isinstance(dep, PythonDependency):
                normalized.append(dep)
            else:
                logger.warning(f"未知的依赖格式: {dep}")

        return normalized

    @staticmethod
    def _check_single_dependency(dep: PythonDependency) -> bool:
        """检查单个依赖是否满足要求"""

        def _try_check(import_name: str) -> bool:
            """尝试使用给定的导入名进行检查"""
            try:
                spec = importlib.util.find_spec(import_name)
                if spec is None:
                    return False

                # 如果没有版本要求，导入成功就够了
                if not dep.version:
                    return True

                # 检查版本要求
                try:
                    module = importlib.import_module(import_name)
                    installed_version = getattr(module, "__version__", None)

                    if installed_version is None:
                        # 尝试其他常见的版本属性
                        installed_version = getattr(module, "VERSION", None)
                        if installed_version is None:
                            logger.debug(f"无法获取包 {import_name} 的版本信息，假设满足要求")
                            return True

                    # 解析版本要求
                    req = Requirement(f"{dep.package_name}{dep.version}")
                    return version.parse(str(installed_version)) in req.specifier

                except Exception as e:
                    logger.debug(f"检查包 {import_name} 版本时出错: {e}")
                    return True  # 如果无法检查版本，假设满足要求

            except ImportError:
                return False
            except Exception as e:
                logger.error(f"检查依赖 {import_name} 时发生未知错误: {e}")
                return False

        # 1. 首先尝试使用原始的 package_name 进行检查
        if _try_check(dep.package_name):
            return True

        # 2. 如果失败，查询别名映射表
        #    注意：此时 dep.package_name 可能是 simple "requests" 或 "beautifulsoup4"
        import_alias = INSTALL_NAME_TO_IMPORT_NAME.get(dep.package_name)
        if import_alias:
            logger.debug(f"依赖 '{dep.package_name}' 导入失败, 尝试使用别名 '{import_alias}'")
            if _try_check(import_alias):
                return True

        # 3. 如果别名也失败了，或者没有别名，最终确认失败
        return False

    def _install_single_package(self, package: str, plugin_name: str = "") -> bool:
        """安装单个包"""
        try:
            cmd = [sys.executable, "-m", "pip", "install", package]

            # 添加镜像源设置
            if self.use_mirror and self.mirror_url:
                cmd.extend(["-i", self.mirror_url])
                logger.debug(f"[Plugin:{plugin_name}] 使用PyPI镜像源: {self.mirror_url}")

            logger.debug(f"[Plugin:{plugin_name}] 执行安装命令: {' '.join(cmd)}")

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.install_timeout, check=False)

            if result.returncode == 0:
                return True
            else:
                logger.error(f"[Plugin:{plugin_name}] pip安装失败: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            logger.error(f"[Plugin:{plugin_name}] 安装 {package} 超时")
            return False
        except Exception as e:
            logger.error(f"[Plugin:{plugin_name}] 安装 {package} 时发生异常: {e}")
            return False


# 全局依赖管理器实例
_global_dependency_manager: DependencyManager | None = None


def get_dependency_manager() -> DependencyManager:
    """获取全局依赖管理器实例"""
    global _global_dependency_manager
    if _global_dependency_manager is None:
        _global_dependency_manager = DependencyManager()
    return _global_dependency_manager


def configure_dependency_manager(auto_install: bool = True, use_mirror: bool = False, mirror_url: str | None = None):
    """配置全局依赖管理器"""
    global _global_dependency_manager
    _global_dependency_manager = DependencyManager(
        auto_install=auto_install, use_mirror=use_mirror, mirror_url=mirror_url
    )
