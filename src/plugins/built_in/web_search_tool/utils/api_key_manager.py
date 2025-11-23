"""
API密钥管理器，提供轮询机制
"""

import itertools
from collections.abc import Callable
from typing import Generic, TypeVar

from src.common.logger import get_logger

logger = get_logger("api_key_manager")

T = TypeVar("T")


class APIKeyManager(Generic[T]):
    """
    API密钥管理器，支持轮询机制
    """

    def __init__(self, api_keys: list[str], client_factory: Callable[[str], T], service_name: str = "Unknown"):
        """
        初始化API密钥管理器

        Args:
            api_keys: API密钥列表
            client_factory: 客户端工厂函数，接受API密钥参数并返回客户端实例
            service_name: 服务名称，用于日志记录
        """
        self.service_name = service_name
        self.clients: list[T] = []
        self.client_cycle: itertools.cycle | None = None

        if api_keys:
            # 过滤有效的API密钥，排除None、空字符串、"None"字符串等
            valid_keys = [
                key.strip() for key in api_keys
                if isinstance(key, str) and key.strip() and key.strip().lower() not in ("none", "null", "")
            ]

            if valid_keys:
                try:
                    self.clients = [client_factory(key) for key in valid_keys]
                    self.client_cycle = itertools.cycle(self.clients)
                    logger.info(f" {service_name} 成功加载 {len(valid_keys)} 个 API 密钥")
                except Exception as e:
                    logger.error(f"❌ 初始化 {service_name} 客户端失败: {e}")
                    self.clients = []
                    self.client_cycle = None
            else:
                logger.warning(f"⚠️  {service_name} API Keys 配置无效（包含None或空值），{service_name} 功能将不可用")
        else:
            logger.warning(f"⚠️  {service_name} API Keys 未配置，{service_name} 功能将不可用")

    def is_available(self) -> bool:
        """检查是否有可用的客户端"""
        return bool(self.clients and self.client_cycle)

    def get_next_client(self) -> T | None:
        """获取下一个客户端（轮询）"""
        if not self.is_available():

            return None
        assert self.client_cycle is not None
        return next(self.client_cycle)

    def get_client_count(self) -> int:
        """获取可用客户端数量"""
        return len(self.clients)


def create_api_key_manager_from_config(
    config_keys: list[str] | None, client_factory: Callable[[str], T], service_name: str
) -> APIKeyManager[T]:
    """
    从配置创建API密钥管理器的便捷函数

    Args:
        config_keys: 从配置读取的API密钥列表
        client_factory: 客户端工厂函数
        service_name: 服务名称

    Returns:
        API密钥管理器实例
    """
    api_keys = config_keys if isinstance(config_keys, list) else []
    return APIKeyManager(api_keys, client_factory, service_name)
