"""
Kokoro Flow Chatter (心流聊天器) 主类

核心聊天处理器，协调所有组件完成"体验-决策-行动"的交互循环。
实现从"消息响应者"到"对话体验者"的核心转变。
"""

import asyncio
import time
import traceback
from typing import TYPE_CHECKING, Any, ClassVar, Optional

from src.chat.planner_actions.action_manager import ChatterActionManager
from src.chat.planner_actions.action_modifier import ActionModifier  # V6: 动作筛选器
from src.common.data_models.message_manager_data_model import StreamContext
from src.common.logger import get_logger
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest
from src.plugin_system.base.base_chatter import BaseChatter
from src.plugin_system.base.component_types import ChatType

from .action_executor import ActionExecutor
from .context_builder import KFCContextBuilder
from .models import (
    KokoroSession,
    LLMResponseModel,
    MentalLogEntry,
    MentalLogEventType,
    SessionStatus,
)
from .prompt_generator import PromptGenerator, get_prompt_generator
from .kfc_scheduler_adapter import KFCSchedulerAdapter, get_scheduler
from .session_manager import SessionManager, get_session_manager

if TYPE_CHECKING:
    from src.common.data_models.database_data_model import DatabaseMessages

logger = get_logger("kokoro_flow_chatter")

# 控制台颜色
SOFT_PURPLE = "\033[38;5;183m"
RESET_COLOR = "\033[0m"


