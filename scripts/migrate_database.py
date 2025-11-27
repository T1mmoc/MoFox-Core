#!/usr/bin/env python3
"""数据库迁移脚本

支持在不同数据库之间迁移数据：
- SQLite <-> MySQL
- SQLite <-> PostgreSQL
- MySQL <-> PostgreSQL

使用方法:
    python scripts/migrate_database.py --help
    python scripts/migrate_database.py --source sqlite --target postgresql
    python scripts/migrate_database.py --source mysql --target postgresql --batch-size 5000

注意事项:
1. 迁移前请备份源数据库
2. 目标数据库应该是空的或不存在的（脚本会自动创建表）
3. 迁移过程可能需要较长时间，请耐心等待

实现细节:
- 使用 SQLAlchemy 进行数据库连接和元数据管理
- 采用流式迁移，避免一次性加载过多数据
- 支持 SQLite、MySQL、PostgreSQL 之间的互相迁移
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from getpass import getpass

# =============================================================================
# 设置日志
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="[%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)

# =============================================================================
# 导入第三方库（延迟导入以便友好报错）
# =============================================================================

try:
    import tomllib
except ImportError:
    tomllib = None

from typing import Any, Iterable, Callable

from sqlalchemy import (
    create_engine,
    MetaData,
    Table,
    inspect,
    text,
)
from sqlalchemy.engine import Engine, Connection
from sqlalchemy.exc import SQLAlchemyError

# ====== 为了在 Windows 上更友好的输出中文，提前设置环境 ======
# 有些 Windows 终端默认编码不是 UTF-8，这里做个兼容
if os.name == "nt":
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass


# =============================================================================
# 配置相关工具
# =============================================================================


def get_project_root() -> str:
    """获取项目根目录（当前脚本的上级目录）"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = get_project_root()


def load_bot_config() -> dict:
    """加载 config/bot_config.toml 配置文件

    返回:
        dict: 配置字典，如果文件不存在或解析失败，则返回空字典
    """
    config_path = os.path.join(PROJECT_ROOT, "config", "bot_config.toml")
    if not os.path.exists(config_path):
        logger.warning("配置文件不存在: %s", config_path)
        return {}

    if tomllib is None:
        logger.warning("当前 Python 版本不支持 tomllib，请使用 Python 3.11+ 或手动安装 tomli")
        return {}

    try:
        with open(config_path, "rb") as f:
            config = tomllib.load(f)
        return config
    except Exception as e:
        logger.error("解析配置文件失败: %s", e)
        return {}


def get_database_config_from_toml(db_type: str) -> dict | None:
    """从 bot_config.toml 中读取数据库配置

    Args:
        db_type: 数据库类型，支持 "sqlite"、"mysql"、"postgresql"

    Returns:
        dict: 数据库配置字典，如果对应配置不存在则返回 None
    """
    config_data = load_bot_config()
    if not config_data:
        return None

    # 兼容旧结构和新结构
    # 旧结构: 顶层直接有 db_type 相关字段
    # 新结构: 在 [database] 下有 db_type 相关字段
    db_config = config_data.get("database", {})

    if db_type == "sqlite":
        sqlite_path = (
            db_config.get("sqlite_path")
            or config_data.get("sqlite_path")
            or "data/MaiBot.db"
        )
        if not os.path.isabs(sqlite_path):
            sqlite_path = os.path.join(PROJECT_ROOT, sqlite_path)
        return {"path": sqlite_path}

    elif db_type == "mysql":
        return {
            "host": db_config.get("mysql_host")
            or config_data.get("mysql_host")
            or "localhost",
            "port": db_config.get("mysql_port")
            or config_data.get("mysql_port")
            or 3306,
            "database": db_config.get("mysql_database")
            or config_data.get("mysql_database")
            or "maibot",
            "user": db_config.get("mysql_user")
            or config_data.get("mysql_user")
            or "root",
            "password": db_config.get("mysql_password")
            or config_data.get("mysql_password")
            or "",
            "charset": db_config.get("mysql_charset")
            or config_data.get("mysql_charset")
            or "utf8mb4",
        }

    elif db_type == "postgresql":
        return {
            "host": db_config.get("postgresql_host")
            or config_data.get("postgresql_host")
            or "localhost",
            "port": db_config.get("postgresql_port")
            or config_data.get("postgresql_port")
            or 5432,
            "database": db_config.get("postgresql_database")
            or config_data.get("postgresql_database")
            or "maibot",
            "user": db_config.get("postgresql_user")
            or config_data.get("postgresql_user")
            or "postgres",
            "password": db_config.get("postgresql_password")
            or config_data.get("postgresql_password")
            or "",
            "schema": db_config.get("postgresql_schema")
            or config_data.get("postgresql_schema")
            or "public",
        }

    return None


