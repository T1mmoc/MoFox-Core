import os
from rich.traceback import install
from src.common.logger import get_logger

# SQLAlchemy相关导入
from src.common.database.sqlalchemy_init import initialize_database_compat
from src.common.database.sqlalchemy_models import get_engine, get_db_session

install(extra_lines=3)

_sql_engine = None

logger = get_logger("database")


# 兼容性：为了不破坏现有代码，保留db变量但指向SQLAlchemy
class DatabaseProxy:
    """数据库代理类"""

    def __init__(self):
        self._engine = None
        self._session = None

    @staticmethod
    def initialize(*args, **kwargs):
        """初始化数据库连接"""
        return initialize_database_compat()


class SQLAlchemyTransaction:
    """SQLAlchemy 异步事务上下文管理器 (兼容旧代码示例，推荐直接使用 get_db_session)。"""

    def __init__(self):
        self._ctx = None
        self.session = None

    async def __aenter__(self):
        # get_db_session 是一个 async contextmanager
        self._ctx = get_db_session()
        self.session = await self._ctx.__aenter__()
        return self.session

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        try:
            if self.session:
                if exc_type is None:
                    try:
                        await self.session.commit()
                    except Exception:
                        await self.session.rollback()
                        raise
                else:
                    await self.session.rollback()
        finally:
            if self._ctx:
                await self._ctx.__aexit__(exc_type, exc_val, exc_tb)


# 创建全局数据库代理实例
db = DatabaseProxy()


async def initialize_sql_database(database_config):
    """
    根据配置初始化SQL数据库连接（SQLAlchemy版本）

    Args:
        database_config: DatabaseConfig对象
    """
    global _sql_engine

    try:
        logger.info("使用SQLAlchemy初始化SQL数据库...")

        # 记录数据库配置信息
        if database_config.database_type == "mysql":
            connection_info = f"{database_config.mysql_user}@{database_config.mysql_host}:{database_config.mysql_port}/{database_config.mysql_database}"
            logger.info("MySQL数据库连接配置:")
            logger.info(f"  连接信息: {connection_info}")
            logger.info(f"  字符集: {database_config.mysql_charset}")
        else:
            ROOT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            if not os.path.isabs(database_config.sqlite_path):
                db_path = os.path.join(ROOT_PATH, database_config.sqlite_path)
            else:
                db_path = database_config.sqlite_path
            logger.info("SQLite数据库连接配置:")
            logger.info(f"  数据库文件: {db_path}")

        # 使用SQLAlchemy初始化
        success = initialize_database_compat()
        if success:
            _sql_engine = await get_engine()
            logger.info("SQLAlchemy数据库初始化成功")
        else:
            logger.error("SQLAlchemy数据库初始化失败")

        return _sql_engine

    except Exception as e:
        logger.error(f"初始化SQL数据库失败: {e}")
        return None
