"""数据库迁移模块

此模块负责数据库结构的自动检查和迁移：
- 自动创建不存在的表
- 自动为现有表添加缺失的列
- 自动为现有表创建缺失的索引

使用新架构的 engine 和 models
"""

from sqlalchemy import inspect
from sqlalchemy.sql import text

from src.common.database.core.engine import get_engine
from src.common.database.core.models import Base
from src.common.logger import get_logger

logger = get_logger("db_migration")


async def check_and_migrate_database(existing_engine=None):
    """异步检查数据库结构并自动迁移

    自动执行以下操作：
    - 创建不存在的表
    - 为现有表添加缺失的列
    - 为现有表创建缺失的索引

    Args:
        existing_engine: 可选的已存在的数据库引擎。如果提供，将使用该引擎；否则获取全局引擎

    Note:
        此函数是幂等的，可以安全地多次调用
    """
    logger.info("正在检查数据库结构并执行自动迁移...")
    engine = existing_engine if existing_engine is not None else await get_engine()

    async with engine.connect() as connection:
        # 在同步上下文中运行inspector操作
        def get_inspector(sync_conn):
            return inspect(sync_conn)

        inspector = await connection.run_sync(get_inspector)

        # 获取数据库中已存在的表名
        db_table_names = await connection.run_sync(
            lambda conn: set(inspector.get_table_names())
        )

        # 1. 首先处理表的创建
        tables_to_create = []
        for table_name, table in Base.metadata.tables.items():
            if table_name not in db_table_names:
                tables_to_create.append(table)

        if tables_to_create:
            logger.info(f"发现 {len(tables_to_create)} 个不存在的表，正在创建...")
            try:
                # 一次性创建所有缺失的表
                await connection.run_sync(
                    lambda sync_conn: Base.metadata.create_all(
                        sync_conn, tables=tables_to_create
                    )
                )
                for table in tables_to_create:
                    logger.info(f"表 '{table.name}' 创建成功。")
                    db_table_names.add(table.name)  # 将新创建的表添加到集合中

                # 提交表创建事务
                await connection.commit()
            except Exception as e:
                logger.error(f"创建表时失败: {e}")
                await connection.rollback()

        # 2. 然后处理现有表的列和索引的添加
        for table_name, table in Base.metadata.tables.items():
            if table_name not in db_table_names:
                logger.warning(
                    f"跳过检查表 '{table_name}'，因为它在创建步骤中可能已失败。"
                )
                continue

            logger.debug(f"正在检查表 '{table_name}' 的列和索引...")

            try:
                # 检查并添加缺失的列
                db_columns_info = await connection.run_sync(
                    lambda conn: {
                        col["name"]: col for col in inspector.get_columns(table_name)
                    }
                )
                db_columns = set(db_columns_info.keys())
                model_columns = {col.name for col in table.c}
                missing_columns = model_columns - db_columns

                if missing_columns:
                    logger.info(
                        f"在表 '{table_name}' 中发现缺失的列: {', '.join(missing_columns)}"
                    )

                    def add_columns_sync(conn):
                        dialect = conn.dialect
                        
                        for column_name in missing_columns:
                            column = table.c[column_name]
                            
                            # 获取列类型的 SQL 表示
                            # 使用 compile 方法获取正确的类型字符串
                            type_compiler = dialect.type_compiler(dialect)
                            column_type_sql = column.type.compile(dialect=dialect)
                            
                            # 构建 ALTER TABLE 语句
                            sql = f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type_sql}"

                            if column.default:
                                # 手动处理不同方言的默认值
                                default_arg = column.default.arg
                                if dialect.name == "sqlite" and isinstance(
                                    default_arg, bool
                                ):
                                    # SQLite 将布尔值存储为 0 或 1
                                    default_value = "1" if default_arg else "0"
                                elif dialect.name == "mysql" and isinstance(default_arg, bool):
                                    # MySQL 也使用 1/0 表示布尔值
                                    default_value = "1" if default_arg else "0"
                                elif isinstance(default_arg, bool):
                                    # PostgreSQL 使用 TRUE/FALSE
                                    default_value = "TRUE" if default_arg else "FALSE"
                                elif isinstance(default_arg, str):
                                    default_value = f"'{default_arg}'"
                                elif default_arg is None:
                                    default_value = "NULL"
                                else:
                                    default_value = str(default_arg)

                                sql += f" DEFAULT {default_value}"

                            if not column.nullable:
                                sql += " NOT NULL"

                            conn.execute(text(sql))
                            logger.info(f"成功向表 '{table_name}' 添加列 '{column_name}'。")

                    await connection.run_sync(add_columns_sync)
                    # 提交列添加事务
                    await connection.commit()
                else:
                    logger.debug(f"表 '{table_name}' 的列结构一致。")

                # 3. 检查并修复列类型不匹配（仅 PostgreSQL）
                await _check_and_fix_column_types(
                    connection, inspector, table_name, table, db_columns_info
                )

                # 检查并创建缺失的索引
                db_indexes = await connection.run_sync(
                    lambda conn: {
                        idx["name"] for idx in inspector.get_indexes(table_name)
                    }
                )
                model_indexes = {idx.name for idx in table.indexes}
                missing_indexes = model_indexes - db_indexes

                if missing_indexes:
                    logger.info(
                        f"在表 '{table_name}' 中发现缺失的索引: {', '.join(missing_indexes)}"
                    )

                    def add_indexes_sync(conn):
                        for index_name in missing_indexes:
                            index_obj = next(
                                (idx for idx in table.indexes if idx.name == index_name),
                                None,
                            )
                            if index_obj is not None:
                                index_obj.create(conn)
                                logger.info(
                                    f"成功为表 '{table_name}' 创建索引 '{index_name}'。"
                                )

                    await connection.run_sync(add_indexes_sync)
                    # 提交索引创建事务
                    await connection.commit()
                else:
                    logger.debug(f"表 '{table_name}' 的索引一致。")

            except Exception as e:
                logger.error(f"在处理表 '{table_name}' 时发生意外错误: {e}")
                await connection.rollback()
                continue

    logger.info("数据库结构检查与自动迁移完成。")