# =============================================================================
# 数据库连接相关
# =============================================================================


def create_sqlite_engine(sqlite_path: str) -> Engine:
    """创建 SQLite 引擎"""
    if not os.path.isabs(sqlite_path):
        sqlite_path = os.path.join(PROJECT_ROOT, sqlite_path)

    # 确保目录存在
    os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)

    url = f"sqlite:///{sqlite_path}"
    logger.info("使用 SQLite 数据库: %s", sqlite_path)
    return create_engine(url, future=True)


def create_mysql_engine(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    charset: str = "utf8mb4",
) -> Engine:
    """创建 MySQL 引擎"""
    # 延迟导入 pymysql，以便友好提示
    try:
        import pymysql  # noqa: F401
    except ImportError:
        logger.error("需要安装 pymysql 才能连接 MySQL: pip install pymysql")
        raise

    url = f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}?charset={charset}"
    logger.info("使用 MySQL 数据库: %s@%s:%s/%s", user, host, port, database)
    return create_engine(url, future=True)


def create_postgresql_engine(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
    schema: str = "public",
) -> Engine:
    """创建 PostgreSQL 引擎"""
    # 在导入 psycopg2 之前设置环境变量，解决 Windows 编码问题
    # psycopg2 在 Windows 上连接时，如果客户端编码与服务器不一致可能会有问题
    os.environ.setdefault("PGCLIENTENCODING", "utf-8")

    # 延迟导入 psycopg2，以便友好提示
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        logger.error("需要安装 psycopg2-binary 才能连接 PostgreSQL: pip install psycopg2-binary")
        raise

    url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
    logger.info("使用 PostgreSQL 数据库: %s@%s:%s/%s (schema=%s)", user, host, port, database, schema)
    engine = create_engine(url, future=True)
    # 为了方便，设置 search_path
    with engine.connect() as conn:
        conn.execute(text(f"SET search_path TO {schema}"))
    return engine


def create_engine_by_type(db_type: str, config: dict) -> Engine:
    """根据数据库类型创建对应的 SQLAlchemy Engine

    Args:
        db_type: 数据库类型，支持 sqlite/mysql/postgresql
        config: 配置字典

    Returns:
        Engine: SQLAlchemy 引擎实例
    """
    db_type = db_type.lower()
    if db_type == "sqlite":
        return create_sqlite_engine(config["path"])
    elif db_type == "mysql":
        return create_mysql_engine(
            host=config["host"],
            port=config["port"],
            database=config["database"],
            user=config["user"],
            password=config["password"],
            charset=config.get("charset", "utf8mb4"),
        )
    elif db_type == "postgresql":
        return create_postgresql_engine(
            host=config["host"],
            port=config["port"],
            database=config["database"],
            user=config["user"],
            password=config["password"],
            schema=config.get("schema", "public"),
        )
    else:
        raise ValueError(f"不支持的数据库类型: {db_type}")


# =============================================================================
# 工具函数
# =============================================================================


