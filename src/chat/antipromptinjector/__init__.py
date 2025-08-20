# -*- coding: utf-8 -*-
"""
MaiBot 反注入系统模块

本模块提供了一个完整的LLM反注入检测和防护系统，用于防止恶意的提示词注入攻击。

主要功能：
1. 基于规则的快速检测
2. 黑白名单机制
3. LLM二次分析
4. 消息处理模式（严格模式/宽松模式/反击模式）

作者: FOX YaNuo
"""

from .anti_injector import AntiPromptInjector, get_anti_injector, initialize_anti_injector
from .types import DetectionResult, ProcessResult
from .core import PromptInjectionDetector, MessageShield
from .processors import (
    initialize_skip_list, 
    should_skip_injection_detection, 
    MessageProcessor
)
from .management import AntiInjectionStatistics, UserBanManager
from .decision import CounterAttackGenerator, ProcessingDecisionMaker
    
__all__ = [
    "AntiPromptInjector",
    "get_anti_injector", 
    "initialize_anti_injector",
    "DetectionResult",
    "ProcessResult",
    "PromptInjectionDetector",
    "MessageShield",
    "MessageProcessor",
    "AntiInjectionStatistics",
    "UserBanManager", 
    "CounterAttackGenerator",
    "ProcessingDecisionMaker",
    "initialize_skip_list",
    "should_skip_injection_detection"
]


__author__ = "FOX YaNuo"
