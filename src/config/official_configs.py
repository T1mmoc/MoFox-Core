import re

from dataclasses import dataclass, field
from typing import Literal, Optional

from src.config.config_base import ConfigBase

"""
须知：
1. 本文件中记录了所有的配置项
2. 所有新增的class都需要继承自ConfigBase
3. 所有新增的class都应在config.py中的Config类中添加字段
4. 对于新增的字段，若为可选项，则应在其后添加field()并设置default_factory或default
"""

@dataclass
class DatabaseConfig(ConfigBase):
    """数据库配置类"""

    database_type: Literal["sqlite", "mysql"] = "sqlite"
    """数据库类型，支持 sqlite 或 mysql"""

    # SQLite 配置
    sqlite_path: str = "data/MaiBot.db"
    """SQLite数据库文件路径"""

    # MySQL 配置
    mysql_host: str = "localhost"
    """MySQL服务器地址"""

    mysql_port: int = 3306
    """MySQL服务器端口"""

    mysql_database: str = "maibot"
    """MySQL数据库名"""

    mysql_user: str = "root"
    """MySQL用户名"""

    mysql_password: str = ""
    """MySQL密码"""

    mysql_charset: str = "utf8mb4"
    """MySQL字符集"""

    mysql_unix_socket: str = ""
    """MySQL Unix套接字路径（可选，用于本地连接，优先于host/port）"""

    # MySQL SSL 配置
    mysql_ssl_mode: str = "DISABLED"
    """SSL模式: DISABLED, PREFERRED, REQUIRED, VERIFY_CA, VERIFY_IDENTITY"""

    mysql_ssl_ca: str = ""
    """SSL CA证书路径"""

    mysql_ssl_cert: str = ""
    """SSL客户端证书路径"""

    mysql_ssl_key: str = ""
    """SSL客户端密钥路径"""

    # MySQL 高级配置
    mysql_autocommit: bool = True
    """自动提交事务"""

    mysql_sql_mode: str = "TRADITIONAL"
    """SQL模式"""

    # 连接池配置
    connection_pool_size: int = 10
    """连接池大小（仅MySQL有效）"""

    connection_timeout: int = 10
    """连接超时时间（秒）"""

@dataclass
class BotConfig(ConfigBase):
    """QQ机器人配置类"""

    platform: str
    """平台"""

    qq_account: str
    """QQ账号"""

    nickname: str
    """昵称"""

    alias_names: list[str] = field(default_factory=lambda: [])
    """别名列表"""


@dataclass
class PersonalityConfig(ConfigBase):
    """人格配置类"""

    personality_core: str
    """核心人格"""

    personality_side: str
    """人格侧写"""

    identity: str = ""
    """身份特征"""

    reply_style: str = ""
    """表达风格"""

    prompt_mode: Literal["s4u", "normal"] = "s4u"
    """Prompt模式选择：s4u为原有s4u样式，normal为0.9之前的模式"""

    compress_personality: bool = True
    """是否压缩人格，压缩后会精简人格信息，节省token消耗并提高回复性能，但是会丢失一些信息，如果人设不长，可以关闭"""

    compress_identity: bool = True
    """是否压缩身份，压缩后会精简身份信息，节省token消耗并提高回复性能，但是会丢失一些信息，如果不长，可以关闭"""


@dataclass
class RelationshipConfig(ConfigBase):
    """关系配置类"""

    enable_relationship: bool = True
    """是否启用关系系统"""

    relation_frequency: int = 1
    """关系频率，麦麦构建关系的速度"""


