"""SQLAlchemy数据库模型定义

替换Peewee ORM，使用SQLAlchemy提供更好的连接池管理和错误恢复能力
"""

from sqlalchemy import Column, String, Float, Integer, Boolean, Text, Index, create_engine, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session, Mapped, mapped_column
from sqlalchemy.pool import QueuePool
import os
import datetime
import time
from typing import Iterator, Optional, Any, Dict
from src.common.logger import get_logger
from contextlib import contextmanager

logger = get_logger("sqlalchemy_models")

# 创建基类
Base = declarative_base()

# MySQL兼容的字段类型辅助函数
def get_string_field(max_length=255, **kwargs):
    """
    根据数据库类型返回合适的字符串字段
    MySQL需要指定长度的VARCHAR用于索引，SQLite可以使用Text
    """
    from src.config.config import global_config
    if global_config.database.database_type == "mysql":
        return String(max_length, **kwargs)
    else:
        return Text(**kwargs)


class ChatStreams(Base):
    """聊天流模型"""
    __tablename__ = 'chat_streams'

    id = Column(Integer, primary_key=True, autoincrement=True)
    stream_id = Column(get_string_field(64), nullable=False, unique=True, index=True)
    create_time = Column(Float, nullable=False)
    group_platform = Column(Text, nullable=True)
    group_id = Column(get_string_field(100), nullable=True, index=True)
    group_name = Column(Text, nullable=True)
    last_active_time = Column(Float, nullable=False)
    platform = Column(Text, nullable=False)
    user_platform = Column(Text, nullable=False)
    user_id = Column(get_string_field(100), nullable=False, index=True)
    user_nickname = Column(Text, nullable=False)
    user_cardname = Column(Text, nullable=True)

    __table_args__ = (
        Index('idx_chatstreams_stream_id', 'stream_id'),
        Index('idx_chatstreams_user_id', 'user_id'),
        Index('idx_chatstreams_group_id', 'group_id'),
    )


class LLMUsage(Base):
    """LLM使用记录模型"""
    __tablename__ = 'llm_usage'

    id = Column(Integer, primary_key=True, autoincrement=True)
    model_name = Column(get_string_field(100), nullable=False, index=True)
    model_assign_name = Column(get_string_field(100), index=True)  # 添加索引
    model_api_provider = Column(get_string_field(100), index=True)  # 添加索引
    user_id = Column(get_string_field(50), nullable=False, index=True)
    request_type = Column(get_string_field(50), nullable=False, index=True)
    endpoint = Column(Text, nullable=False)
    prompt_tokens = Column(Integer, nullable=False)
    completion_tokens = Column(Integer, nullable=False)
    time_cost = Column(Float, nullable=True)
    total_tokens = Column(Integer, nullable=False)
    cost = Column(Float, nullable=False)
    status = Column(Text, nullable=False)
    timestamp = Column(DateTime, nullable=False, index=True, default=datetime.datetime.now)

    __table_args__ = (
        Index('idx_llmusage_model_name', 'model_name'),
        Index('idx_llmusage_model_assign_name', 'model_assign_name'),
        Index('idx_llmusage_model_api_provider', 'model_api_provider'),
        Index('idx_llmusage_time_cost', 'time_cost'),
        Index('idx_llmusage_user_id', 'user_id'),
        Index('idx_llmusage_request_type', 'request_type'),
        Index('idx_llmusage_timestamp', 'timestamp'),
    )


class Emoji(Base):
    """表情包模型"""
    __tablename__ = 'emoji'

    id = Column(Integer, primary_key=True, autoincrement=True)
    full_path = Column(get_string_field(500), nullable=False, unique=True, index=True)
    format = Column(Text, nullable=False)
    emoji_hash = Column(get_string_field(64), nullable=False, index=True)
    description = Column(Text, nullable=False)
    query_count = Column(Integer, nullable=False, default=0)
    is_registered = Column(Boolean, nullable=False, default=False)
    is_banned = Column(Boolean, nullable=False, default=False)
    emotion = Column(Text, nullable=True)
    record_time = Column(Float, nullable=False)
    register_time = Column(Float, nullable=True)
    usage_count = Column(Integer, nullable=False, default=0)
    last_used_time = Column(Float, nullable=True)

    __table_args__ = (
        Index('idx_emoji_full_path', 'full_path'),
        Index('idx_emoji_hash', 'emoji_hash'),
    )


