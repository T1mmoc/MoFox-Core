"""数据库方言适配器

提供跨数据库兼容性支持，处理不同数据库之间的差异：
- SQLite: 轻量级本地数据库
- MySQL: 高性能关系型数据库
- PostgreSQL: 功能丰富的开源数据库

主要职责：
1. 提供数据库特定的类型映射
2. 处理方言特定的查询语法
3. 提供数据库特定的优化配置
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from sqlalchemy import String, Text
from sqlalchemy.types import TypeEngine


class DatabaseDialect(Enum):
    """数据库方言枚举"""

    SQLITE = "sqlite"
    MYSQL = "mysql"
    POSTGRESQL = "postgresql"


@dataclass
class DialectConfig:
    """方言配置"""

    dialect: DatabaseDialect
    # 连接验证查询
    ping_query: str
    # 是否支持 RETURNING 子句
    supports_returning: bool
    # 是否支持原生 JSON 类型
    supports_native_json: bool
    # 是否支持数组类型
    supports_arrays: bool
    # 是否需要指定字符串长度用于索引
    requires_length_for_index: bool
    # 默认字符串长度（用于索引列）
    default_string_length: int
    # 事务隔离级别
    isolation_level: str
    # 额外的引擎参数
    engine_kwargs: dict[str, Any] = field(default_factory=dict)


# 预定义的方言配置
DIALECT_CONFIGS: dict[DatabaseDialect, DialectConfig] = {
    DatabaseDialect.SQLITE: DialectConfig(
        dialect=DatabaseDialect.SQLITE,
        ping_query="SELECT 1",
        supports_returning=True,  # SQLite 3.35+ 支持
        supports_native_json=False,
        supports_arrays=False,
        requires_length_for_index=False,
        default_string_length=255,
        isolation_level="SERIALIZABLE",
        engine_kwargs={
            "connect_args": {
                "check_same_thread": False,
                "timeout": 60,
            }
        },
    ),
    DatabaseDialect.MYSQL: DialectConfig(
        dialect=DatabaseDialect.MYSQL,
        ping_query="SELECT 1",
        supports_returning=False,  # MySQL 8.0.21+ 有限支持
        supports_native_json=True,  # MySQL 5.7+
        supports_arrays=False,
        requires_length_for_index=True,  # MySQL 索引需要指定长度
        default_string_length=255,
        isolation_level="READ COMMITTED",
        engine_kwargs={
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        },
    ),
    DatabaseDialect.POSTGRESQL: DialectConfig(
        dialect=DatabaseDialect.POSTGRESQL,
        ping_query="SELECT 1",
        supports_returning=True,
        supports_native_json=True,
        supports_arrays=True,
        requires_length_for_index=False,
        default_string_length=255,
        isolation_level="READ COMMITTED",
        engine_kwargs={
            "pool_pre_ping": True,
            "pool_recycle": 3600,
        },
    ),
}


class DialectAdapter:
    """数据库方言适配器

    根据当前配置的数据库类型，提供相应的类型映射和查询支持
    """

    _current_dialect: DatabaseDialect | None = None
    _config: DialectConfig | None = None

    @classmethod
    def initialize(cls, db_type: str) -> None:
        """初始化适配器

        Args:
            db_type: 数据库类型字符串 ("sqlite", "mysql", "postgresql")
        """
        try:
            cls._current_dialect = DatabaseDialect(db_type.lower())
            cls._config = DIALECT_CONFIGS[cls._current_dialect]
        except ValueError:
            raise ValueError(f"不支持的数据库类型: {db_type}，支持的类型: sqlite, mysql, postgresql")

    @classmethod
    def get_dialect(cls) -> DatabaseDialect:
        """获取当前数据库方言"""
        if cls._current_dialect is None:
            # 延迟初始化：从配置获取
            from src.config.config import global_config

            if global_config is None:
                raise RuntimeError("配置尚未初始化，无法获取数据库方言")
            cls.initialize(global_config.database.database_type)
        return cls._current_dialect  # type: ignore

    @classmethod
    def get_config(cls) -> DialectConfig:
        """获取当前方言配置"""
        if cls._config is None:
            cls.get_dialect()  # 触发初始化
        return cls._config  # type: ignore

    @classmethod
    def get_string_type(cls, max_length: int = 255, indexed: bool = False) -> TypeEngine:
        """获取适合当前数据库的字符串类型

        Args:
            max_length: 最大长度
            indexed: 是否用于索引

        Returns:
            SQLAlchemy 类型
        """
        config = cls.get_config()

        # MySQL 索引列需要指定长度
        if config.requires_length_for_index and indexed:
            return String(max_length)

        # SQLite 和 PostgreSQL 可以使用 Text
        if config.dialect in (DatabaseDialect.SQLITE, DatabaseDialect.POSTGRESQL):
            return Text() if not indexed else String(max_length)

        # MySQL 使用 VARCHAR
        return String(max_length)

    @classmethod
    def get_ping_query(cls) -> str:
        """获取连接验证查询"""
        return cls.get_config().ping_query

    @classmethod
    def supports_returning(cls) -> bool:
        """是否支持 RETURNING 子句"""
        return cls.get_config().supports_returning

    @classmethod
    def supports_native_json(cls) -> bool:
        """是否支持原生 JSON 类型"""
        return cls.get_config().supports_native_json

    @classmethod
    def get_engine_kwargs(cls) -> dict[str, Any]:
        """获取引擎额外参数"""
        return cls.get_config().engine_kwargs.copy()

    @classmethod
    def is_sqlite(cls) -> bool:
        """是否为 SQLite"""
        return cls.get_dialect() == DatabaseDialect.SQLITE

    @classmethod
    def is_mysql(cls) -> bool:
        """是否为 MySQL"""
        return cls.get_dialect() == DatabaseDialect.MYSQL

    @classmethod
    def is_postgresql(cls) -> bool:
        """是否为 PostgreSQL"""
        return cls.get_dialect() == DatabaseDialect.POSTGRESQL


def get_dialect_adapter() -> type[DialectAdapter]:
    """获取方言适配器类"""
    return DialectAdapter


def get_indexed_string_field(max_length: int = 255) -> TypeEngine:
    """获取用于索引的字符串字段类型

    这是一个便捷函数，用于在模型定义中获取适合当前数据库的字符串类型

    Args:
        max_length: 最大长度（对于 MySQL 是必需的）

    Returns:
        SQLAlchemy 类型
    """
    return DialectAdapter.get_string_type(max_length, indexed=True)


def get_text_field() -> TypeEngine:
    """获取文本字段类型

    用于不需要索引的大文本字段

    Returns:
        SQLAlchemy Text 类型
    """
    return Text()
