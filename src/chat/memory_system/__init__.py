# -*- coding: utf-8 -*-
"""
简化记忆系统模块
移除即时记忆和长期记忆分类，实现统一记忆架构和智能遗忘机制
"""

# 核心数据结构
from .memory_chunk import (
    MemoryChunk,
    MemoryMetadata,
    ContentStructure,
    MemoryType,
    ImportanceLevel,
    ConfidenceLevel,
    create_memory_chunk
)

# 遗忘引擎
from .memory_forgetting_engine import (
    MemoryForgettingEngine,
    ForgettingConfig,
    get_memory_forgetting_engine
)

# 统一存储系统
from .unified_memory_storage import (
    UnifiedMemoryStorage,
    UnifiedStorageConfig,
    get_unified_memory_storage,
    initialize_unified_memory_storage
)

# 记忆核心系统
from .memory_system import (
    MemorySystem,
    MemorySystemConfig,
    get_memory_system,
    initialize_memory_system
)

# 记忆管理器
from .memory_manager import (
    MemoryManager,
    MemoryResult,
    memory_manager
)

# 激活器
from .enhanced_memory_activator import (
    MemoryActivator,
    memory_activator,
    enhanced_memory_activator
)

# 格式化器
from .memory_formatter import (
    MemoryFormatter,
    FormatterConfig,
    format_memories_for_llm,
    format_memories_bracket_style
)

# 兼容性别名
from .memory_chunk import MemoryChunk as Memory

__all__ = [
    # 核心数据结构
    "MemoryChunk",
    "Memory",  # 兼容性别名
    "MemoryMetadata",
    "ContentStructure",
    "MemoryType",
    "ImportanceLevel",
    "ConfidenceLevel",
    "create_memory_chunk",

    # 遗忘引擎
    "MemoryForgettingEngine",
    "ForgettingConfig",
    "get_memory_forgetting_engine",

    # 统一存储
    "UnifiedMemoryStorage",
    "UnifiedStorageConfig",
    "get_unified_memory_storage",
    "initialize_unified_memory_storage",

    # 记忆系统
    "MemorySystem",
    "MemorySystemConfig",
    "get_memory_system",
    "initialize_memory_system",

    # 记忆管理器
    "MemoryManager",
    "MemoryResult", 
    "memory_manager",

    # 激活器
    "MemoryActivator",
    "memory_activator",
    "enhanced_memory_activator",  # 兼容性别名

    # 格式化器
    "MemoryFormatter",
    "FormatterConfig",
    "format_memories_for_llm",
    "format_memories_bracket_style",
]

# 版本信息
__version__ = "3.0.0"
__author__ = "MoFox Team"
__description__ = "简化记忆系统 - 统一记忆架构与智能遗忘机制"