class Messages(Base):
    """消息模型"""
    __tablename__ = 'messages'

    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(get_string_field(100), nullable=False, index=True)
    time = Column(Float, nullable=False)
    chat_id = Column(get_string_field(64), nullable=False, index=True)
    reply_to = Column(Text, nullable=True)
    interest_value = Column(Float, nullable=True)
    is_mentioned = Column(Boolean, nullable=True)

    # 从 chat_info 扁平化而来的字段
    chat_info_stream_id = Column(Text, nullable=False)
    chat_info_platform = Column(Text, nullable=False)
    chat_info_user_platform = Column(Text, nullable=False)
    chat_info_user_id = Column(Text, nullable=False)
    chat_info_user_nickname = Column(Text, nullable=False)
    chat_info_user_cardname = Column(Text, nullable=True)
    chat_info_group_platform = Column(Text, nullable=True)
    chat_info_group_id = Column(Text, nullable=True)
    chat_info_group_name = Column(Text, nullable=True)
    chat_info_create_time = Column(Float, nullable=False)
    chat_info_last_active_time = Column(Float, nullable=False)

    # 从顶层 user_info 扁平化而来的字段
    user_platform = Column(Text, nullable=True)
    user_id = Column(get_string_field(100), nullable=True, index=True)
    user_nickname = Column(Text, nullable=True)
    user_cardname = Column(Text, nullable=True)

    processed_plain_text = Column(Text, nullable=True)
    display_message = Column(Text, nullable=True)
    memorized_times = Column(Integer, nullable=False, default=0)
    priority_mode = Column(Text, nullable=True)
    priority_info = Column(Text, nullable=True)
    additional_config = Column(Text, nullable=True)
    is_emoji = Column(Boolean, nullable=False, default=False)
    is_picid = Column(Boolean, nullable=False, default=False)
    is_command = Column(Boolean, nullable=False, default=False)
    is_notify = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index('idx_messages_message_id', 'message_id'),
        Index('idx_messages_chat_id', 'chat_id'),
        Index('idx_messages_time', 'time'),
        Index('idx_messages_user_id', 'user_id'),
    )


class ActionRecords(Base):
    """动作记录模型"""
    __tablename__ = 'action_records'

    id = Column(Integer, primary_key=True, autoincrement=True)
    action_id = Column(get_string_field(100), nullable=False, index=True)
    time = Column(Float, nullable=False)
    action_name = Column(Text, nullable=False)
    action_data = Column(Text, nullable=False)
    action_done = Column(Boolean, nullable=False, default=False)
    action_build_into_prompt = Column(Boolean, nullable=False, default=False)
    action_prompt_display = Column(Text, nullable=False)
    chat_id = Column(get_string_field(64), nullable=False, index=True)
    chat_info_stream_id = Column(Text, nullable=False)
    chat_info_platform = Column(Text, nullable=False)

    __table_args__ = (
        Index('idx_actionrecords_action_id', 'action_id'),
        Index('idx_actionrecords_chat_id', 'chat_id'),
        Index('idx_actionrecords_time', 'time'),
    )


class Images(Base):
    """图像信息模型"""
    __tablename__ = 'images'

    id = Column(Integer, primary_key=True, autoincrement=True)
    image_id = Column(Text, nullable=False, default="")
    emoji_hash = Column(get_string_field(64), nullable=False, index=True)
    description = Column(Text, nullable=True)
    path = Column(get_string_field(500), nullable=False, unique=True)
    count = Column(Integer, nullable=False, default=1)
    timestamp = Column(Float, nullable=False)
    type = Column(Text, nullable=False)
    vlm_processed = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index('idx_images_emoji_hash', 'emoji_hash'),
        Index('idx_images_path', 'path'),
    )


class ImageDescriptions(Base):
    """图像描述信息模型"""
    __tablename__ = 'image_descriptions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(Text, nullable=False)
    image_description_hash = Column(get_string_field(64), nullable=False, index=True)
    description = Column(Text, nullable=False)
    timestamp = Column(Float, nullable=False)

    __table_args__ = (
        Index('idx_imagedesc_hash', 'image_description_hash'),
    )