def chunked_iterable(iterable: Iterable, size: int) -> Iterable[list]:
    """将可迭代对象分块

    Args:
        iterable: 可迭代对象
        size: 每块大小

    Yields:
        list: 分块列表
    """
    chunk: list[Any] = []
    for item in iterable:
        chunk.append(item)
        if len(chunk) >= size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def get_table_row_count(conn: Connection, table: Table) -> int:
    """获取表的行数"""
    try:
        result = conn.execute(text(f"SELECT COUNT(*) FROM {table.name}"))
        return int(result.scalar() or 0)
    except SQLAlchemyError as e:
        logger.warning("获取表行数失败 %s: %s", table.name, e)
        return 0


def copy_table_structure(source_table: Table, target_metadata: MetaData, target_engine: Engine) -> Table:
    """在目标数据库中创建与源表结构相同的表

    Args:
        source_table: 源表对象
        target_metadata: 目标元数据对象
        target_engine: 目标数据库引擎

    Returns:
        Table: 目标表对象
    """
    # 复制表结构
    target_table = Table(
        source_table.name,
        target_metadata,
        *[c.copy() for c in source_table.columns],
        *[c.copy() for c in source_table.constraints],
    )
    target_metadata.create_all(target_engine, tables=[target_table])
    return target_table


def migrate_table_data(
    source_conn: Connection,
    target_conn: Connection,
    source_table: Table,
    target_table: Table,
    batch_size: int = 1000,
) -> tuple[int, int]:
    """迁移单个表的数据

    Args:
        source_conn: 源数据库连接
        target_conn: 目标数据库连接
        source_table: 源表对象
        target_table: 目标表对象
        batch_size: 每批次处理大小

    Returns:
        tuple[int, int]: (迁移行数, 错误数量)
    """
    total_rows = get_table_row_count(source_conn, source_table)
    logger.info(
        "开始迁移表: %s (共 %s 行)",
        source_table.name,
        total_rows if total_rows else "未知",
    )

    migrated_rows = 0
    error_count = 0

    # 使用流式查询，避免一次性加载太多数据
    # 对于 SQLAlchemy 1.4/2.0 可以使用 yield_per
    try:
        select_stmt = source_table.select()
        result = source_conn.execute(select_stmt)
    except SQLAlchemyError as e:
        logger.error("查询表 %s 失败: %s", source_table.name, e)
        return 0, 1

    columns = source_table.columns.keys()

    def insert_batch(rows: list[dict]):
        nonlocal migrated_rows, error_count
        if not rows:
            return
        try:
            target_conn.execute(target_table.insert(), rows)
            migrated_rows += len(rows)
            logger.info("  已迁移 %d/%s 行", migrated_rows, total_rows or "?")
        except SQLAlchemyError as e:
            logger.error("写入表 %s 失败: %s", target_table.name, e)
            error_count += len(rows)

    batch: list[dict] = []
    for row in result:
        row_dict = {col: row[col] for col in columns}
        batch.append(row_dict)
        if len(batch) >= batch_size:
            insert_batch(batch)
            batch = []

    if batch:
        insert_batch(batch)

    logger.info(
        "完成迁移表: %s (成功: %d 行, 失败: %d 行)",
        source_table.name,
        migrated_rows,
        error_count,
    )

    return migrated_rows, error_count


def confirm_action(prompt: str, default: bool = False) -> bool:
    """确认操作

    Args:
        prompt: 提示信息
        default: 默认值

    Returns:
        bool: 用户是否确认
    """
    while True:
        if default:
            choice = input(f"{prompt} [Y/n]: ").strip().lower()
            if choice == "":
                return True
        else:
            choice = input(f"{prompt} [y/N]: ").strip().lower()
            if choice == "":
                return False

        if choice in ("y", "yes"):
            return True
        elif choice in ("n", "no"):
            return False
        else:
            print("请输入 y 或 n")


# =============================================================================
# 迁移器实现
# =============================================================================