@dataclass
class ChatConfig(ConfigBase):
    """聊天配置类"""

    max_context_size: int = 18
    """上下文长度"""


    replyer_random_probability: float = 0.5
    """
    发言时选择推理模型的概率（0-1之间）
    选择普通模型的概率为 1 - reasoning_normal_model_probability
    """

    thinking_timeout: int = 40
    """麦麦最长思考规划时间，超过这个时间的思考会放弃（往往是api反应太慢）"""

    talk_frequency: float = 1
    """回复频率阈值"""

    mentioned_bot_inevitable_reply: bool = False
    """提及 bot 必然回复"""

    at_bot_inevitable_reply: bool = False
    """@bot 必然回复"""
    
    talk_frequency: float = 0.5
    """回复频率阈值"""

    # 合并后的时段频率配置
    talk_frequency_adjust: list[list[str]] = field(default_factory=lambda: [])


    focus_value: float = 0.5
    """麦麦的专注思考能力，越低越容易专注，消耗token也越多"""
    
    focus_value_adjust: list[list[str]] = field(default_factory=lambda: [])
    
    """
    统一的活跃度和专注度配置
    格式：[["platform:chat_id:type", "HH:MM,frequency", "HH:MM,frequency", ...], ...]

    全局配置示例：
    [["", "8:00,1", "12:00,2", "18:00,1.5", "00:00,0.5"]]

    特定聊天流配置示例：
    [
        ["", "8:00,1", "12:00,1.2", "18:00,1.5", "01:00,0.6"],  # 全局默认配置
        ["qq:1026294844:group", "12:20,1", "16:10,2", "20:10,1", "00:10,0.3"],  # 特定群聊配置
        ["qq:729957033:private", "8:20,1", "12:10,2", "20:10,1.5", "00:10,0.2"]  # 特定私聊配置
    ]

    说明：
    - 当第一个元素为空字符串""时，表示全局默认配置
    - 当第一个元素为"platform:id:type"格式时，表示特定聊天流配置
    - 后续元素是"时间,频率"格式，表示从该时间开始使用该频率，直到下一个时间点
    - 优先级：特定聊天流配置 > 全局配置 > 默认值
    
    注意：
    - talk_frequency_adjust 控制回复频率，数值越高回复越频繁
    - focus_value_adjust 控制专注思考能力，数值越低越容易专注，消耗token也越多
    """
    
    
    def get_current_focus_value(self, chat_stream_id: Optional[str] = None) -> float:
        """
        根据当前时间和聊天流获取对应的 focus_value
        """
        if not self.focus_value_adjust:
            return self.focus_value
        
        if chat_stream_id:
            stream_focus_value = self._get_stream_specific_focus_value(chat_stream_id)
            if stream_focus_value is not None:
                return stream_focus_value
        
        global_focus_value = self._get_global_focus_value()
        if global_focus_value is not None:
            return global_focus_value
        
        return self.focus_value

    force_focus_private: bool = False
    """是否强制私聊进入专注模式，开启后私聊将始终保持专注状态"""

    group_chat_mode: Literal["auto", "normal", "focus"] = "auto"
    """群聊聊天模式设置：auto-自动切换，normal-强制普通模式，focus-强制专注模式"""
    
    planner_custom_prompt_enable: bool = False
    """是否启用决策器自定义提示词"""
    
    planner_custom_prompt_content: str = ""
    """决策器自定义提示词内容，仅在planner_custom_prompt_enable为True时生效"""
    
    timestamp_display_mode: Literal["normal", "normal_no_YMD", "relative"] = "normal_no_YMD"
    """
    消息时间戳显示模式：
    - normal: 完整日期时间格式 (YYYY-MM-DD HH:MM:SS)
    - normal_no_YMD: 仅显示时间 (HH:MM:SS)
    - relative: 相对时间格式 (几分钟前/几小时前等)
    """

    def get_current_talk_frequency(self, chat_stream_id: Optional[str] = None) -> float:
        """
        根据当前时间和聊天流获取对应的 talk_frequency

        Args:
            chat_stream_id: 聊天流ID，格式为 "platform:chat_id:type"

        Returns:
            float: 对应的频率值
        """
        if not self.talk_frequency_adjust:
            return self.talk_frequency

        # 优先检查聊天流特定的配置
        if chat_stream_id:
            stream_frequency = self._get_stream_specific_frequency(chat_stream_id)
            if stream_frequency is not None:
                return stream_frequency

        # 检查全局时段配置（第一个元素为空字符串的配置）
        global_frequency = self._get_global_frequency()
        if global_frequency is not None:
            return global_frequency

        # 如果都没有匹配，返回默认值
        return self.talk_frequency

    def _get_time_based_frequency(self, time_freq_list: list[str]) -> Optional[float]:
        """
        根据时间配置列表获取当前时段的频率

        Args:
            time_freq_list: 时间频率配置列表，格式为 ["HH:MM,frequency", ...]

        Returns:
            float: 频率值，如果没有配置则返回 None
        """
        from datetime import datetime

        current_time = datetime.now().strftime("%H:%M")
        current_hour, current_minute = map(int, current_time.split(":"))
        current_minutes = current_hour * 60 + current_minute

        # 解析时间频率配置
        time_freq_pairs = []
        for time_freq_str in time_freq_list:
            try:
                time_str, freq_str = time_freq_str.split(",")
                hour, minute = map(int, time_str.split(":"))
                frequency = float(freq_str)
                minutes = hour * 60 + minute
                time_freq_pairs.append((minutes, frequency))
            except (ValueError, IndexError):
                continue

        if not time_freq_pairs:
            return None

        # 按时间排序
        time_freq_pairs.sort(key=lambda x: x[0])

        # 查找当前时间对应的频率
        current_frequency = None
        for minutes, frequency in time_freq_pairs:
            if current_minutes >= minutes:
                current_frequency = frequency
            else:
                break

        # 如果当前时间在所有配置时间之前，使用最后一个时间段的频率（跨天逻辑）
        if current_frequency is None and time_freq_pairs:
            current_frequency = time_freq_pairs[-1][1]

        return current_frequency

    def _get_stream_specific_focus_value(self, chat_stream_id: str) -> Optional[float]:
        """
        获取特定聊天流在当前时间的专注度

        Args:
            chat_stream_id: 聊天流ID（哈希值）

        Returns:
            float: 专注度值，如果没有配置则返回 None
        """
        # 查找匹配的聊天流配置
        for config_item in self.focus_value_adjust:
            if not config_item or len(config_item) < 2:
                continue

            stream_config_str = config_item[0]  # 例如 "qq:1026294844:group"

            # 解析配置字符串并生成对应的 chat_id
            config_chat_id = self._parse_stream_config_to_chat_id(stream_config_str)
            if config_chat_id is None:
                continue

            # 比较生成的 chat_id
            if config_chat_id != chat_stream_id:
                continue

            # 使用通用的时间专注度解析方法
            return self._get_time_based_focus_value(config_item[1:])

        return None

    def _get_stream_specific_frequency(self, chat_stream_id: str):
        """
        获取特定聊天流在当前时间的频率

        Args:
            chat_stream_id: 聊天流ID（哈希值）

        Returns:
            float: 频率值，如果没有配置则返回 None
        """
        # 查找匹配的聊天流配置
        for config_item in self.talk_frequency_adjust:
            if not config_item or len(config_item) < 2:
                continue

            stream_config_str = config_item[0]  # 例如 "qq:1026294844:group"

            # 解析配置字符串并生成对应的 chat_id
            config_chat_id = self._parse_stream_config_to_chat_id(stream_config_str)
            if config_chat_id is None:
                continue

            # 比较生成的 chat_id
            if config_chat_id != chat_stream_id:
                continue

            # 使用通用的时间频率解析方法
            return self._get_time_based_frequency(config_item[1:])

        return None

    def _parse_stream_config_to_chat_id(self, stream_config_str: str) -> Optional[str]:
        """
        解析流配置字符串并生成对应的 chat_id

        Args:
            stream_config_str: 格式为 "platform:id:type" 的字符串

        Returns:
            str: 生成的 chat_id，如果解析失败则返回 None
        """
        try:
            parts = stream_config_str.split(":")
            if len(parts) != 3:
                return None

            platform = parts[0]
            id_str = parts[1]
            stream_type = parts[2]

            # 判断是否为群聊
            is_group = stream_type == "group"

            # 使用与 ChatStream.get_stream_id 相同的逻辑生成 chat_id
            import hashlib

            if is_group:
                components = [platform, str(id_str)]
            else:
                components = [platform, str(id_str), "private"]
            key = "_".join(components)
            return hashlib.md5(key.encode()).hexdigest()

        except (ValueError, IndexError):
            return None

    def _get_global_frequency(self) -> Optional[float]:
        """
        获取全局默认频率配置

        Returns:
            float: 频率值，如果没有配置则返回 None
        """
        for config_item in self.talk_frequency_adjust:
            if not config_item or len(config_item) < 2:
                continue

            # 检查是否为全局默认配置（第一个元素为空字符串）
            if config_item[0] == "":
                return self._get_time_based_frequency(config_item[1:])

        return None