async def create_all_tables(existing_engine=None):
    """创建所有表（不进行迁移检查）

    直接创建所有在 Base.metadata 中定义的表。
    如果表已存在，将被跳过。

    Args:
        existing_engine: 可选的已存在的数据库引擎

    Note:
        生产环境建议使用 check_and_migrate_database()
    """
    logger.info("正在创建所有数据库表...")
    engine = existing_engine if existing_engine is not None else await get_engine()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    logger.info("数据库表创建完成。")


async def drop_all_tables(existing_engine=None):
    """删除所有表（危险操作！）

    删除所有在 Base.metadata 中定义的表。

    Args:
        existing_engine: 可选的已存在的数据库引擎

    Warning:
        此操作将删除所有数据，不可恢复！仅用于测试环境！
    """
    logger.warning("⚠️  正在删除所有数据库表...")
    engine = existing_engine if existing_engine is not None else await get_engine()

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)

    logger.warning("所有数据库表已删除。")


# =============================================================================
# 列类型修复辅助函数
# =============================================================================

# 已知需要修复的列类型映射
# 格式: {(表名, 列名): (期望的Python类型类别, PostgreSQL USING 子句)}
# Python类型类别: "boolean", "integer", "float", "string"
_BOOLEAN_USING_CLAUSE = (
    "boolean",
    "USING CASE WHEN {column} IS NULL THEN FALSE "
    "WHEN {column} = 0 THEN FALSE ELSE TRUE END"
)