class Videos(Base):
    """视频信息模型"""
    __tablename__ = 'videos'

    id = Column(Integer, primary_key=True, autoincrement=True)
    video_id = Column(Text, nullable=False, default="")
    video_hash = Column(get_string_field(64), nullable=False, index=True, unique=True)
    description = Column(Text, nullable=True)
    count = Column(Integer, nullable=False, default=1)
    timestamp = Column(Float, nullable=False)
    vlm_processed = Column(Boolean, nullable=False, default=False)
    
    # 视频特有属性
    duration = Column(Float, nullable=True)  # 视频时长（秒）
    frame_count = Column(Integer, nullable=True)  # 总帧数
    fps = Column(Float, nullable=True)  # 帧率
    resolution = Column(Text, nullable=True)  # 分辨率
    file_size = Column(Integer, nullable=True)  # 文件大小（字节）

    __table_args__ = (
        Index('idx_videos_video_hash', 'video_hash'),
        Index('idx_videos_timestamp', 'timestamp'),
    )


class OnlineTime(Base):
    """在线时长记录模型"""
    __tablename__ = 'online_time'

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(Text, nullable=False, default=str(datetime.datetime.now))
    duration = Column(Integer, nullable=False)
    start_timestamp = Column(DateTime, nullable=False, default=datetime.datetime.now)
    end_timestamp = Column(DateTime, nullable=False, index=True)

    __table_args__ = (
        Index('idx_onlinetime_end_timestamp', 'end_timestamp'),
    )


class PersonInfo(Base):
    """人物信息模型"""
    __tablename__ = 'person_info'

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(get_string_field(100), nullable=False, unique=True, index=True)
    person_name = Column(Text, nullable=True)
    name_reason = Column(Text, nullable=True)
    platform = Column(Text, nullable=False)
    user_id = Column(get_string_field(50), nullable=False, index=True)
    nickname = Column(Text, nullable=True)
    impression = Column(Text, nullable=True)
    short_impression = Column(Text, nullable=True)
    points = Column(Text, nullable=True)
    forgotten_points = Column(Text, nullable=True)
    info_list = Column(Text, nullable=True)
    know_times = Column(Float, nullable=True)
    know_since = Column(Float, nullable=True)
    last_know = Column(Float, nullable=True)
    attitude = Column(Integer, nullable=True, default=50)

    __table_args__ = (
        Index('idx_personinfo_person_id', 'person_id'),
        Index('idx_personinfo_user_id', 'user_id'),
    )


class Memory(Base):
    """记忆模型"""
    __tablename__ = 'memory'

    id = Column(Integer, primary_key=True, autoincrement=True)
    memory_id = Column(get_string_field(64), nullable=False, index=True)
    chat_id = Column(Text, nullable=True)
    memory_text = Column(Text, nullable=True)
    keywords = Column(Text, nullable=True)
    create_time = Column(Float, nullable=True)
    last_view_time = Column(Float, nullable=True)

    __table_args__ = (
        Index('idx_memory_memory_id', 'memory_id'),
    )


class Expression(Base):
    """表达风格模型"""
    __tablename__ = 'expression'

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    situation: Mapped[str] = mapped_column(Text, nullable=False)
    style: Mapped[str] = mapped_column(Text, nullable=False)
    count: Mapped[float] = mapped_column(Float, nullable=False)
    last_active_time: Mapped[float] = mapped_column(Float, nullable=False)
    chat_id: Mapped[str] = mapped_column(get_string_field(64), nullable=False, index=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    create_date: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index('idx_expression_chat_id', 'chat_id'),
    )


class ThinkingLog(Base):
    """思考日志模型"""
    __tablename__ = 'thinking_logs'

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(get_string_field(64), nullable=False, index=True)
    trigger_text = Column(Text, nullable=True)
    response_text = Column(Text, nullable=True)
    trigger_info_json = Column(Text, nullable=True)
    response_info_json = Column(Text, nullable=True)
    timing_results_json = Column(Text, nullable=True)
    chat_history_json = Column(Text, nullable=True)
    chat_history_in_thinking_json = Column(Text, nullable=True)
    chat_history_after_response_json = Column(Text, nullable=True)
    heartflow_data_json = Column(Text, nullable=True)
    reasoning_data_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now)

    __table_args__ = (
        Index('idx_thinkinglog_chat_id', 'chat_id'),
    )


