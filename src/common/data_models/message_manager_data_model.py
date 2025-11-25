"""
消息管理模块数据模型
定义消息管理器使用的数据结构
"""

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

from src.common.logger import get_logger
from src.config.config import global_config
from src.plugin_system.base.component_types import ChatMode, ChatType

from . import BaseDataModel

if TYPE_CHECKING:
    from .database_data_model import DatabaseMessages

logger = get_logger("stream_context")

_background_tasks: set[asyncio.Task] = set()
_unified_memory_manager = None


def _get_unified_memory_manager():
    """获取记忆体系单例"""
    global _unified_memory_manager
    if _unified_memory_manager is None:
        try:
            from src.memory_graph.manager_singleton import get_unified_memory_manager

            _unified_memory_manager = get_unified_memory_manager()
        except Exception as e:
            logger.warning(f"获取统一记忆管理器失败，可能未实现: {e}")
            _unified_memory_manager = False  # ���Ϊ���ã������ظ�����
    return _unified_memory_manager if _unified_memory_manager is not False else None


class MessageStatus(Enum):
    """消息状态枚举"""

    UNREAD = "unread"  # 未读消息
    READ = "read"  # 已读消息
    PROCESSING = "processing"  # 处理中


@dataclass
class DecisionRecord(BaseDataModel):
    """决策记录"""

    thought: str
    action: str


