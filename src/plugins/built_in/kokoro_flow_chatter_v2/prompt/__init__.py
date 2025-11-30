"""
Kokoro Flow Chatter V2 - 提示词模块

使用项目统一的 Prompt 管理系统管理所有提示词模板
"""

# 导入 prompts 模块以注册提示词
from . import prompts  # noqa: F401
from .builder import PromptBuilder, get_prompt_builder
from .prompts import PROMPT_NAMES

__all__ = [
    "PromptBuilder",
    "get_prompt_builder",
    "PROMPT_NAMES",
]