class KokoroFlowChatter(BaseChatter):
    """
    心流聊天器 (Kokoro Flow Chatter)
    
    专为私聊场景设计的AI聊天处理器，核心特点：
    - 心理状态驱动的交互模型
    - 连续的时间观念和等待体验
    - 深度情感连接和长期关系维护
    
    状态机：
    IDLE -> RESPONDING -> WAITING -> (收到消息) -> RESPONDING
                       -> (超时) -> FOLLOW_UP_PENDING -> RESPONDING/IDLE
    """
    
    chatter_name: str = "KokoroFlowChatter"
    chatter_description: str = "心流聊天器 - 专为私聊设计的深度情感交互处理器"
    chat_types: ClassVar[list[ChatType]] = [ChatType.PRIVATE]  # 仅支持私聊
    
    def __init__(
        self,
        stream_id: str,
        action_manager: ChatterActionManager,
        plugin_config: dict | None = None,
    ):
        """
        初始化心流聊天器
        
        Args:
            stream_id: 聊天流ID
            action_manager: 动作管理器
            plugin_config: 插件配置
        """
        super().__init__(stream_id, action_manager, plugin_config)
        
        # 核心组件
        self.session_manager: SessionManager = get_session_manager()
        self.prompt_generator: PromptGenerator = get_prompt_generator()
        self.scheduler: KFCSchedulerAdapter = get_scheduler()
        self.action_executor: ActionExecutor = ActionExecutor(stream_id)
        
        # 配置
        self._load_config()
        
        # 并发控制
        self._lock = asyncio.Lock()
        
        # 统计信息
        self.stats = {
            "messages_processed": 0,
            "llm_calls": 0,
            "successful_responses": 0,
            "failed_responses": 0,
            "timeout_decisions": 0,
        }
        self.last_activity_time = time.time()
        
        # 设置调度器回调
        self._setup_scheduler_callbacks()
        
        logger.info(f"{SOFT_PURPLE}[KFC]{RESET_COLOR} 初始化完成: stream_id={stream_id}")
    
    def _load_config(self) -> None:
        """
        加载配置（从 global_config.kokoro_flow_chatter 读取）
        
        设计理念：KFC不是独立人格，它复用全局的人设、情感框架和回复模型，
        只保留最少的行为控制开关。
        """
        # 获取 KFC 配置
        if global_config and hasattr(global_config, 'kokoro_flow_chatter'):
            kfc_config = global_config.kokoro_flow_chatter
            
            # 核心行为配置
            self.max_wait_seconds_default: int = kfc_config.max_wait_seconds_default
            self.enable_continuous_thinking: bool = kfc_config.enable_continuous_thinking
            
            # 主动思考子配置（V3: 人性化驱动，无机械限制）
            proactive_cfg = kfc_config.proactive_thinking
            self.enable_proactive: bool = proactive_cfg.enabled
            self.silence_threshold_seconds: int = proactive_cfg.silence_threshold_seconds
            self.min_interval_between_proactive: int = proactive_cfg.min_interval_between_proactive
            
            logger.debug("[KFC] 已从 global_config.kokoro_flow_chatter 加载配置")
        else:
            # 回退到默认值
            self.max_wait_seconds_default = 300
            self.enable_continuous_thinking = True
            self.enable_proactive = True
            self.silence_threshold_seconds = 7200
            self.min_interval_between_proactive = 1800
            
            logger.debug("[KFC] 使用默认配置")
    
    def _setup_scheduler_callbacks(self) -> None:
        """设置调度器回调"""
        self.scheduler.set_timeout_callback(self._on_session_timeout)
        
        if self.enable_continuous_thinking:
            self.scheduler.set_continuous_thinking_callback(
                self._on_continuous_thinking
            )
    
    async def execute(self, context: StreamContext) -> dict:
        """
        执行聊天处理逻辑（BaseChatter接口实现）
        
        Args:
            context: StreamContext对象，包含聊天上下文信息
            
        Returns:
            处理结果字典
        """
        async with self._lock:
            try:
                self.last_activity_time = time.time()
                
                # 获取未读消息（提前获取用于动作筛选）
                unread_messages = context.get_unread_messages()
                
                if not unread_messages:
                    logger.debug(f"[KFC] 没有未读消息: {self.stream_id}")
                    return self._build_result(success=True, message="no_unread_messages")
                
                # 处理最后一条消息
                target_message = unread_messages[-1]
                message_content = self._extract_message_content(target_message)
                
                # V2: 加载可用动作（动态动作发现）
                await self.action_executor.load_actions()
                raw_action_count = len(self.action_executor.get_available_actions())
                logger.debug(f"[KFC] 原始加载 {raw_action_count} 个动作")
                
                # V6: 使用ActionModifier筛选动作（复用AFC的三阶段筛选逻辑）
                # 阶段0: 聊天类型过滤（私聊/群聊）
                # 阶段2: 关联类型匹配（适配器能力检查）
                # 阶段3: 激活判定（go_activate + LLM判断）
                action_modifier = ActionModifier(
                    action_manager=self.action_executor._action_manager,
                    chat_id=self.stream_id,
                )
                await action_modifier.modify_actions(message_content=message_content)
                
                # 获取筛选后的动作
                available_actions = self.action_executor._action_manager.get_using_actions()
                logger.info(
                    f"[KFC] 动作筛选: {raw_action_count} -> {len(available_actions)} "
                    f"(筛除 {raw_action_count - len(available_actions)} 个)"
                )
                
                # 执行核心处理流程（传递筛选后的动作）
                result = await self._handle_message(target_message, context, available_actions)
                
                # 更新统计
                self.stats["messages_processed"] += 1
                
                return result
                
            except asyncio.CancelledError:
                logger.info(f"[KFC] 处理被取消: {self.stream_id}")
                self.stats["failed_responses"] += 1
                raise
            except Exception as e:
                logger.error(f"[KFC] 处理出错: {e}\n{traceback.format_exc()}")
                self.stats["failed_responses"] += 1
                return self._build_result(
                    success=False,
                    message=str(e),
                    error=True
                )
    
    async def _handle_message(
        self,
        message: "DatabaseMessages",
        context: StreamContext,
        available_actions: dict | None = None,
    ) -> dict:
        """
        处理单条消息的核心逻辑
        
        实现"体验 -> 决策 -> 行动"的交互模式
        V5超融合：集成S4U所有上下文模块
        
        Args:
            message: 要处理的消息
            context: 聊天上下文
            available_actions: 可用动作字典（V2新增）
            
        Returns:
            处理结果字典
        """
        # 1. 获取或创建会话
        user_id = str(message.user_info.user_id)
        session = await self.session_manager.get_session(user_id, self.stream_id)
        
        # 2. 记录收到消息的事件
        await self._record_user_message(session, message)
        
        # 3. 更新会话状态为RESPONDING
        old_status = session.status
        session.status = SessionStatus.RESPONDING
        
        # 4. 如果之前在等待，结束等待状态
        if old_status == SessionStatus.WAITING:
            session.end_waiting()
            logger.debug(f"[KFC] 收到消息，结束等待: user={user_id}")
        
        # 5. V5超融合：构建S4U上下文数据
        chat_stream = await self._get_chat_stream()
        context_data = {}
        
        if chat_stream:
            try:
                context_builder = KFCContextBuilder(chat_stream)
                sender_name = message.user_info.user_nickname or user_id
                target_message = self._extract_message_content(message)
                
                context_data = await context_builder.build_all_context(
                    sender_name=sender_name,
                    target_message=target_message,
                    context=context,
                )
                logger.info(f"[KFC] 超融合上下文构建完成: {list(context_data.keys())}")
            except Exception as e:
                logger.warning(f"[KFC] 构建S4U上下文失败，使用基础模式: {e}")
        
        # 6. 生成提示词（V3: 从共享数据源读取历史, V5: 传递S4U上下文）
        system_prompt, user_prompt = self.prompt_generator.generate_responding_prompt(
            session=session,
            message_content=self._extract_message_content(message),
            sender_name=message.user_info.user_nickname or user_id,
            sender_id=user_id,
            message_time=message.time,
            available_actions=available_actions,
            context=context,  # V3: 传递StreamContext以读取共享历史
            context_data=context_data,  # V5: S4U上下文数据
            chat_stream=chat_stream,  # V5: 聊天流用于场景判断
        )
        
        # 7. 调用LLM
        llm_response = await self._call_llm(system_prompt, user_prompt)
        self.stats["llm_calls"] += 1
        
        # 8. 解析响应
        parsed_response = self.action_executor.parse_llm_response(llm_response)
        
        # 9. 执行动作
        execution_result = await self.action_executor.execute_actions(
            parsed_response,
            session,
            chat_stream
        )
        
        # 10. 处理执行结果
        if execution_result["has_reply"]:
            # 如果发送了回复，检查是否需要进入等待状态
            max_wait = parsed_response.max_wait_seconds
            
            if max_wait > 0:
                # 正常等待状态
                session.start_waiting(
                    expected_reaction=parsed_response.expected_user_reaction,
                    max_wait=max_wait
                )
                logger.debug(
                    f"[KFC] 进入等待状态: user={user_id}, "
                    f"max_wait={max_wait}s"
                )
            else:
                # max_wait=0 表示不等待（话题结束/用户说再见等）
                session.status = SessionStatus.IDLE
                session.end_waiting()
                logger.info(
                    f"[KFC] 话题结束，不等待用户回复: user={user_id} "
                    f"(max_wait_seconds=0)"
                )
            
            session.total_interactions += 1
            self.stats["successful_responses"] += 1
        else:
            # 没有发送回复，返回空闲状态
            session.status = SessionStatus.IDLE
            logger.debug(f"[KFC] 无回复动作，返回空闲: user={user_id}")
        
        # 11. 保存会话
        await self.session_manager.save_session(user_id)
        
        # 12. 标记消息为已读
        context.mark_message_as_read(str(message.message_id))
        
        return self._build_result(
            success=True,
            message="processed",
            has_reply=execution_result["has_reply"],
            thought=parsed_response.thought,
        )
    
    async def _record_user_message(
        self,
        session: KokoroSession,
        message: "DatabaseMessages",
    ) -> None:
        """记录用户消息到会话历史"""
        content = self._extract_message_content(message)
        session.last_user_message = content
        
        entry = MentalLogEntry(
            event_type=MentalLogEventType.USER_MESSAGE,
            timestamp=message.time or time.time(),
            thought="",  # 用户消息不需要内心独白
            content=content,
            metadata={
                "message_id": str(message.message_id),
                "user_id": str(message.user_info.user_id),
                "user_name": message.user_info.user_nickname,
            },
        )
        session.add_mental_log_entry(entry)
    
    def _extract_message_content(self, message: "DatabaseMessages") -> str:
        """提取消息内容"""
        return (
            message.processed_plain_text
            or message.display_message
            or ""
        )
    
    async def _call_llm(
        self,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """
        调用LLM生成响应
        
        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            
        Returns:
            LLM的响应文本
        """
        try:
            # 获取模型配置
            # 使用 replyer 任务的模型配置（KFC 生成回复，必须使用回复专用模型）
            if model_config is None:
                raise RuntimeError("model_config 未初始化")
            task_config = model_config.model_task_config.replyer
            
            llm_request = LLMRequest(
                model_set=task_config,
                request_type="kokoro_flow_chatter",
            )
            
            # 构建完整的提示词（将系统提示词和用户提示词合并）
            full_prompt = f"{system_prompt}\n\n{user_prompt}"
            
            # INFO日志：打印完整的KFC提示词（可观测性增强）
            logger.info(
                f"Final KFC prompt constructed for stream {self.stream_id}:\n"
                f"--- PROMPT START ---\n"
                f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}\n"
                f"--- PROMPT END ---"
            )
            
            # 生成响应
            response, _ = await llm_request.generate_response_async(
                prompt=full_prompt,
            )
            
            # INFO日志：打印原始JSON响应（可观测性增强）
            logger.info(
                f"Raw JSON response from LLM for stream {self.stream_id}:\n"
                f"--- JSON START ---\n"
                f"{response}\n"
                f"--- JSON END ---"
            )
            
            logger.info(f"[KFC] LLM响应长度: {len(response)}")
            return response
            
        except Exception as e:
            logger.error(f"[KFC] 调用LLM失败: {e}")
            # 返回一个默认的JSON响应
            return '{"thought": "出现了技术问题", "expected_user_reaction": "", "max_wait_seconds": 60, "actions": [{"type": "do_nothing"}]}'
    
    async def _get_chat_stream(self):
        """获取聊天流对象"""
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager
            
            chat_manager = get_chat_manager()
            if chat_manager:
                return await chat_manager.get_stream(self.stream_id)
        except Exception as e:
            logger.warning(f"[KFC] 获取chat_stream失败: {e}")
        return None
    
    async def _on_session_timeout(self, session: KokoroSession) -> None:
        """
        会话超时回调
        
        当等待超时时，触发后续决策流程
        
        Args:
            session: 超时的会话
        """
        logger.info(f"[KFC] 处理超时决策: user={session.user_id}")
        self.stats["timeout_decisions"] += 1
        
        try:
            # V2: 加载可用动作
            available_actions = await self.action_executor.load_actions()
            
            # 生成超时决策提示词（V2: 传递可用动作）
            system_prompt, user_prompt = self.prompt_generator.generate_timeout_decision_prompt(
                session,
                available_actions=available_actions,
            )
            
            # 调用LLM
            llm_response = await self._call_llm(system_prompt, user_prompt)
            self.stats["llm_calls"] += 1
            
            # 解析响应
            parsed_response = self.action_executor.parse_llm_response(llm_response)
            
            # 执行动作
            chat_stream = await self._get_chat_stream()
            execution_result = await self.action_executor.execute_actions(
                parsed_response,
                session,
                chat_stream
            )
            
            # 更新会话状态
            if execution_result["has_reply"]:
                # 如果发送了后续消息，重新进入等待
                session.start_waiting(
                    expected_reaction=parsed_response.expected_user_reaction,
                    max_wait=parsed_response.max_wait_seconds
                )
            else:
                # 否则返回空闲状态
                session.status = SessionStatus.IDLE
                session.end_waiting()
            
            # 保存会话
            await self.session_manager.save_session(session.user_id)
            
        except Exception as e:
            logger.error(f"[KFC] 超时决策处理失败: {e}")
            # 发生错误时返回空闲状态
            session.status = SessionStatus.IDLE
            session.end_waiting()
            await self.session_manager.save_session(session.user_id)
    
    async def _on_continuous_thinking(self, session: KokoroSession) -> None:
        """
        连续思考回调（V2升级版）
        
        在等待期间更新心理状态，可选择调用LLM生成更自然的想法
        V2: 支持通过配置启用LLM驱动的连续思考
        
        Args:
            session: 会话
        """
        logger.debug(f"[KFC] 连续思考触发: user={session.user_id}")
        
        # 检查是否启用LLM驱动的连续思考
        use_llm_thinking = self.get_config(
            "behavior.use_llm_continuous_thinking",
            default=False
        )
        
        if use_llm_thinking and isinstance(use_llm_thinking, bool) and use_llm_thinking:
            try:
                # V2: 加载可用动作
                available_actions = await self.action_executor.load_actions()
                
                # 生成连续思考提示词
                system_prompt, user_prompt = self.prompt_generator.generate_continuous_thinking_prompt(
                    session,
                    available_actions=available_actions,
                )
                
                # 调用LLM
                llm_response = await self._call_llm(system_prompt, user_prompt)
                self.stats["llm_calls"] += 1
                
                # 解析并执行（可能会更新内部状态）
                parsed_response = self.action_executor.parse_llm_response(llm_response)
                
                # 只执行内部动作，不执行外部动作
                for action in parsed_response.actions:
                    if action.type == "update_internal_state":
                        await self.action_executor._execute_internal_action(action, session)
                
                # 记录思考内容
                entry = MentalLogEntry(
                    event_type=MentalLogEventType.CONTINUOUS_THINKING,
                    timestamp=time.time(),
                    thought=parsed_response.thought,
                    content="",
                    emotional_snapshot=session.emotional_state.to_dict(),
                )
                session.add_mental_log_entry(entry)
                
                # 保存会话
                await self.session_manager.save_session(session.user_id)
                
            except Exception as e:
                logger.warning(f"[KFC] LLM连续思考失败: {e}")
        
        # 简单模式：更新焦虑程度（已在scheduler中处理）
        # 这里可以添加额外的逻辑
    
    def _build_result(
        self,
        success: bool,
        message: str = "",
        error: bool = False,
        **kwargs,
    ) -> dict:
        """构建返回结果"""
        result = {
            "success": success,
            "stream_id": self.stream_id,
            "message": message,
            "error": error,
            "timestamp": time.time(),
        }
        result.update(kwargs)
        return result
    
    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            **self.stats,
            "last_activity_time": self.last_activity_time,
            "action_executor_stats": self.action_executor.get_execution_stats(),
        }
    
    def reset_stats(self) -> None:
        """重置统计信息"""
        self.stats = {
            "messages_processed": 0,
            "llm_calls": 0,
            "successful_responses": 0,
            "failed_responses": 0,
            "timeout_decisions": 0,
        }
        self.action_executor.reset_stats()
    
    async def get_session_info(self) -> Optional[dict]:
        """获取当前会话信息（用于调试）"""
        try:
            # 尝试获取当前用户的会话
            sessions = await self.session_manager.get_all_waiting_sessions()
            for session in sessions:
                if session.stream_id == self.stream_id:
                    return session.to_dict()
        except Exception as e:
            logger.error(f"获取会话信息失败: {e}")
        return None
    
    def __str__(self) -> str:
        """字符串表示"""
        return f"KokoroFlowChatter(stream_id={self.stream_id})"
    
    def __repr__(self) -> str:
        """详细字符串表示"""
        return (
            f"KokoroFlowChatter(stream_id={self.stream_id}, "
            f"messages_processed={self.stats['messages_processed']})"
        )