@dataclass
class MessageReceiveConfig(ConfigBase):
    """消息接收配置类"""

    ban_words: set[str] = field(default_factory=lambda: set())
    """过滤词列表"""

    ban_msgs_regex: set[str] = field(default_factory=lambda: set())
    """过滤正则表达式列表"""


@dataclass
class NormalChatConfig(ConfigBase):
    """普通聊天配置类"""

    willing_mode: str = "classical"
    """意愿模式"""

@dataclass
class ExpressionConfig(ConfigBase):
    """表达配置类"""

    expression_learning: list[list] = field(default_factory=lambda: [])
    """
    表达学习配置列表，支持按聊天流配置
    格式: [["chat_stream_id", "use_expression", "enable_learning", learning_intensity], ...]

    示例:
    [
        ["", "enable", "enable", 1.0],  # 全局配置：使用表达，启用学习，学习强度1.0
        ["qq:1919810:private", "enable", "enable", 1.5],  # 特定私聊配置：使用表达，启用学习，学习强度1.5
        ["qq:114514:private", "enable", "disable", 0.5],  # 特定私聊配置：使用表达，禁用学习，学习强度0.5
    ]

    说明:
    - 第一位: chat_stream_id，空字符串表示全局配置
    - 第二位: 是否使用学到的表达 ("enable"/"disable")
    - 第三位: 是否学习表达 ("enable"/"disable") 
    - 第四位: 学习强度（浮点数），影响学习频率，最短学习时间间隔 = 300/学习强度（秒）
    """

    expression_groups: list[list[str]] = field(default_factory=list)
    """
    表达学习互通组
    格式: [["qq:12345:group", "qq:67890:private"]]
    """

    def _parse_stream_config_to_chat_id(self, stream_config_str: str) -> Optional[str]:
        """
        解析流配置字符串并生成对应的 chat_id

        Args:
            stream_config_str: 格式为 "platform:id:type" 的字符串

        Returns:
            str: 生成的 chat_id，如果解析失败则返回 None
        """
        try:
            parts = stream_config_str.split(":")
            if len(parts) != 3:
                return None

            platform = parts[0]
            id_str = parts[1]
            stream_type = parts[2]

            # 判断是否为群聊
            is_group = stream_type == "group"

            # 使用与 ChatStream.get_stream_id 相同的逻辑生成 chat_id
            import hashlib

            if is_group:
                components = [platform, str(id_str)]
            else:
                components = [platform, str(id_str), "private"]
            key = "_".join(components)
            return hashlib.md5(key.encode()).hexdigest()

        except (ValueError, IndexError):
            return None

    def get_expression_config_for_chat(self, chat_stream_id: Optional[str] = None) -> tuple[bool, bool, float]:
        """
        根据聊天流ID获取表达配置

        Args:
            chat_stream_id: 聊天流ID，格式为哈希值

        Returns:
            tuple: (是否使用表达, 是否学习表达, 学习间隔)
        """
        if not self.expression_learning:
            # 如果没有配置，使用默认值：启用表达，启用学习，300秒间隔
            return True, True, 300

        # 优先检查聊天流特定的配置
        if chat_stream_id:
            specific_config = self._get_stream_specific_config(chat_stream_id)
            if specific_config is not None:
                return specific_config

        # 检查全局配置（第一个元素为空字符串的配置）
        global_config = self._get_global_config()
        if global_config is not None:
            return global_config

        # 如果都没有匹配，返回默认值
        return True, True, 300

    def _get_stream_specific_config(self, chat_stream_id: str) -> Optional[tuple[bool, bool, float]]:
        """
        获取特定聊天流的表达配置

        Args:
            chat_stream_id: 聊天流ID（哈希值）

        Returns:
            tuple: (是否使用表达, 是否学习表达, 学习间隔)，如果没有配置则返回 None
        """
        for config_item in self.expression_learning:
            if not config_item or len(config_item) < 4:
                continue

            stream_config_str = config_item[0]  # 例如 "qq:1026294844:group"

            # 如果是空字符串，跳过（这是全局配置）
            if stream_config_str == "":
                continue

            # 解析配置字符串并生成对应的 chat_id
            config_chat_id = self._parse_stream_config_to_chat_id(stream_config_str)
            if config_chat_id is None:
                continue

            # 比较生成的 chat_id
            if config_chat_id != chat_stream_id:
                continue

            # 解析配置
            try:
                use_expression = config_item[1].lower() == "enable"
                enable_learning = config_item[2].lower() == "enable"
                learning_intensity = float(config_item[3])
                return use_expression, enable_learning, learning_intensity
            except (ValueError, IndexError):
                continue

        return None

    def _get_global_config(self) -> Optional[tuple[bool, bool, float]]:
        """
        获取全局表达配置

        Returns:
            tuple: (是否使用表达, 是否学习表达, 学习间隔)，如果没有配置则返回 None
        """
        for config_item in self.expression_learning:
            if not config_item or len(config_item) < 4:
                continue

            # 检查是否为全局配置（第一个元素为空字符串）
            if config_item[0] == "":
                try:
                    use_expression = config_item[1].lower() == "enable"
                    enable_learning = config_item[2].lower() == "enable"
                    learning_intensity = float(config_item[3])
                    return use_expression, enable_learning, learning_intensity
                except (ValueError, IndexError):
                    continue

        return None


