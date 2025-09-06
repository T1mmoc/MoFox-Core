"""
Exa search engine implementation
"""
import asyncio
import functools
from datetime import datetime, timedelta
from typing import Dict, List, Any
from exa_py import Exa

from src.common.logger import get_logger
from src.plugin_system.apis import config_api
from .base import BaseSearchEngine
from ..utils.api_key_manager import create_api_key_manager_from_config

logger = get_logger("exa_engine")


class ExaSearchEngine(BaseSearchEngine):
    """
    Exa搜索引擎实现
    """
    
    def __init__(self):
        self._initialize_clients()
    
    def _initialize_clients(self):
        """初始化Exa客户端"""
        # 从主配置文件读取API密钥
        exa_api_keys = config_api.get_global_config("exa.api_keys", None)
        
        # 创建API密钥管理器
        self.api_manager = create_api_key_manager_from_config(
            exa_api_keys,
            lambda key: Exa(api_key=key),
            "Exa"
        )
    
    def is_available(self) -> bool:
        """检查Exa搜索引擎是否可用"""
        return self.api_manager.is_available()
    
    async def search(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """执行Exa搜索"""
        if not self.is_available():
            return []
            
        query = args["query"]
        num_results = args.get("num_results", 3)
        time_range = args.get("time_range", "any")

        exa_args = {"num_results": num_results, "text": True, "highlights": True}
        if time_range != "any":
            today = datetime.now()
            start_date = today - timedelta(days=7 if time_range == "week" else 30)
            exa_args["start_published_date"] = start_date.strftime('%Y-%m-%d')

        try:
            # 使用API密钥管理器获取下一个客户端
            exa_client = self.api_manager.get_next_client()
            if not exa_client:
                logger.error("无法获取Exa客户端")
                return []
                
            loop = asyncio.get_running_loop()
            func = functools.partial(exa_client.search_and_contents, query, **exa_args)
            search_response = await loop.run_in_executor(None, func)
            
            return [
                {
                    "title": res.title,
                    "url": res.url,
                    "snippet": " ".join(getattr(res, 'highlights', [])) or (getattr(res, 'text', '')[:250] + '...'),
                    "provider": "Exa"
                }
                for res in search_response.results
            ]
        except Exception as e:
            logger.error(f"Exa 搜索失败: {e}")
            return []
