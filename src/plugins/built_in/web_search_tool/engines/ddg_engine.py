"""
DuckDuckGo search engine implementation
"""
from typing import Dict, List, Any
from asyncddgs import aDDGS

from src.common.logger import get_logger
from .base import BaseSearchEngine

logger = get_logger("ddg_engine")


class DDGSearchEngine(BaseSearchEngine):
    """
    DuckDuckGo搜索引擎实现
    """
    
    def is_available(self) -> bool:
        """检查DuckDuckGo搜索引擎是否可用"""
        return True  # DuckDuckGo不需要API密钥，总是可用
    
    async def search(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """执行DuckDuckGo搜索"""
        query = args["query"]
        num_results = args.get("num_results", 3)
        
        try:
            async with aDDGS() as ddgs:
                search_response = await ddgs.text(query, max_results=num_results)
            
            return [
                {
                    "title": r.get("title"),
                    "url": r.get("href"),
                    "snippet": r.get("body"),
                    "provider": "DuckDuckGo"
                }
                for r in search_response
            ]
        except Exception as e:
            logger.error(f"DuckDuckGo 搜索失败: {e}")
            return []