class DatabaseMigrator:
    """通用数据库迁移器"""

    def __init__(
        self,
        source_type: str,
        target_type: str,
        batch_size: int = 1000,
        source_config: dict | None = None,
        target_config: dict | None = None,
    ):
        """初始化迁移器

        Args:
            source_type: 源数据库类型
            target_type: 目标数据库类型
            batch_size: 批量处理大小
            source_config: 源数据库配置（可选，默认从配置文件读取）
            target_config: 目标数据库配置（可选，需要手动指定）
        """
        self.source_type = source_type.lower()
        self.target_type = target_type.lower()
        self.batch_size = batch_size
        self.source_config = source_config
        self.target_config = target_config

        self._validate_database_types()

        self.source_engine: Any = None
        self.target_engine: Any = None
        self.metadata = MetaData()

        # 统计信息
        self.stats = {
            "tables_migrated": 0,
            "rows_migrated": 0,
            "errors": [],
            "start_time": None,
            "end_time": None,
        }

    def _validate_database_types(self):
        """验证数据库类型"""
        supported_types = {"sqlite", "mysql", "postgresql"}
        if self.source_type not in supported_types:
            raise ValueError(f"不支持的源数据库类型: {self.source_type}")
        if self.target_type not in supported_types:
            raise ValueError(f"不支持的目标数据库类型: {self.target_type}")

    def _load_source_config(self) -> dict:
        """加载源数据库配置

        如果初始化时提供了 source_config，则直接使用；
        否则从 bot_config.toml 中读取。
        """
        if self.source_config:
            logger.info("使用传入的源数据库配置")
            return self.source_config

        logger.info("未提供源数据库配置，尝试从 bot_config.toml 读取")
        config = get_database_config_from_toml(self.source_type)
        if not config:
            raise ValueError("无法从配置文件中读取源数据库配置，请检查 config/bot_config.toml")

        logger.info("成功从配置文件读取源数据库配置")
        return config

    def _load_target_config(self) -> dict:
        """加载目标数据库配置

        目标数据库配置必须通过初始化参数提供，或者通过命令行参数构建。
        """
        if not self.target_config:
            raise ValueError("未提供目标数据库配置，请通过命令行参数指定或在交互模式中输入")
        logger.info("使用传入的目标数据库配置")
        return self.target_config

    def _connect_databases(self):
        """连接源数据库和目标数据库"""
        # 源数据库配置
        source_config = self._load_source_config()
        # 目标数据库配置
        target_config = self._load_target_config()

        # 创建引擎
        self.source_engine = create_engine_by_type(self.source_type, source_config)
        self.target_engine = create_engine_by_type(self.target_type, target_config)

        # 反射源数据库元数据
        logger.info("正在反射源数据库元数据...")
        self.metadata.reflect(bind=self.source_engine)
        logger.info("发现 %d 张表: %s", len(self.metadata.tables), ", ".join(self.metadata.tables.keys()))

    def _get_tables_in_dependency_order(self) -> list[Table]:
        """获取按依赖顺序排序的表列表

        为了避免外键约束问题，创建表时需要按照依赖顺序，
        例如先创建被引用的表，再创建引用它们的表。
        """
        inspector = inspect(self.source_engine)

        # 构建依赖图：table -> set(dependent_tables)
        dependencies: dict[str, set[str]] = {}
        for table_name in self.metadata.tables:
            dependencies[table_name] = set()

        for table_name, table in self.metadata.tables.items():
            fks = inspector.get_foreign_keys(table_name)
            for fk in fks:
                # 被引用的表
                referred_table = fk["referred_table"]
                if referred_table in dependencies:
                    dependencies[table_name].add(referred_table)

        # 拓扑排序
        sorted_tables: list[Table] = []
        visited: set[str] = set()
        temp_mark: set[str] = set()

        def visit(table_name: str):
            if table_name in visited:
                return
            if table_name in temp_mark:
                logger.warning("检测到循环依赖，表: %s", table_name)
                return
            temp_mark.add(table_name)
            for dep in dependencies[table_name]:
                visit(dep)
            temp_mark.remove(table_name)
            visited.add(table_name)
            sorted_tables.append(self.metadata.tables[table_name])

        for table_name in dependencies:
            if table_name not in visited:
                visit(table_name)

        return sorted_tables

    def _drop_target_tables(self, conn: Connection):
        """删除目标数据库中已经存在的表（谨慎操作）

        这里为了避免冲突，迁移前会询问用户是否删除目标库中已经存在的同名表。
        """
        inspector = inspect(conn)
        existing_tables = inspector.get_table_names()

        if not existing_tables:
            logger.info("目标数据库中没有已存在的表，无需删除")
            return

        logger.info("目标数据库中当前存在的表: %s", ", ".join(existing_tables))
        if confirm_action("是否删除目标数据库中已有的所有表？此操作不可恢复！", default=False):
            with conn.begin():
                for table_name in existing_tables:
                    try:
                        logger.info("删除目标数据库中表: %s", table_name)
                        conn.execute(text(f"DROP TABLE IF EXISTS {table_name} CASCADE"))
                    except SQLAlchemyError as e:
                        logger.error("删除表 %s 失败: %s", table_name, e)
                        self.stats["errors"].append(
                            f"删除表 {table_name} 失败: {e}"
                        )
        else:
            logger.info("用户选择保留目标数据库中已有的表，可能会与迁移数据发生冲突。")

    def migrate(self):
        """执行迁移操作"""
        import time

        self.stats["start_time"] = time.time()

        # 连接数据库
        self._connect_databases()

        # 获取表的依赖顺序
        tables = self._get_tables_in_dependency_order()
        logger.info("按依赖顺序迁移表: %s", ", ".join(t.name for t in tables))

        # 删除目标库中已有表（可选）
        with self.target_engine.connect() as target_conn:
            self._drop_target_tables(target_conn)

        # 开始迁移
        with self.source_engine.connect() as source_conn, self.target_engine.connect() as target_conn:
            for source_table in tables:
                try:
                    # 在目标库中创建表结构
                    target_table = copy_table_structure(source_table, MetaData(), self.target_engine)

                    # 迁移数据
                    migrated_rows, error_count = migrate_table_data(
                        source_conn,
                        target_conn,
                        source_table,
                        target_table,
                        batch_size=self.batch_size,
                    )

                    self.stats["tables_migrated"] += 1
                    self.stats["rows_migrated"] += migrated_rows
                    if error_count > 0:
                        self.stats["errors"].append(
                            f"表 {source_table.name} 迁移失败 {error_count} 行"
                        )

                except Exception as e:
                    logger.error("迁移表 %s 时发生错误: %s", source_table.name, e)
                    self.stats["errors"].append(f"表 {source_table.name} 迁移失败: {e}")

        self.stats["end_time"] = time.time()

    def print_summary(self):
        """打印迁移总结"""
        import time

        duration = None
        if self.stats["start_time"] is not None and self.stats["end_time"] is not None:
            duration = self.stats["end_time"] - self.stats["start_time"]

        print("\n" + "=" * 60)
        print("迁移完成！")
        print(f"  迁移表数量: {self.stats['tables_migrated']}")
        print(f"  迁移行数量: {self.stats['rows_migrated']}")
        if duration is not None:
            print(f"  总耗时: {duration:.2f} 秒")
        if self.stats["errors"]:
            print("  ⚠️ 发生错误:")
            for err in self.stats["errors"]:
                print(f"    - {err}")
        else:
            print("  没有发生错误 🎉")
        print("=" * 60 + "\n")

    def run(self):
        """运行迁移并打印总结"""
        self.migrate()
        self.print_summary()
        return self.stats