@dataclass
class ToolConfig(ConfigBase):
    """工具配置类"""

    enable_tool: bool = False
    """是否在聊天中启用工具"""

@dataclass
class VoiceConfig(ConfigBase):
    """语音识别配置类"""

    enable_asr: bool = False
    """是否启用语音识别"""


@dataclass
class EmojiConfig(ConfigBase):
    """表情包配置类"""

    emoji_chance: float = 0.6
    """发送表情包的基础概率"""

    max_reg_num: int = 200
    """表情包最大注册数量"""

    do_replace: bool = True
    """达到最大注册数量时替换旧表情包"""

    check_interval: int = 120
    """表情包检查间隔（分钟）"""

    steal_emoji: bool = True
    """是否偷取表情包，让麦麦可以发送她保存的这些表情包"""

    content_filtration: bool = False
    """是否开启表情包过滤"""

    filtration_prompt: str = "符合公序良俗"
    """表情包过滤要求"""

    enable_emotion_analysis: bool = True
    """是否启用表情包感情关键词二次识别，启用后表情包在第一次识别完毕后将送入第二次大模型识别来总结感情关键词，并构建进回复和决策器的上下文消息中"""


@dataclass
class MemoryConfig(ConfigBase):
    """记忆配置类"""

    enable_memory: bool = True

    memory_build_interval: int = 600
    """记忆构建间隔（秒）"""

    memory_build_distribution: tuple[
        float,
        float,
        float,
        float,
        float,
        float,
    ] = field(default_factory=lambda: (6.0, 3.0, 0.6, 32.0, 12.0, 0.4))
    """记忆构建分布，参数：分布1均值，标准差，权重，分布2均值，标准差，权重"""

    memory_build_sample_num: int = 8
    """记忆构建采样数量"""

    memory_build_sample_length: int = 40
    """记忆构建采样长度"""

    memory_compress_rate: float = 0.1
    """记忆压缩率"""

    forget_memory_interval: int = 1000
    """记忆遗忘间隔（秒）"""

    memory_forget_time: int = 24
    """记忆遗忘时间（小时）"""

    memory_forget_percentage: float = 0.01
    """记忆遗忘比例"""

    consolidate_memory_interval: int = 1000
    """记忆整合间隔（秒）"""

    consolidation_similarity_threshold: float = 0.7
    """整合相似度阈值"""

    consolidate_memory_percentage: float = 0.01
    """整合检查节点比例"""

    memory_ban_words: list[str] = field(default_factory=lambda: ["表情包", "图片", "回复", "聊天记录"])
    """不允许记忆的词列表"""

    enable_instant_memory: bool = True
    """是否启用即时记忆"""


