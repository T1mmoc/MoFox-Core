import os
from pymongo import MongoClient
from pymongo.database import Database
from rich.traceback import install
from src.common.logger import get_logger

# SQLAlchemy相关导入
from src.common.database.sqlalchemy_init import initialize_database_compat
from src.common.database.sqlalchemy_models import get_engine, get_db_session

install(extra_lines=3)

_client = None
_db = None
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
    """SQLAlchemy事务上下文管理器"""

    def __init__(self):
        self.session = None

    def __enter__(self):
        self.session = get_db_session()
        return self.session

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.await session.commit()
        else:
            self.session.rollback()
        self.session.close()


# 创建全局数据库代理实例
db = DatabaseProxy()


def __create_database_instance():
    uri = os.getenv("MONGODB_URI")
    host = os.getenv("MONGODB_HOST", "127.0.0.1")
    port = int(os.getenv("MONGODB_PORT", "27017"))
    # db_name 变量在创建连接时不需要，在获取数据库实例时才使用
    username = os.getenv("MONGODB_USERNAME")
    password = os.getenv("MONGODB_PASSWORD")
    auth_source = os.getenv("MONGODB_AUTH_SOURCE")

    if uri:
        # 支持标准mongodb://和mongodb+srv://连接字符串
        if uri.startswith(("mongodb://", "mongodb+srv://")):
            return MongoClient(uri)
        else:
            raise ValueError(
                "Invalid MongoDB URI format. URI must start with 'mongodb://' or 'mongodb+srv://'. "
                "For MongoDB Atlas, use 'mongodb+srv://' format. "
                "See: https://www.mongodb.com/docs/manual/reference/connection-string/"
            )

    if username and password:
        # 如果有用户名和密码，使用认证连接
        return MongoClient(host, port, username=username, password=password, authSource=auth_source)

    # 否则使用无认证连接
    return MongoClient(host, port)


def get_db():
    """获取MongoDB连接实例，延迟初始化。"""
    global _client, _db
    if _client is None:
        _client = __create_database_instance()
        _db = _client[os.getenv("DATABASE_NAME", "MegBot")]
    return _db


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


class DBWrapper:
    """数据库代理类，保持接口兼容性同时实现懒加载。"""

    def __getattr__(self, name):
        return getattr(get_db(), name)

    def __getitem__(self, key):
        return get_db()[key]  # type: ignore


# 全局MongoDB数据库访问点
memory_db: Database = DBWrapper()  # type: ignore