# =============================================================================
# 命令行参数解析
# =============================================================================


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="数据库迁移工具 - 在 SQLite、MySQL、PostgreSQL 之间迁移数据",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例:
  # 从 SQLite 迁移到 PostgreSQL
  python scripts/migrate_database.py \
    --source sqlite \
    --target postgresql \
    --target-host localhost \
    --target-port 5432 \
    --target-database maibot \
    --target-user postgres \
    --target-password your_password

  # 从 SQLite 迁移到 MySQL
  python scripts/migrate_database.py \
    --source sqlite \
    --target mysql \
    --target-host localhost \
    --target-port 3306 \
    --target-database maibot \
    --target-user root \
    --target-password your_password

  # 使用交互式向导模式（推荐）
  python scripts/migrate_database.py
  python scripts/migrate_database.py --interactive
        """,
    )

    # 基本参数
    parser.add_argument(
        "--source",
        type=str,
        choices=["sqlite", "mysql", "postgresql"],
        help="源数据库类型（不指定时，在交互模式中选择）",
    )
    parser.add_argument(
        "--target",
        type=str,
        choices=["sqlite", "mysql", "postgresql"],
        help="目标数据库类型（不指定时，在交互模式中选择）",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="批量处理大小（默认: 1000）",
    )

    parser.add_argument(
        "--interactive",
        action="store_true",
        help="启用交互式向导模式（推荐：直接运行脚本或加上此参数）",
    )

    # 源数据库参数（可选，默认从 bot_config.toml 读取）
    source_group = parser.add_argument_group("源数据库配置（可选，默认从 bot_config.toml 读取）")
    source_group.add_argument("--source-path", type=str, help="SQLite 数据库路径")
    source_group.add_argument("--source-host", type=str, help="MySQL/PostgreSQL 主机")
    source_group.add_argument("--source-port", type=int, help="MySQL/PostgreSQL 端口")
    source_group.add_argument("--source-database", type=str, help="数据库名")
    source_group.add_argument("--source-user", type=str, help="用户名")
    source_group.add_argument("--source-password", type=str, help="密码")

    # 目标数据库参数
    target_group = parser.add_argument_group("目标数据库配置")
    target_group.add_argument("--target-path", type=str, help="SQLite 数据库路径")
    target_group.add_argument("--target-host", type=str, help="MySQL/PostgreSQL 主机")
    target_group.add_argument("--target-port", type=int, help="MySQL/PostgreSQL 端口")
    target_group.add_argument("--target-database", type=str, help="数据库名")
    target_group.add_argument("--target-user", type=str, help="用户名")
    target_group.add_argument("--target-password", type=str, help="密码")
    target_group.add_argument("--target-schema", type=str, default="public", help="PostgreSQL schema")
    target_group.add_argument("--target-charset", type=str, default="utf8mb4", help="MySQL 字符集")

    return parser.parse_args()


def build_config_from_args(args, prefix: str, db_type: str) -> dict | None:
    """从命令行参数构建配置

    Args:
        args: 命令行参数
        prefix: 参数前缀 ("source" 或 "target")
        db_type: 数据库类型

    Returns:
        配置字典或 None
    """
    if db_type == "sqlite":
        path = getattr(args, f"{prefix}_path", None)
        if path:
            return {"path": path}
        return None

    elif db_type in ("mysql", "postgresql"):
        host = getattr(args, f"{prefix}_host", None)
        if not host:
            return None

        config = {
            "host": host,
            "port": getattr(args, f"{prefix}_port") or (3306 if db_type == "mysql" else 5432),
            "database": getattr(args, f"{prefix}_database") or "maibot",
            "user": getattr(args, f"{prefix}_user") or ("root" if db_type == "mysql" else "postgres"),
            "password": getattr(args, f"{prefix}_password") or "",
        }

        if db_type == "mysql":
            config["charset"] = getattr(args, f"{prefix}_charset", "utf8mb4")
        elif db_type == "postgresql":
            config["schema"] = getattr(args, f"{prefix}_schema", "public")

        return config

    return None


def _ask_choice(prompt: str, options: list[str], default_index: int | None = None) -> str:
    """在控制台中让用户从多个选项中选择一个"""
    while True:
        print()
        print(prompt)
        for i, opt in enumerate(options, start=1):
            default_mark = ""
            if default_index is not None and i - 1 == default_index:
                default_mark = "  (默认)"
            print(f"  {i}) {opt}{default_mark}")
        ans = input("请输入选项编号: ").strip()
        if not ans and default_index is not None:
            return options[default_index]
        if ans.isdigit():
            idx = int(ans)
            if 1 <= idx <= len(options):
                return options[idx - 1]
        print("❌ 无效的选择，请重新输入。")


def _ask_int(prompt: str, default: int | None = None) -> int:
    """在控制台中输入正整数"""
    while True:
        suffix = f" (默认 {default})" if default is not None else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            return default
        try:
            value = int(raw)
            if value <= 0:
                raise ValueError()
            return value
        except ValueError:
            print("❌ 请输入一个大于 0 的整数。")


def _ask_str(
    prompt: str,
    default: str | None = None,
    allow_empty: bool = False,
    is_password: bool = False,
) -> str:
    """在控制台中输入字符串，可选默认值/密码输入"""
    while True:
        suffix = f" (默认: {default})" if default is not None else ""
        full_prompt = f"{prompt}{suffix}: "
        raw = getpass(full_prompt) if is_password else input(full_prompt)
        raw = raw.strip()
        if not raw:
            if default is not None:
                return default
            if allow_empty:
                return ""
            print("❌ 输入不能为空，请重新输入。")
            continue
        return raw


def interactive_setup() -> dict:
    """交互式向导，返回用于初始化 DatabaseMigrator 的参数字典"""
    print("=" * 60)
    print("🌟 数据库迁移向导")
    print("只需回答几个问题，我会帮你构造迁移配置。")
    print("=" * 60)

    db_types = ["sqlite", "mysql", "postgresql"]

    # 选择源数据库
    source_type = _ask_choice("请选择【源数据库类型】:", db_types, default_index=0)

    # 选择目标数据库（不能与源相同）
    while True:
        default_idx = 2 if len(db_types) >= 3 else 0
        target_type = _ask_choice("请选择【目标数据库类型】:", db_types, default_index=default_idx)
        if target_type != source_type:
            break
        print("❌ 目标数据库不能和源数据库相同，请重新选择。")

    # 批量大小
    batch_size = _ask_int("请输入批量大小 batch-size", default=1000)

    # 源数据库配置：默认使用 bot_config.toml
    print()
    print("源数据库配置：")
    print("  默认会从 config/bot_config.toml 中读取对应配置。")
    use_default_source = input("是否使用配置文件中的【源数据库】配置? [Y/n]: ").strip().lower()
    if use_default_source in ("", "y", "yes"):
        source_config = None  # 让 DatabaseMigrator 自己去读配置
    else:
        # 简单交互式配置源数据库
        print("请手动输入源数据库连接信息：")
        if source_type == "sqlite":
            source_path = _ask_str("源 SQLite 文件路径", default="data/MaiBot.db")
            source_config = {"path": source_path}
        else:
            port_default = 3306 if source_type == "mysql" else 5432
            user_default = "root" if source_type == "mysql" else "postgres"
            host = _ask_str("源数据库 host", default="localhost")
            port = _ask_int("源数据库 port", default=port_default)
            database = _ask_str("源数据库名", default="maibot")
            user = _ask_str("源数据库用户名", default=user_default)
            password = _ask_str("源数据库密码（输入时不回显）", default="", is_password=True)
            source_config = {
                "host": host,
                "port": port,
                "database": database,
                "user": user,
                "password": password,
            }
            if source_type == "mysql":
                source_config["charset"] = _ask_str("源数据库字符集", default="utf8mb4")
            elif source_type == "postgresql":
                source_config["schema"] = _ask_str("源数据库 schema", default="public")

    # 目标数据库配置（必须显式确认）
    print()
    print("目标数据库配置：")
    if target_type == "sqlite":
        target_path = _ask_str(
            "目标 SQLite 文件路径（若不存在会自动创建）",
            default="data/MaiBot_target.db",
        )
        target_config = {"path": target_path}
    else:
        port_default = 3306 if target_type == "mysql" else 5432
        user_default = "root" if target_type == "mysql" else "postgres"
        host = _ask_str("目标数据库 host", default="localhost")
        port = _ask_int("目标数据库 port", default=port_default)
        database = _ask_str("目标数据库名", default="maibot")
        user = _ask_str("目标数据库用户名", default=user_default)
        password = _ask_str("目标数据库密码（输入时不回显）", default="", is_password=True)

        target_config = {
            "host": host,
            "port": port,
            "database": database,
            "user": user,
            "password": password,
        }
        if target_type == "mysql":
            target_config["charset"] = _ask_str("目标数据库字符集", default="utf8mb4")
        elif target_type == "postgresql":
            target_config["schema"] = _ask_str("目标数据库 schema", default="public")

    print()
    print("=" * 60)
    print("迁移配置确认：")
    print(f"  源数据库类型: {source_type}")
    print(f"  目标数据库类型: {target_type}")
    print(f"  批量大小: {batch_size}")
    print("⚠️ 请确认目标数据库为空或可以被覆盖，并且已备份源数据库。")
    confirm = input("是否开始迁移？[Y/n]: ").strip().lower()
    if confirm not in ("", "y", "yes"):
        print("已取消迁移。")
        sys.exit(0)

    return {
        "source_type": source_type,
        "target_type": target_type,
        "batch_size": batch_size,
        "source_config": source_config,
        "target_config": target_config,
    }


def main():
    """主函数"""
    args = parse_args()

    # 如果没有任何参数，或者显式指定 --interactive，则进入交互模式
    if args.interactive or len(sys.argv) == 1:
        params = interactive_setup()
        try:
            migrator = DatabaseMigrator(**params)
            stats = migrator.run()
            if stats["errors"]:
                sys.exit(1)
            return
        except KeyboardInterrupt:
            print("\n迁移被用户中断")
            sys.exit(130)
        except Exception as e:
            print(f"迁移失败: {e}")
            sys.exit(1)

    # 非交互模式：保持原有行为，但如果没给 source/target，就提示错误
    if not args.source or not args.target:
        print("错误: 非交互模式下必须指定 --source 和 --target。")
        print("你也可以直接运行脚本或添加 --interactive 使用交互式向导。")
        sys.exit(2)

    # 构建配置
    source_config = build_config_from_args(args, "source", args.source)
    target_config = build_config_from_args(args, "target", args.target)

    # 验证目标配置
    if target_config is None:
        if args.target == "sqlite":
            if not args.target_path:
                print("错误: 目标数据库为 SQLite 时，必须指定 --target-path（或使用交互模式）")
                sys.exit(1)
            target_config = {"path": args.target_path}
        else:
            if not args.target_host:
                print(f"错误: 目标数据库为 {args.target} 时，必须指定 --target-host（或使用交互模式）")
                sys.exit(1)

    try:
        migrator = DatabaseMigrator(
            source_type=args.source,
            target_type=args.target,
            batch_size=args.batch_size,
            source_config=source_config,
            target_config=target_config,
        )

        stats = migrator.run()

        # 如果有错误，返回非零退出码
        if stats["errors"]:
            sys.exit(1)

    except KeyboardInterrupt:
        print("\n迁移被用户中断")
        sys.exit(130)
    except Exception as e:
        print(f"迁移失败: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