class GraphNodes(Base):
    """记忆图节点模型"""
    __tablename__ = 'graph_nodes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    concept = Column(get_string_field(255), nullable=False, unique=True, index=True)
    memory_items = Column(Text, nullable=False)
    hash = Column(Text, nullable=False)
    created_time = Column(Float, nullable=False)
    last_modified = Column(Float, nullable=False)

    __table_args__ = (
        Index('idx_graphnodes_concept', 'concept'),
    )


class GraphEdges(Base):
    """记忆图边模型"""
    __tablename__ = 'graph_edges'

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(get_string_field(255), nullable=False, index=True)
    target = Column(get_string_field(255), nullable=False, index=True)
    strength = Column(Integer, nullable=False)
    hash = Column(Text, nullable=False)
    created_time = Column(Float, nullable=False)
    last_modified = Column(Float, nullable=False)

    __table_args__ = (
        Index('idx_graphedges_source', 'source'),
        Index('idx_graphedges_target', 'target'),
    )


class Schedule(Base):
    """日程模型"""
    __tablename__ = 'schedule'

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(get_string_field(10), nullable=False, unique=True, index=True)  # YYYY-MM-DD格式
    schedule_data = Column(Text, nullable=False)  # JSON格式的日程数据
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.now, onupdate=datetime.datetime.now)

    __table_args__ = (
        Index('idx_schedule_date', 'date'),
    )


class  MaiZoneScheduleStatus(Base):
    """麦麦空间日程处理状态模型"""
    __tablename__ = 'maizone_schedule_status'

    id = Column(Integer, primary_key=True, autoincrement=True)
    datetime_hour = Column(get_string_field(13), nullable=False, unique=True, index=True)  # YYYY-MM-DD HH格式，精确到小时
    activity = Column(Text, nullable=False)  # 该小时的活动内容
    is_processed = Column(Boolean, nullable=False, default=False)  # 是否已处理
    processed_at = Column(DateTime, nullable=True)  # 处理时间
    story_content = Column(Text, nullable=True)  # 生成的说说内容
    send_success = Column(Boolean, nullable=False, default=False)  # 是否发送成功
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now)
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.now, onupdate=datetime.datetime.now)

    __table_args__ = (
        Index('idx_maizone_datetime_hour', 'datetime_hour'),
        Index('idx_maizone_is_processed', 'is_processed'),
    )


class BanUser(Base):
    """被禁用用户模型"""
    __tablename__ = 'ban_users'

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(Text, nullable=False)
    user_id = Column(get_string_field(50), nullable=False, index=True)
    violation_num = Column(Integer, nullable=False, default=0)
    reason = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now)

    __table_args__ = (
        Index('idx_violation_num', 'violation_num'),
        Index('idx_banuser_user_id', 'user_id'),
        Index('idx_banuser_platform', 'platform'),
        Index('idx_banuser_platform_user_id', 'platform', 'user_id'),
    )


class AntiInjectionStats(Base):
    """反注入系统统计模型"""
    __tablename__ = 'anti_injection_stats'

    id = Column(Integer, primary_key=True, autoincrement=True)
    total_messages = Column(Integer, nullable=False, default=0)
    """总处理消息数"""
    
    detected_injections = Column(Integer, nullable=False, default=0)
    """检测到的注入攻击数"""
    
    blocked_messages = Column(Integer, nullable=False, default=0)
    """被阻止的消息数"""
    
    shielded_messages = Column(Integer, nullable=False, default=0)
    """被加盾的消息数"""
    
    processing_time_total = Column(Float, nullable=False, default=0.0)
    """总处理时间"""
    
    total_process_time = Column(Float, nullable=False, default=0.0)
    """累计总处理时间"""
    
    last_process_time = Column(Float, nullable=False, default=0.0)
    """最近一次处理时间"""
    
    error_count = Column(Integer, nullable=False, default=0)
    """错误计数"""
    
    start_time = Column(DateTime, nullable=False, default=datetime.datetime.now)
    """统计开始时间"""
    
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now)
    """记录创建时间"""
    
    updated_at = Column(DateTime, nullable=False, default=datetime.datetime.now, onupdate=datetime.datetime.now)
    """记录更新时间"""

    __table_args__ = (
        Index('idx_anti_injection_stats_created_at', 'created_at'),
        Index('idx_anti_injection_stats_updated_at', 'updated_at'),
    )