@dataclass
class StreamContext(BaseDataModel):
    """聊天流上下文信息"""

    stream_id: str
    chat_type: ChatType = ChatType.PRIVATE  # 聊天类型，默认为私聊
    chat_mode: ChatMode = ChatMode.FOCUS  # 聊天模式，默认为专注模式
    max_context_size: int = field(default_factory=lambda: getattr(global_config.chat, "max_context_size", 100))
    unread_messages: list["DatabaseMessages"] = field(default_factory=list)
    history_messages: list["DatabaseMessages"] = field(default_factory=list)
    last_check_time: float = field(default_factory=time.time)
    is_active: bool = True
    processing_task: asyncio.Task | None = None
    stream_loop_task: asyncio.Task | None = None  # 流循环任务
    is_chatter_processing: bool = False  # Chatter 是否正在处理
    interruption_count: int = 0  # 打断计数器
    last_interruption_time: float = 0.0  # 上次打断时间

    current_message: Optional["DatabaseMessages"] = None
    triggering_user_id: str | None = None  # 记录当前触发的用户ID
    is_replying: bool = False  # 是否正在进行回复
    processing_message_id: str | None = None  # 当前正在规划/处理的目标消息ID，用于防止重复回复
    decision_history: list["DecisionRecord"] = field(default_factory=list)  # 决策历史

    # 消息缓存系统相关字段
    message_cache: deque["DatabaseMessages"] = field(default_factory=deque)  # 消息缓存队列
    is_cache_enabled: bool = False  # 是否为当前用户启用缓存
    cache_stats: dict = field(default_factory=lambda: {
        "total_cached_messages": 0,
        "total_flushed_messages": 0,
        "cache_hits": 0,
        "cache_misses": 0
    })  # 缓存统计信息

    created_time: float = field(default_factory=time.time)
    last_access_time: float = field(default_factory=time.time)
    access_count: int = 0
    total_messages: int = 0
    _history_initialized: bool = field(default=False, init=False)

    def __post_init__(self):
        """初始化历史消息异步加载"""
        if not self.max_context_size or self.max_context_size <= 0:
            self.max_context_size = getattr(global_config.chat, "max_context_size", 100)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                task = asyncio.create_task(self._initialize_history_from_db())
                _background_tasks.add(task)
                task.add_done_callback(_background_tasks.discard)
        except RuntimeError:
            # 事件循环未运行时，await ensure_history_initialized 进行初始化
            pass

    def _update_access_stats(self):
        """更新访问统计信息，记录最后访问时间"""
        self.last_access_time = time.time()
        self.access_count += 1

    async def add_message(self, message: "DatabaseMessages", skip_energy_update: bool = False) -> bool:
        """添加消息到上下文，支持跳过能量更新的选项"""
        try:
            cache_enabled = global_config.chat.enable_message_cache
            if cache_enabled and not self.is_cache_enabled:
                self.enable_cache(True)
                logger.debug(f"为StreamContext {self.stream_id} 启用消息缓存系统")

            if message.interest_value is None:
                message.interest_value = 0.3
            message.should_reply = False
            message.should_act = False
            message.interest_calculated = False
            message.semantic_embedding = None
            message.is_read = False

            success = self.add_message_with_cache_check(message, force_direct=not cache_enabled)
            if not success:
                logger.error(f"StreamContext消息添加失败: {self.stream_id}")
                return False

            self._detect_chat_type(message)
            self.total_messages += 1
            self._update_access_stats()

            if cache_enabled and self.is_cache_enabled:
                if self.is_chatter_processing:
                    logger.debug(f"消息已缓存到StreamContext等待处理: stream={self.stream_id}")
                else:
                    logger.debug(f"消息直接添加到StreamContext未处理列表: stream={self.stream_id}")
            else:
                logger.debug(f"消息添加到StreamContext成功: {self.stream_id}")
            # ͬ�����ݵ�ͳһ�������
            try:
                if global_config.memory and global_config.memory.enable:
                    unified_manager = _get_unified_memory_manager()
                    if unified_manager:
                        message_dict = {
                            "message_id": str(message.message_id),
                            "sender_id": message.user_info.user_id,
                            "sender_name": message.user_info.user_nickname,
                            "content": message.processed_plain_text or message.display_message or "",
                            "timestamp": message.time,
                            "platform": message.chat_info.platform,
                            "stream_id": self.stream_id,
                        }
                        await unified_manager.add_message(message_dict)
                        logger.debug(f"��Ϣ�����ӵ��������ϵͳ: {message.message_id}")
            except Exception as e:
                logger.error(f"������Ϣ���������ϵͳʧ��: {e}", exc_info=True)

            return True
        except Exception as e:
            logger.error(f"������Ϣ������������ʧ�� {self.stream_id}: {e}", exc_info=True)
            return False

    async def update_message(self, message_id: str, updates: dict[str, Any]) -> bool:
        """�����������е���Ϣ"""
        try:
            for message in self.unread_messages:
                if str(message.message_id) == str(message_id):
                    if "interest_value" in updates:
                        message.interest_value = updates["interest_value"]
                    if "actions" in updates:
                        message.actions = updates["actions"]
                    if "should_reply" in updates:
                        message.should_reply = updates["should_reply"]
                    break

            for message in self.history_messages:
                if str(message.message_id) == str(message_id):
                    if "interest_value" in updates:
                        message.interest_value = updates["interest_value"]
                    if "actions" in updates:
                        message.actions = updates["actions"]
                    if "should_reply" in updates:
                        message.should_reply = updates["should_reply"]
                    break

            logger.debug(f"���µ�����������Ϣ: {self.stream_id}/{message_id}")
            return True
        except Exception as e:
            logger.error(f"���µ�����������Ϣʧ�� {self.stream_id}/{message_id}: {e}", exc_info=True)
            return False

    def add_action_to_message(self, message_id: str, action: str):
        """
        向指定消息添加执行的动作

        Args:
            message_id: 消息ID
            action: 要添加的动作名称
        """
        # 在未读消息中查找并更新（统一转字符串比较）
        for message in self.unread_messages:
            if str(message.message_id) == str(message_id):
                message.add_action(action)
                break

        # 在历史消息中查找并更新（统一转字符串比较）
        for message in self.history_messages:
            if str(message.message_id) == str(message_id):
                message.add_action(action)
                break

    def mark_message_as_read(self, message_id: str, max_history_size: int | None = None):
        """标记消息为已读"""
        # 先找到要标记的消息（处理 int/str 类型不匹配问题）
        message_to_mark = None
        for msg in self.unread_messages:
            # 统一转换为字符串比较，避免 int vs str 导致的匹配失败
            if str(msg.message_id) == str(message_id):
                message_to_mark = msg
                break

        # 然后移动到历史消息
        if message_to_mark:
            message_to_mark.is_read = True

            # 应用历史消息长度限制
            if max_history_size is None:
                max_history_size = self.max_context_size

            # 如果历史消息已达到最大长度，移除最旧的消息
            if len(self.history_messages) >= max_history_size:
                # 移除最旧的历史消息（保持先进先出）
                removed_count = len(self.history_messages) - max_history_size + 1
                self.history_messages = self.history_messages[removed_count:]

            self.history_messages.append(message_to_mark)
            self.unread_messages.remove(message_to_mark)

    def get_unread_messages(self) -> list["DatabaseMessages"]:
        """获取未读消息"""
        return [msg for msg in self.unread_messages if not msg.is_read]

    def get_history_messages(self, limit: int = 20) -> list["DatabaseMessages"]:
        """获取历史消息"""
        # 优先返回最近的历史消息和所有未读消息
        recent_history = self.history_messages[-limit:] if len(self.history_messages) > limit else self.history_messages
        return recent_history

    def get_messages(self, limit: int | None = None, include_unread: bool = True) -> list["DatabaseMessages"]:
        """获取上下文中的消息集合"""
        try:
            messages: list["DatabaseMessages"] = []
            if include_unread:
                messages.extend(self.get_unread_messages())

            if limit:
                messages.extend(self.get_history_messages(limit=limit))
            else:
                messages.extend(self.get_history_messages())

            messages.sort(key=lambda msg: getattr(msg, "time", 0))

            if limit and len(messages) > limit:
                messages = messages[-limit:]

            self._update_access_stats()
            return messages
        except Exception as e:
            logger.error(f"获取上下文消息失败 {self.stream_id}: {e}", exc_info=True)
            return []

    def mark_messages_as_read(self, message_ids: list[str]) -> bool:
        """批量标记消息为已读"""
        try:
            marked_count = 0
            for message_id in message_ids:
                try:
                    self.mark_message_as_read(message_id, max_history_size=self.max_context_size)
                    marked_count += 1
                except Exception as e:
                    logger.warning(f"标记消息已读失败 {message_id}: {e}")
            return marked_count > 0
        except Exception as e:
            logger.error(f"批量标记消息已读失败 {self.stream_id}: {e}", exc_info=True)
            return False

    def calculate_interruption_probability(self, max_limit: int, min_probability: float = 0.1, probability_factor: float | None = None) -> float:
        """计算打断概率 - 使用反比例函数模型

        Args:
            max_limit: 最大打断次数
            min_probability: 最低打断概率
            probability_factor: 已废弃的参数，保留是为了向后兼容，不再使用

        Returns:
            float: 打断概率 (0.0 - 1.0)
        """
        if max_limit <= 0:
            return 0.0

        # 如果已达到或超过最大次数，完全禁止打断
        if self.interruption_count >= max_limit:
            return 0.0

        # 反比例函数概率计算：前期高概率，快速衰减到低概率
        # 公式：概率 = a / (count + b) + min_probability
        # 参数设计：
        # - a = 1.4 (反比例系数)
        # - b = 2.0 (平移参数)
        # 这确保了：
        # - 第1次打断：80% 概率 (count=0)
        # - 第2次打断：35% 概率 (count=1)
        # - 第3次打断：15% 概率 (count=2)
        # - 第4次及以后：约10% 概率 (趋近于min_probability)
        # - 达到max_limit：0% 概率 (达到上限)

        a = 1.4  # 反比例系数
        b = 2.0  # 平移参数

        probability = a / (self.interruption_count + b) + min_probability

        # 确保概率在合理范围内
        probability = max(min_probability, probability)
        return max(0.0, min(1.0, probability))

    async def clear_context(self) -> bool:
        """清空上下文的未读与历史消息并重置状态"""
        try:
            self.unread_messages.clear()
            self.history_messages.clear()
            for attr in ["interruption_count", "afc_threshold_adjustment", "last_check_time"]:
                if hasattr(self, attr):
                    if attr in ["interruption_count", "afc_threshold_adjustment"]:
                        setattr(self, attr, 0)
                    else:
                        setattr(self, attr, time.time())
            await self._update_stream_energy()
            logger.debug(f"清空上下文成功: {self.stream_id}")
            return True
        except Exception as e:
            logger.error(f"清空上下文失败 {self.stream_id}: {e}", exc_info=True)
            return False

    def get_statistics(self) -> dict[str, Any]:
        """获取上下文统计信息"""
        try:
            current_time = time.time()
            uptime = current_time - self.created_time

            stats = {
                "stream_id": self.stream_id,
                "context_type": type(self).__name__,
                "total_messages": len(self.history_messages) + len(self.unread_messages),
                "unread_messages": len(self.unread_messages),
                "history_messages": len(self.history_messages),
                "is_active": self.is_active,
                "last_check_time": self.last_check_time,
                "interruption_count": self.interruption_count,
                "afc_threshold_adjustment": getattr(self, "afc_threshold_adjustment", 0.0),
                "created_time": self.created_time,
                "last_access_time": self.last_access_time,
                "access_count": self.access_count,
                "uptime_seconds": uptime,
                "idle_seconds": current_time - self.last_access_time,
            }

            stats["cache_stats"] = self.get_cache_stats()
            return stats
        except Exception as e:
            logger.error(f"获取上下文统计失败 {self.stream_id}: {e}", exc_info=True)
            return {}

    def validate_integrity(self) -> bool:
        """校验上下文结构完整性"""
        try:
            required_attrs = ["stream_id", "unread_messages", "history_messages"]
            for attr in required_attrs:
                if not hasattr(self, attr):
                    logger.warning(f"上下文缺少必要属性: {attr}")
                    return False

            all_messages = self.unread_messages + self.history_messages
            message_ids = [msg.message_id for msg in all_messages if hasattr(msg, "message_id")]
            if len(message_ids) != len(set(message_ids)):
                logger.warning(f"上下文中存在重复的消息ID: {self.stream_id}")
                return False

            return True

        except Exception as e:
            logger.error(f"校验上下文完整性失败 {self.stream_id}: {e}")
            return False


    async def increment_interruption_count(self):
        """增加打断计数"""
        self.interruption_count += 1
        self.last_interruption_time = time.time()

        # 同步打断计数到ChatStream
        await self._sync_interruption_count_to_stream()

    async def reset_interruption_count(self):
        """重置打断计数"""
        self.interruption_count = 0
        self.last_interruption_time = 0.0

        # 同步打断计数到ChatStream
        await self._sync_interruption_count_to_stream()


    async def _sync_interruption_count_to_stream(self):
        """同步打断计数到ChatStream"""
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager

            chat_manager = get_chat_manager()
            if chat_manager:
                chat_stream = await chat_manager.get_stream(self.stream_id)
                if chat_stream and hasattr(chat_stream, "interruption_count"):
                    # 在这里我们只是标记需要保存，实际的保存会在下次save时进行
                    chat_stream.saved = False
                    logger.debug(
                        f"已同步StreamContext {self.stream_id} 的打断计数 {self.interruption_count} 到ChatStream"
                    )
        except Exception as e:
            logger.warning(f"同步打断计数到ChatStream失败: {e}")

    def set_current_message(self, message: "DatabaseMessages"):
        """设置当前消息"""
        self.current_message = message

    def get_template_name(self) -> str | None:
        """获取模板名称"""
        if (
            self.current_message
            and hasattr(self.current_message, "additional_config")
            and self.current_message.additional_config
        ):
            import orjson
            try:
                config = orjson.loads(self.current_message.additional_config)
                if config.get("template_info") and not config.get("template_default", True):
                    return config.get("template_name")
            except (orjson.JSONDecodeError, AttributeError):
                pass
        return None

    def get_last_message(self) -> Optional["DatabaseMessages"]:
        """获取最后一条消息"""
        if self.current_message:
            return self.current_message
        if self.unread_messages:
            return self.unread_messages[-1]
        if self.history_messages:
            return self.history_messages[-1]
        return None

    async def ensure_history_initialized(self):
        """初始化历史消息异步加载"""
        if not self._history_initialized:
            await self._initialize_history_from_db()

    async def refresh_focus_energy_from_history(self) -> None:
        """根据历史消息刷新关注能量"""
        await self._update_stream_energy(include_unread=False)

    async def _update_stream_energy(self, include_unread: bool = False) -> None:
        """使用当前上下文消息更新关注能量"""
        try:
            history_messages = self.get_history_messages(limit=self.max_context_size)
            messages: list["DatabaseMessages"] = list(history_messages)

            if include_unread:
                messages.extend(self.get_unread_messages())

            user_id = None
            if messages:
                last_message = messages[-1]
                if hasattr(last_message, "user_info") and last_message.user_info:
                    user_id = last_message.user_info.user_id

            from src.chat.energy_system import energy_manager

            await energy_manager.calculate_focus_energy(
                stream_id=self.stream_id,
                messages=messages,
                user_id=user_id,
            )

        except Exception as e:
            logger.error(f"更新能量体系失败 {self.stream_id}: {e}")

    async def _initialize_history_from_db(self):
        """Load history messages from database into context."""
        if self._history_initialized:
            logger.debug(f"历史信息已初始化,stream={self.stream_id}, 当前条数={len(self.history_messages)}")
            return

        logger.info(f"?? [历史加载] 开始从数据库读取历史消息: {self.stream_id}")
        self._history_initialized = True

        try:
            logger.debug(f"开始加载数据库历史消息: {self.stream_id}")

            from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat

            db_messages = await get_raw_msg_before_timestamp_with_chat(
                chat_id=self.stream_id,
                timestamp=time.time(),
                limit=self.max_context_size,
            )

            if db_messages:
                logger.info(f"[历史加载] 从数据库获取到 {len(db_messages)} 条历史消息")
                loaded_count = 0
                for msg_dict in db_messages:
                    try:
                        db_msg = DatabaseMessages(**msg_dict)
                        db_msg.is_read = True
                        self.history_messages.append(db_msg)
                        loaded_count += 1

                    except Exception as e:
                        logger.warning(f"转换历史消息失败 (message_id={msg_dict.get('message_id', 'unknown')}): {e}")
                        continue

                if len(self.history_messages) > self.max_context_size:
                    removed_count = len(self.history_messages) - self.max_context_size
                    self.history_messages = self.history_messages[-self.max_context_size :]
                    logger.debug(f"[历史加载] 移除了 {removed_count} 条最早的消息以适配当前容量限制")

                logger.info(f"[历史加载] 成功加载 {loaded_count} 条历史消息到内存: {self.stream_id}")
            else:
                logger.debug(f"无历史消息需要加载: {self.stream_id}")

        except Exception as e:
            logger.error(f"从数据库加载历史消息失败: {self.stream_id}, {e}", exc_info=True)
            self._history_initialized = False

    def _detect_chat_type(self, message: "DatabaseMessages"):
        """基于消息内容检测聊天类型"""
        if len(self.unread_messages) == 1:
            if message.chat_info.group_info:
                self.chat_type = ChatType.GROUP
            else:
                self.chat_type = ChatType.PRIVATE

    async def _calculate_message_interest(self, message: "DatabaseMessages") -> float:
        """调用兴趣系统计算消息兴趣值"""
        try:
            from src.chat.interest_system.interest_manager import get_interest_manager

            interest_manager = get_interest_manager()

            if interest_manager.has_calculator():
                result = await interest_manager.calculate_interest(message)

                if result.success:
                    message.interest_value = result.interest_value
                    message.should_reply = result.should_reply
                    message.should_act = result.should_act
                    message.interest_calculated = True

                    logger.debug(
                        f"消息 {message.message_id} 兴趣值已更新: {result.interest_value:.3f}, "
                        f"should_reply: {result.should_reply}, should_act: {result.should_act}"
                    )
                    return result.interest_value
                else:
                    logger.warning(f"消息 {message.message_id} 兴趣值计算失败: {result.error_message}")
                    message.interest_calculated = False
                    return 0.5
            else:
                logger.debug("未找到兴趣计算器，使用默认兴趣值")
                return 0.5

        except Exception as e:
            logger.error(f"计算消息兴趣时出现异常: {e}", exc_info=True)
            if hasattr(message, "interest_calculated"):
                message.interest_calculated = False
            return 0.5

    def check_types(self, types: list) -> bool:
        """
        检查当前消息是否支持指定的类型

        Args:
            types: 需要检查的消息类型列表，如 ["text", "image", "emoji"]

        Returns:
            bool: 如果消息支持所有指定的类型则返回True，否则返回False
        """
        if not self.current_message:
            logger.warning("[问题] StreamContext.check_types: current_message 为 None")
            return False

        if not types:
            # 如果没有指定类型要求，默认为支持
            return True

        logger.debug(f"[check_types] 检查消息是否支持类型: {types}")

        # 优先从additional_config中获取format_info
        if hasattr(self.current_message, "additional_config") and self.current_message.additional_config:
            import orjson
            try:
                logger.debug(f"[check_types] additional_config 类型: {type(self.current_message.additional_config)}")
                config = orjson.loads(self.current_message.additional_config)
                logger.debug(f"[check_types] 解析后的 config 键: {config.keys() if isinstance(config, dict) else 'N/A'}")

                # 检查format_info结构
                if "format_info" in config:
                    format_info = config["format_info"]
                    logger.debug(f"[check_types] 找到 format_info: {format_info}")

                    # 方法1: 直接检查accept_format字段
                    if "accept_format" in format_info:
                        accept_format = format_info["accept_format"]
                        # 确保accept_format是列表类型
                        if isinstance(accept_format, str):
                            accept_format = [accept_format]
                        elif isinstance(accept_format, list):
                            pass
                        else:
                            # 如果accept_format不是字符串或列表，尝试转换为列表
                            accept_format = list(accept_format) if hasattr(accept_format, "__iter__") else []

                        # 检查所有请求的类型是否都被支持
                        for requested_type in types:
                            if requested_type not in accept_format:
                                logger.debug(f"[check_types] 消息不支持类型 '{requested_type}'，支持的类型: {accept_format}")
                                return False
                        logger.debug("[check_types] ✅ 消息支持所有请求的类型 (来自 accept_format)")
                        return True

                    # 方法2: 检查content_format字段（向后兼容）
                    elif "content_format" in format_info:
                        content_format = format_info["content_format"]
                        # 确保content_format是列表类型
                        if isinstance(content_format, str):
                            content_format = [content_format]
                        elif isinstance(content_format, list):
                            pass
                        else:
                            content_format = list(content_format) if hasattr(content_format, "__iter__") else []

                        # 检查所有请求的类型是否都被支持
                        for requested_type in types:
                            if requested_type not in content_format:
                                logger.debug(f"[check_types] 消息不支持类型 '{requested_type}'，支持的内容格式: {content_format}")
                                return False
                        logger.debug("[check_types] ✅ 消息支持所有请求的类型 (来自 content_format)")
                        return True
                else:
                    logger.warning("[check_types] [问题] additional_config 中没有 format_info 字段")

            except (orjson.JSONDecodeError, AttributeError, TypeError) as e:
                logger.warning(f"[check_types] [问题] 解析消息格式信息失败: {e}")
        else:
            logger.warning("[check_types] [问题] current_message 没有 additional_config 或为空")

        # 备用方案：如果无法从additional_config获取格式信息，使用默认支持的类型
        # 大多数消息至少支持text类型
        logger.debug("[check_types] 使用备用方案：默认支持类型检查")
        default_supported_types = ["text", "emoji"]
        for requested_type in types:
            if requested_type not in default_supported_types:
                logger.debug(f"[check_types] 使用默认类型检查，消息可能不支持类型 '{requested_type}'")
                # 对于非基础类型，返回False以避免错误
                if requested_type not in ["text", "emoji", "reply"]:
                    logger.warning(f"[check_types] ❌ 备用方案拒绝类型 '{requested_type}'")
                    return False
        logger.debug("[check_types] ✅ 备用方案通过所有类型检查")
        return True

    # ==================== 消息缓存系统方法 ====================

    def enable_cache(self, enabled: bool = True):
        """
        启用或禁用消息缓存系统

        Args:
            enabled: 是否启用缓存
        """
        self.is_cache_enabled = enabled
        logger.debug(f"StreamContext {self.stream_id} 缓存系统已{'启用' if enabled else '禁用'}")

    def add_message_to_cache(self, message: "DatabaseMessages") -> bool:
        """
        添加消息到缓存队列

        Args:
            message: 要缓存的消息

        Returns:
            bool: 是否成功添加到缓存
        """
        if not self.is_cache_enabled:
            self.cache_stats["cache_misses"] += 1
            logger.debug(f"StreamContext {self.stream_id} 缓存未启用，消息无法缓存")
            return False

        try:
            self.message_cache.append(message)
            self.cache_stats["total_cached_messages"] += 1
            self.cache_stats["cache_hits"] += 1
            logger.debug(f"消息已添加到缓存: stream={self.stream_id}, message_id={message.message_id}, 缓存大小={len(self.message_cache)}")
            return True
        except Exception as e:
            logger.error(f"添加消息到缓存失败: stream={self.stream_id}, error={e}")
            return False

    def flush_cached_messages(self) -> list["DatabaseMessages"]:
        """
        刷新缓存消息到未读消息列表

        Returns:
            list[DatabaseMessages]: 刷新的消息列表
        """
        if not self.message_cache:
            logger.debug(f"StreamContext {self.stream_id} 缓存为空，无需刷新")
            return []

        try:
            cached_messages = list(self.message_cache)
            cache_size = len(cached_messages)

            # 清空缓存队列
            self.message_cache.clear()

            # 将缓存消息添加到未读消息列表
            self.unread_messages.extend(cached_messages)

            # 更新统计信息
            self.cache_stats["total_flushed_messages"] += cache_size

            logger.debug(f"缓存消息已刷新到未读列表: stream={self.stream_id}, 数量={cache_size}")
            return cached_messages

        except Exception as e:
            logger.error(f"刷新缓存消息失败: stream={self.stream_id}, error={e}")
            return []

    def get_cache_size(self) -> int:
        """
        获取当前缓存大小

        Returns:
            int: 缓存中的消息数量
        """
        return len(self.message_cache)

    def clear_cache(self):
        """清空消息缓存"""
        cache_size = len(self.message_cache)
        self.message_cache.clear()
        logger.debug(f"消息缓存已清空: stream={self.stream_id}, 清空数量={cache_size}")

    def has_cached_messages(self) -> bool:
        """
        检查是否有缓存的消息

        Returns:
            bool: 是否有缓存消息
        """
        return len(self.message_cache) > 0

    def get_cache_stats(self) -> dict:
        """
        获取缓存统计信息

        Returns:
            dict: 缓存统计数据
        """
        stats = self.cache_stats.copy()
        stats.update({
            "current_cache_size": len(self.message_cache),
            "is_cache_enabled": self.is_cache_enabled,
            "stream_id": self.stream_id
        })
        return stats

    def add_message_with_cache_check(self, message: "DatabaseMessages", force_direct: bool = False) -> bool:
        """
        智能添加消息：根据缓存状态决定是缓存还是直接添加到未读列表

        Args:
            message: 要添加的消息
            force_direct: 是否强制直接添加到未读列表（跳过缓存）

        Returns:
            bool: 是否成功添加
        """
        try:
            # 如果强制直接添加或缓存未启用，直接添加到未读列表
            if force_direct or not self.is_cache_enabled:
                self.unread_messages.append(message)
                logger.debug(f"消息直接添加到未读列表: stream={self.stream_id}, message_id={message.message_id}")
                return True

            # 如果正在处理中，添加到缓存
            if self.is_chatter_processing:
                return self.add_message_to_cache(message)

            # 如果没有在处理，先刷新缓存再添加到未读列表
            self.flush_cached_messages()
            self.unread_messages.append(message)
            logger.debug(f"消息添加到未读列表（已刷新缓存）: stream={self.stream_id}, message_id={message.message_id}")
            return True

        except Exception as e:
            logger.error(f"智能添加消息失败: stream={self.stream_id}, error={e}")
            return False

    def __deepcopy__(self, memo):
        """自定义深拷贝，跳过不可序列化的 asyncio.Task (processing_task)。

        deepcopy 在内部可能会尝试 pickle 某些对象（如 asyncio.Task），
        这会在多线程或运行时事件循环中导致 TypeError。这里我们手动复制
        __dict__ 中的字段，确保 processing_task 被设置为 None，其他字段使用
        copy.deepcopy 递归复制。
        """
        import copy

        # 如果已经复制过，直接返回缓存结果
        obj_id = id(self)
        if obj_id in memo:
            return memo[obj_id]

        # 创建一个未初始化的新实例，然后逐个字段深拷贝
        cls = self.__class__
        new = cls.__new__(cls)
        memo[obj_id] = new

        for k, v in self.__dict__.items():
            if k in ["processing_task", "stream_loop_task"]:
                # 不复制 asyncio.Task，避免无法 pickling
                setattr(new, k, None)
            elif k == "message_cache":
                # 深拷贝消息缓存队列
                try:
                    setattr(new, k, copy.deepcopy(v, memo))
                except Exception:
                    # 如果拷贝失败，创建新的空队列
                    setattr(new, k, deque())
            else:
                try:
                    setattr(new, k, copy.deepcopy(v, memo))
                except Exception:
                    # 如果某个字段无法深拷贝，退回到原始引用（安全性谨慎）
                    setattr(new, k, v)

        return new


@dataclass
class MessageManagerStats(BaseDataModel):
    """消息管理器统计信息"""

    total_streams: int = 0
    active_streams: int = 0
    total_unread_messages: int = 0
    total_processed_messages: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def uptime(self) -> float:
        """运行时间"""
        return time.time() - self.start_time


@dataclass
class StreamStats(BaseDataModel):
    """聊天流统计信息"""

    stream_id: str
    is_active: bool
    unread_count: int
    history_count: int
    last_check_time: float
    has_active_task: bool