@dataclass
class MoodConfig(ConfigBase):
    """情绪配置类"""

    enable_mood: bool = False
    """是否启用情绪系统"""

    mood_update_threshold: float = 1.0
    """情绪更新阈值,越高，更新越慢"""


@dataclass
class KeywordRuleConfig(ConfigBase):
    """关键词规则配置类"""

    keywords: list[str] = field(default_factory=lambda: [])
    """关键词列表"""

    regex: list[str] = field(default_factory=lambda: [])
    """正则表达式列表"""

    reaction: str = ""
    """关键词触发的反应"""

    def __post_init__(self):
        """验证配置"""
        if not self.keywords and not self.regex:
            raise ValueError("关键词规则必须至少包含keywords或regex中的一个")

        if not self.reaction:
            raise ValueError("关键词规则必须包含reaction")

        # 验证正则表达式
        for pattern in self.regex:
            try:
                re.compile(pattern)
            except re.error as e:
                raise ValueError(f"无效的正则表达式 '{pattern}': {str(e)}") from e


@dataclass
class KeywordReactionConfig(ConfigBase):
    """关键词配置类"""

    keyword_rules: list[KeywordRuleConfig] = field(default_factory=lambda: [])
    """关键词规则列表"""

    regex_rules: list[KeywordRuleConfig] = field(default_factory=lambda: [])
    """正则表达式规则列表"""

    def __post_init__(self):
        """验证配置"""
        # 验证所有规则
        for rule in self.keyword_rules + self.regex_rules:
            if not isinstance(rule, KeywordRuleConfig):
                raise ValueError(f"规则必须是KeywordRuleConfig类型，而不是{type(rule).__name__}")