class CacheEntries(Base):
    """工具缓存条目模型"""
    __tablename__ = 'cache_entries'

    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(get_string_field(500), nullable=False, unique=True, index=True)
    """缓存键，包含工具名、参数和代码哈希"""
    
    cache_value = Column(Text, nullable=False)
    """缓存的数据，JSON格式"""
    
    expires_at = Column(Float, nullable=False, index=True)
    """过期时间戳"""
    
    tool_name = Column(get_string_field(100), nullable=False, index=True)
    """工具名称"""
    
    created_at = Column(Float, nullable=False, default=lambda: time.time())
    """创建时间戳"""
    
    last_accessed = Column(Float, nullable=False, default=lambda: time.time())
    """最后访问时间戳"""
    
    access_count = Column(Integer, nullable=False, default=0)
    """访问次数"""

    __table_args__ = (
        Index('idx_cache_entries_key', 'cache_key'),
        Index('idx_cache_entries_expires_at', 'expires_at'),
        Index('idx_cache_entries_tool_name', 'tool_name'),
        Index('idx_cache_entries_created_at', 'created_at'),
    )

class MonthlyPlan(Base):
    """月度计划模型"""
    __tablename__ = 'monthly_plans'

    id = Column(Integer, primary_key=True, autoincrement=True)
    plan_text = Column(Text, nullable=False)
    target_month = Column(String(7), nullable=False, index=True)  # "YYYY-MM"
    status = Column(get_string_field(20), nullable=False, default='active', index=True)  # 'active', 'completed', 'archived'
    usage_count = Column(Integer, nullable=False, default=0)
    last_used_date = Column(String(10), nullable=True, index=True)  # "YYYY-MM-DD" format
    created_at = Column(DateTime, nullable=False, default=datetime.datetime.now)
    
    # 保留 is_deleted 字段以兼容现有数据，但标记为已弃用
    is_deleted = Column(Boolean, nullable=False, default=False)

    __table_args__ = (
        Index('idx_monthlyplan_target_month_status', 'target_month', 'status'),
        Index('idx_monthlyplan_last_used_date', 'last_used_date'),
        Index('idx_monthlyplan_usage_count', 'usage_count'),
        # 保留旧索引以兼容
        Index('idx_monthlyplan_target_month_is_deleted', 'target_month', 'is_deleted'),
    )

# 数据库引擎和会话管理
_engine = None
_SessionLocal = None


def get_database_url():
    """获取数据库连接URL"""
    from src.config.config import global_config
    config = global_config.database

    if config.database_type == "mysql":
        # 对用户名和密码进行URL编码，处理特殊字符
        from urllib.parse import quote_plus
        encoded_user = quote_plus(config.mysql_user)
        encoded_password = quote_plus(config.mysql_password)
        
        # 检查是否配置了Unix socket连接
        if config.mysql_unix_socket:
            # 使用Unix socket连接
            encoded_socket = quote_plus(config.mysql_unix_socket)
            return (
                f"mysql+pymysql://{encoded_user}:{encoded_password}"
                f"@/{config.mysql_database}"
                f"?unix_socket={encoded_socket}&charset={config.mysql_charset}"
            )
        else:
            # 使用标准TCP连接
            return (
                f"mysql+pymysql://{encoded_user}:{encoded_password}"
                f"@{config.mysql_host}:{config.mysql_port}/{config.mysql_database}"
                f"?charset={config.mysql_charset}"
            )
    else:  # SQLite
        # 如果是相对路径，则相对于项目根目录
        if not os.path.isabs(config.sqlite_path):
            ROOT_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
            db_path = os.path.join(ROOT_PATH, config.sqlite_path)
        else:
            db_path = config.sqlite_path

        # 确保数据库目录存在
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

        return f"sqlite:///{db_path}"


