"""
Base search engine interface
"""
from abc import ABC, abstractmethod
from typing import Dict, List, Any


class BaseSearchEngine(ABC):
    """
    搜索引擎基类
    """
    
    @abstractmethod
    async def search(self, args: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        执行搜索
        
        Args:
            args: 搜索参数，包含 query、num_results、time_range 等
            
        Returns:
            搜索结果列表，每个结果包含 title、url、snippet、provider 字段
        """
        pass
    
    @abstractmethod
    def is_available(self) -> bool:
        """
        检查搜索引擎是否可用
        """
        pass