@dataclass
class CustomPromptConfig(ConfigBase):
    """自定义提示词配置类"""

    image_prompt: str = ""
    """图片提示词"""


@dataclass
class ResponsePostProcessConfig(ConfigBase):
    """回复后处理配置类"""

    enable_response_post_process: bool = True
    """是否启用回复后处理，包括错别字生成器，回复分割器"""


@dataclass
class ChineseTypoConfig(ConfigBase):
    """中文错别字配置类"""

    enable: bool = True
    """是否启用中文错别字生成器"""

    error_rate: float = 0.01
    """单字替换概率"""

    min_freq: int = 9
    """最小字频阈值"""

    tone_error_rate: float = 0.1
    """声调错误概率"""

    word_replace_rate: float = 0.006
    """整词替换概率"""


@dataclass
class ResponseSplitterConfig(ConfigBase):
    """回复分割器配置类"""

    enable: bool = True
    """是否启用回复分割器"""

    max_length: int = 256
    """回复允许的最大长度"""

    max_sentence_num: int = 3
    """回复允许的最大句子数"""

    enable_kaomoji_protection: bool = False
    """是否启用颜文字保护"""


@dataclass
class TelemetryConfig(ConfigBase):
    """遥测配置类"""

    enable: bool = True
    """是否启用遥测"""


@dataclass
class DebugConfig(ConfigBase):
    """调试配置类"""

    show_prompt: bool = False
    """是否显示prompt"""


@dataclass
class ExperimentalConfig(ConfigBase):
    """实验功能配置类"""

    enable_friend_chat: bool = False
    """是否启用好友聊天"""

    pfc_chatting: bool = False
    """是否启用PFC"""


@dataclass
class MaimMessageConfig(ConfigBase):
    """maim_message配置类"""

    use_custom: bool = False
    """是否使用自定义的maim_message配置"""

    host: str = "127.0.0.1"
    """主机地址"""

    port: int = 8090
    """"端口号"""

    mode: Literal["ws", "tcp"] = "ws"
    """连接模式，支持ws和tcp"""

    use_wss: bool = False
    """是否使用WSS安全连接"""

    cert_file: str = ""
    """SSL证书文件路径，仅在use_wss=True时有效"""

    key_file: str = ""
    """SSL密钥文件路径，仅在use_wss=True时有效"""

    auth_token: list[str] = field(default_factory=lambda: [])
    """认证令牌，用于API验证，为空则不启用验证"""