def initialize_database():
    """初始化数据库引擎和会话"""
    global _engine, _SessionLocal

    if _engine is not None:
        return _engine, _SessionLocal

    database_url = get_database_url()
    from src.config.config import global_config
    config = global_config.database

    # 配置引擎参数
    engine_kwargs: Dict[str, Any] = {
        'echo': False,  # 生产环境关闭SQL日志
        'future': True,
    }

    if config.database_type == "mysql":
        # MySQL连接池配置
        engine_kwargs.update({
            'poolclass': QueuePool,
            'pool_size': config.connection_pool_size,
            'max_overflow': config.connection_pool_size * 2,
            'pool_timeout': config.connection_timeout,
            'pool_recycle': 3600,  # 1小时回收连接
            'pool_pre_ping': True,  # 连接前ping检查
            'connect_args': {
                'autocommit': config.mysql_autocommit,
                'charset': config.mysql_charset,
                'connect_timeout': config.connection_timeout,
                'read_timeout': 30,
                'write_timeout': 30,
            }
        })
    else:
        # SQLite配置 - 添加连接池设置以避免连接耗尽
        engine_kwargs.update({
            'poolclass': QueuePool,
            'pool_size': 20,  # 增加池大小
            'max_overflow': 30,  # 增加溢出连接数
            'pool_timeout': 60,  # 增加超时时间
            'pool_recycle': 3600,  # 1小时回收连接
            'pool_pre_ping': True,  # 连接前ping检查
            'connect_args': {
                'check_same_thread': False,
                'timeout': 30,
            }
        })

    _engine = create_engine(database_url, **engine_kwargs)
    _SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_engine)

    # 调用新的迁移函数，它会处理表的创建和列的添加
    from src.common.database.db_migration import check_and_migrate_database
    check_and_migrate_database()

    logger.info(f"SQLAlchemy数据库初始化成功: {config.database_type}")
    return _engine, _SessionLocal


@contextmanager
def get_db_session() -> Iterator[Session]:
    """数据库会话上下文管理器 - 推荐使用这个而不是get_session()"""
    session: Optional[Session] = None
    try:
        engine, SessionLocal = initialize_database()
        if not SessionLocal:
            raise RuntimeError("Database session not initialized")
        session = SessionLocal()
        yield session
        #session.commit()
    except Exception:
        if session:
            session.rollback()
        raise
    finally:
        if session:
            session.close()
            


def get_engine():
    """获取数据库引擎"""
    engine, _ = initialize_database()
    return engine


class PermissionNodes(Base):
    """权限节点模型"""
    __tablename__ = 'permission_nodes'

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(get_string_field(255), nullable=False, unique=True, index=True)  # 权限节点名称
    description = Column(Text, nullable=False)  # 权限描述
    plugin_name = Column(get_string_field(100), nullable=False, index=True)  # 所属插件
    default_granted = Column(Boolean, default=False, nullable=False)  # 默认是否授权
    created_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)  # 创建时间
    
    __table_args__ = (
        Index('idx_permission_plugin', 'plugin_name'),
        Index('idx_permission_node', 'node_name'),
    )


class UserPermissions(Base):
    """用户权限模型"""
    __tablename__ = 'user_permissions'

    id = Column(Integer, primary_key=True, autoincrement=True)
    platform = Column(get_string_field(50), nullable=False, index=True)  # 平台类型
    user_id = Column(get_string_field(100), nullable=False, index=True)  # 用户ID
    permission_node = Column(get_string_field(255), nullable=False, index=True)  # 权限节点名称
    granted = Column(Boolean, default=True, nullable=False)  # 是否授权
    granted_at = Column(DateTime, default=datetime.datetime.utcnow, nullable=False)  # 授权时间
    granted_by = Column(get_string_field(100), nullable=True)  # 授权者信息
    
    __table_args__ = (
        Index('idx_user_platform_id', 'platform', 'user_id'),
        Index('idx_user_permission', 'platform', 'user_id', 'permission_node'),
        Index('idx_permission_granted', 'permission_node', 'granted'),
    )
