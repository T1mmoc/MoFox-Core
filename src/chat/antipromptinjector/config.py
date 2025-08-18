# -*- coding: utf-8 -*-
"""
反注入系统配置模块

本模块定义了反注入系统的检测结果和统计数据类。
配置直接从 global_config.anti_prompt_injection 获取。
"""

import time
from typing import List, Optional
from dataclasses import dataclass, field
from enum import Enum


class ProcessResult(Enum):
    """处理结果枚举"""
    ALLOWED = "allowed"           # 允许通过
    BLOCKED_INJECTION = "blocked_injection"  # 被阻止-注入攻击
    BLOCKED_BAN = "blocked_ban"   # 被阻止-用户封禁
    SHIELDED = "shielded"         # 已加盾处理


@dataclass
class DetectionResult:
    """检测结果类"""
    
    is_injection: bool = False
    confidence: float = 0.0
    matched_patterns: List[str] = field(default_factory=list)
    llm_analysis: Optional[str] = None
    processing_time: float = 0.0
    detection_method: str = "unknown"
    reason: str = ""
    
    def __post_init__(self):
        """结果后处理"""
        self.timestamp = time.time()