_COLUMN_TYPE_FIXES = {
    # messages 表的布尔列
    ("messages", "is_public_notice"): _BOOLEAN_USING_CLAUSE,
    ("messages", "should_reply"): _BOOLEAN_USING_CLAUSE,
    ("messages", "should_act"): _BOOLEAN_USING_CLAUSE,
    ("messages", "is_mentioned"): _BOOLEAN_USING_CLAUSE,
    ("messages", "is_emoji"): _BOOLEAN_USING_CLAUSE,
    ("messages", "is_picid"): _BOOLEAN_USING_CLAUSE,
    ("messages", "is_command"): _BOOLEAN_USING_CLAUSE,
    ("messages", "is_notify"): _BOOLEAN_USING_CLAUSE,
}


def _get_expected_pg_type(python_type_category: str) -> str:
    """获取期望的 PostgreSQL 类型名称"""
    mapping = {
        "boolean": "boolean",
        "integer": "integer",
        "float": "double precision",
        "string": "text",
    }
    return mapping.get(python_type_category, "text")


def _normalize_pg_type(type_name: str) -> str:
    """标准化 PostgreSQL 类型名称用于比较"""
    type_name = type_name.lower().strip()
    # 处理常见的别名
    aliases = {
        "bool": "boolean",
        "int": "integer",
        "int4": "integer",
        "int8": "bigint",
        "float8": "double precision",
        "float4": "real",
        "numeric": "numeric",
        "decimal": "numeric",
    }
    return aliases.get(type_name, type_name)


async def _check_and_fix_column_types(connection, inspector, table_name, table, db_columns_info):
    """检查并修复列类型不匹配的问题（仅 PostgreSQL）
    
    Args:
        connection: 数据库连接
        inspector: SQLAlchemy inspector
        table_name: 表名
        table: SQLAlchemy Table 对象
        db_columns_info: 数据库中列的信息字典
    """
    # 获取数据库方言
    def get_dialect_name(conn):
        return conn.dialect.name
    
    dialect_name = await connection.run_sync(get_dialect_name)
    
    # 目前只处理 PostgreSQL
    if dialect_name != "postgresql":
        return
    
    for (fix_table, fix_column), (expected_type_category, using_clause) in _COLUMN_TYPE_FIXES.items():
        if fix_table != table_name:
            continue
        
        if fix_column not in db_columns_info:
            continue
        
        col_info = db_columns_info[fix_column]
        current_type = _normalize_pg_type(str(col_info.get("type", "")))
        expected_type = _get_expected_pg_type(expected_type_category)
        
        # 如果类型已经正确，跳过
        if current_type == expected_type:
            continue
        
        # 检查是否需要修复：如果当前是 numeric 但期望是 boolean
        if current_type == "numeric" and expected_type == "boolean":
            logger.warning(
                f"发现列类型不匹配: {table_name}.{fix_column} "
                f"(当前: {current_type}, 期望: {expected_type})"
            )
            
            # PostgreSQL 需要先删除默认值，再修改类型，最后重新设置默认值
            using_sql = using_clause.format(column=fix_column)
            drop_default_sql = f"ALTER TABLE {table_name} ALTER COLUMN {fix_column} DROP DEFAULT"
            alter_type_sql = f"ALTER TABLE {table_name} ALTER COLUMN {fix_column} TYPE BOOLEAN {using_sql}"
            set_default_sql = f"ALTER TABLE {table_name} ALTER COLUMN {fix_column} SET DEFAULT FALSE"
            
            try:
                def execute_alter(conn):
                    # 步骤 1: 删除默认值
                    try:
                        conn.execute(text(drop_default_sql))
                    except Exception:
                        pass  # 如果没有默认值，忽略错误
                    # 步骤 2: 修改类型
                    conn.execute(text(alter_type_sql))
                    # 步骤 3: 重新设置默认值
                    conn.execute(text(set_default_sql))
                
                await connection.run_sync(execute_alter)
                await connection.commit()
                logger.info(f"成功修复列类型: {table_name}.{fix_column} -> BOOLEAN")
            except Exception as e:
                logger.error(f"修复列类型失败 {table_name}.{fix_column}: {e}")
                await connection.rollback()