@dataclass
class LPMMKnowledgeConfig(ConfigBase):
    """LPMM知识库配置类"""

    enable: bool = True
    """是否启用LPMM知识库"""

    rag_synonym_search_top_k: int = 10
    """RAG同义词搜索的Top K数量"""

    rag_synonym_threshold: float = 0.8
    """RAG同义词搜索的相似度阈值"""

    info_extraction_workers: int = 3
    """信息提取工作线程数"""

    qa_relation_search_top_k: int = 10
    """QA关系搜索的Top K数量"""

    qa_relation_threshold: float = 0.75
    """QA关系搜索的相似度阈值"""

    qa_paragraph_search_top_k: int = 1000
    """QA段落搜索的Top K数量"""

    qa_paragraph_node_weight: float = 0.05
    """QA段落节点权重"""

    qa_ent_filter_top_k: int = 10
    """QA实体过滤的Top K数量"""

    qa_ppr_damping: float = 0.8
    """QA PageRank阻尼系数"""

    qa_res_top_k: int = 10
    """QA最终结果的Top K数量"""

    embedding_dimension: int = 1024
    """嵌入向量维度，应该与模型的输出维度一致"""


@dataclass
class ScheduleConfig(ConfigBase):
    """日程配置类"""

    enable: bool = True
    """是否启用日程管理功能"""

    guidelines: Optional[str] = field(default=None)
    """日程生成指导原则，如果为None则使用默认指导原则"""


@dataclass
class VideoAnalysisConfig(ConfigBase):
    """视频分析配置类"""
    
    enable: bool = True
    """是否启用视频分析功能"""
    
    analysis_mode: Literal["frame_by_frame", "batch_frames", "auto"] = "auto"
    """分析模式：逐帧分析(慢但详细)、批量分析(快但可能略简单)或自动选择"""
    
    max_frames: int = 8
    """最大分析帧数"""
    
    frame_quality: int = 85
    """帧图像JPEG质量 (1-100)"""
    
    max_image_size: int = 800
    """单帧最大图像尺寸(像素)"""
    
    batch_analysis_prompt: str = field(default="""请分析这个视频的内容。这些图片是从视频中按时间顺序提取的关键帧。

请提供详细的分析，包括：
1. 视频的整体内容和主题
2. 主要人物、对象和场景描述
3. 动作、情节和时间线发展
4. 视觉风格和艺术特点
5. 整体氛围和情感表达
6. 任何特殊的视觉效果或文字内容

请用中文回答，分析要详细准确。""")
    """批量分析时使用的提示词"""
    
    enable_frame_timing: bool = True
    """是否在分析中包含帧的时间信息"""


@dataclass
class DependencyManagementConfig(ConfigBase):
    """插件Python依赖管理配置类"""
    
    auto_install: bool = True
    """是否启用自动安装Python依赖包（主开关）"""
    
    auto_install_timeout: int = 300
    """安装超时时间（秒）"""
    
    use_mirror: bool = False
    """是否使用PyPI镜像源"""
    
    mirror_url: str = ""
    """PyPI镜像源URL，如: "https://pypi.tuna.tsinghua.edu.cn/simple" """
    
    use_proxy: bool = False
    """是否使用网络代理（高级选项）"""
    
    proxy_url: str = ""
    """网络代理URL，如: "http://proxy.example.com:8080" """
    
    pip_options: list[str] = field(default_factory=lambda: [
        "--no-warn-script-location",
        "--disable-pip-version-check"
    ])
    """pip安装选项"""
    
    prompt_before_install: bool = False
    """安装前是否提示用户（暂未实现）"""
    
    install_log_level: str = "INFO"
    """依赖安装日志级别"""


@dataclass
class ExaConfig(ConfigBase):
    """EXA搜索引擎配置类"""
    
    api_key: str = "None"
    """EXA API密钥，用于联网搜索功能。请填入有效的EXA API密钥"""


@dataclass 
class WebSearchConfig(ConfigBase):
    """联网搜索组件配置类"""
    
    enable_web_search_tool: bool = True
    """是否启用联网搜索工具"""
    
    enable_url_tool: bool = True
    """是否启用URL解析工具"""