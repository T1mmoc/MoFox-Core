"""
Kokoro Flow Chatter V2 - 配置

可以通过 TOML 配置文件覆盖默认值
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class WaitingDefaults:
    """等待配置默认值"""
    
    # 默认最大等待时间（秒）
    default_max_wait_seconds: int = 300
    
    # 最小等待时间
    min_wait_seconds: int = 30
    
    # 最大等待时间
    max_wait_seconds: int = 1800


@dataclass
class ProactiveConfig:
    """主动思考配置"""
    
    # 是否启用主动思考
    enabled: bool = True
    
    # 沉默阈值（秒），超过此时间考虑主动发起
    silence_threshold_seconds: int = 7200
    
    # 两次主动发起最小间隔（秒）
    min_interval_between_proactive: int = 1800
    
    # 勿扰时段开始（HH:MM 格式）
    quiet_hours_start: str = "23:00"
    
    # 勿扰时段结束
    quiet_hours_end: str = "07:00"
    
    # 主动发起概率（0.0 ~ 1.0）
    trigger_probability: float = 0.3


@dataclass
class PromptConfig:
    """提示词配置"""
    
    # 活动记录保留条数
    max_activity_entries: int = 30
    
    # 每条记录最大字符数
    max_entry_length: int = 500
    
    # 是否包含人物关系信息
    include_relation: bool = True
    
    # 是否包含记忆信息
    include_memory: bool = True


@dataclass
class SessionConfig:
    """会话配置"""
    
    # Session 持久化目录（相对于 data/）
    session_dir: str = "kokoro_flow_chatter_v2/sessions"
    
    # Session 自动过期时间（秒），超过此时间未活动自动清理
    session_expire_seconds: int = 86400 * 7  # 7 天
    
    # 活动记录保留上限
    max_mental_log_entries: int = 100


@dataclass  
class LLMConfig:
    """LLM 配置"""
    
    # 模型名称（空则使用默认）
    model_name: str = ""
    
    # Temperature
    temperature: float = 0.8
    
    # 最大 Token
    max_tokens: int = 1024
    
    # 请求超时（秒）
    timeout: float = 60.0


@dataclass
class KokoroFlowChatterV2Config:
    """Kokoro Flow Chatter V2 总配置"""
    
    # 是否启用
    enabled: bool = True
    
    # 启用的消息源类型（空列表表示全部）
    enabled_stream_types: List[str] = field(default_factory=lambda: ["private"])
    
    # 等待配置
    waiting: WaitingDefaults = field(default_factory=WaitingDefaults)
    
    # 主动思考配置
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    
    # 提示词配置
    prompt: PromptConfig = field(default_factory=PromptConfig)
    
    # 会话配置
    session: SessionConfig = field(default_factory=SessionConfig)
    
    # LLM 配置
    llm: LLMConfig = field(default_factory=LLMConfig)
    
    # 调试模式
    debug: bool = False


# 全局配置单例
_config: Optional[KokoroFlowChatterV2Config] = None


def get_config() -> KokoroFlowChatterV2Config:
    """获取全局配置"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def load_config() -> KokoroFlowChatterV2Config:
    """从全局配置加载 KFC V2 配置"""
    from src.config.config import global_config
    
    config = KokoroFlowChatterV2Config()
    
    # 尝试从全局配置读取
    if not global_config:
        return config
    
    try:
        if hasattr(global_config, 'kokoro_flow_chatter_v2'):
            kfc_cfg = getattr(global_config, 'kokoro_flow_chatter_v2')
            
            # 基础配置
            if hasattr(kfc_cfg, 'enabled'):
                config.enabled = kfc_cfg.enabled
            if hasattr(kfc_cfg, 'enabled_stream_types'):
                config.enabled_stream_types = list(kfc_cfg.enabled_stream_types)
            if hasattr(kfc_cfg, 'debug'):
                config.debug = kfc_cfg.debug
            
            # 等待配置
            if hasattr(kfc_cfg, 'waiting'):
                wait_cfg = kfc_cfg.waiting
                config.waiting = WaitingDefaults(
                    default_max_wait_seconds=getattr(wait_cfg, 'default_max_wait_seconds', 300),
                    min_wait_seconds=getattr(wait_cfg, 'min_wait_seconds', 30),
                    max_wait_seconds=getattr(wait_cfg, 'max_wait_seconds', 1800),
                )
            
            # 主动思考配置
            if hasattr(kfc_cfg, 'proactive'):
                pro_cfg = kfc_cfg.proactive
                config.proactive = ProactiveConfig(
                    enabled=getattr(pro_cfg, 'enabled', True),
                    silence_threshold_seconds=getattr(pro_cfg, 'silence_threshold_seconds', 7200),
                    min_interval_between_proactive=getattr(pro_cfg, 'min_interval_between_proactive', 1800),
                    quiet_hours_start=getattr(pro_cfg, 'quiet_hours_start', "23:00"),
                    quiet_hours_end=getattr(pro_cfg, 'quiet_hours_end', "07:00"),
                    trigger_probability=getattr(pro_cfg, 'trigger_probability', 0.3),
                )
            
            # 提示词配置
            if hasattr(kfc_cfg, 'prompt'):
                pmt_cfg = kfc_cfg.prompt
                config.prompt = PromptConfig(
                    max_activity_entries=getattr(pmt_cfg, 'max_activity_entries', 30),
                    max_entry_length=getattr(pmt_cfg, 'max_entry_length', 500),
                    include_relation=getattr(pmt_cfg, 'include_relation', True),
                    include_memory=getattr(pmt_cfg, 'include_memory', True),
                )
            
            # 会话配置
            if hasattr(kfc_cfg, 'session'):
                sess_cfg = kfc_cfg.session
                config.session = SessionConfig(
                    session_dir=getattr(sess_cfg, 'session_dir', "kokoro_flow_chatter_v2/sessions"),
                    session_expire_seconds=getattr(sess_cfg, 'session_expire_seconds', 86400 * 7),
                    max_mental_log_entries=getattr(sess_cfg, 'max_mental_log_entries', 100),
                )
            
            # LLM 配置
            if hasattr(kfc_cfg, 'llm'):
                llm_cfg = kfc_cfg.llm
                config.llm = LLMConfig(
                    model_name=getattr(llm_cfg, 'model_name', ""),
                    temperature=getattr(llm_cfg, 'temperature', 0.8),
                    max_tokens=getattr(llm_cfg, 'max_tokens', 1024),
                    timeout=getattr(llm_cfg, 'timeout', 60.0),
                )
    
    except Exception as e:
        from src.common.logger import get_logger
        logger = get_logger("kfc_v2_config")
        logger.warning(f"加载 KFC V2 配置失败，使用默认值: {e}")
    
    return config


def reload_config() -> KokoroFlowChatterV2Config:
    """重新加载配置"""
    global _config
    _config = load_config()
    return _config
