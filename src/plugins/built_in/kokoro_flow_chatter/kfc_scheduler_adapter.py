"""
Kokoro Flow Chatter 调度器适配器

基于项目统一的 UnifiedScheduler 实现 KFC 的定时任务功能。
不再自己创建后台循环，而是复用全局调度器的基础设施。

核心功能：
1. 会话等待超时检测
2. 连续思考触发
3. 与 UnifiedScheduler 的集成
"""

import time
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from src.common.logger import get_logger
from src.plugin_system.apis.unified_scheduler import (
    TriggerType,
    unified_scheduler,
)

from .models import (
    KokoroSession,
    MentalLogEntry,
    MentalLogEventType,
    SessionStatus,
)
from .session_manager import get_session_manager

if TYPE_CHECKING:
    from .chatter import KokoroFlowChatter

logger = get_logger("kokoro_scheduler_adapter")


class KFCSchedulerAdapter:
    """
    KFC 调度器适配器
    
    使用 UnifiedScheduler 实现 KFC 的定时任务功能，不再自行管理后台循环。
    
    核心功能：
    1. 定期检查处于 WAITING 状态的会话
    2. 在特定时间点触发"连续思考"
    3. 处理等待超时并触发决策
    """
    
    # 连续思考触发点（等待进度的百分比）
    CONTINUOUS_THINKING_TRIGGERS = [0.3, 0.6, 0.85]
    
    # 任务名称常量
    TASK_NAME_WAITING_CHECK = "kfc_waiting_check"
    
    def __init__(
        self,
        check_interval: float = 10.0,
        on_timeout_callback: Optional[Callable[[KokoroSession], Coroutine[Any, Any, None]]] = None,
        on_continuous_thinking_callback: Optional[Callable[[KokoroSession], Coroutine[Any, Any, None]]] = None,
    ):
        """
        初始化调度器适配器
        
        Args:
            check_interval: 检查间隔（秒）
            on_timeout_callback: 超时回调函数
            on_continuous_thinking_callback: 连续思考回调函数
        """
        self.check_interval = check_interval
        self.on_timeout_callback = on_timeout_callback
        self.on_continuous_thinking_callback = on_continuous_thinking_callback
        
        self._registered = False
        self._schedule_id: Optional[str] = None
        
        # 统计信息
        self._stats = {
            "total_checks": 0,
            "timeouts_triggered": 0,
            "continuous_thinking_triggered": 0,
            "last_check_time": 0.0,
        }
        
        logger.info("KFCSchedulerAdapter 初始化完成")
    
    async def start(self) -> None:
        """启动调度器（注册到 UnifiedScheduler）"""
        if self._registered:
            logger.warning("KFC 调度器已在运行中")
            return
        
        # 注册周期性检查任务
        self._schedule_id = await unified_scheduler.create_schedule(
            callback=self._check_waiting_sessions,
            trigger_type=TriggerType.TIME,
            trigger_config={"delay_seconds": self.check_interval},
            is_recurring=True,
            task_name=self.TASK_NAME_WAITING_CHECK,
            force_overwrite=True,
            timeout=30.0,  # 单次检查超时 30 秒
        )
        
        self._registered = True
        logger.info(f"KFC 调度器已注册到 UnifiedScheduler: schedule_id={self._schedule_id}")
    
    async def stop(self) -> None:
        """停止调度器（从 UnifiedScheduler 注销）"""
        if not self._registered:
            return
        
        try:
            if self._schedule_id:
                await unified_scheduler.remove_schedule(self._schedule_id)
                logger.info(f"KFC 调度器已从 UnifiedScheduler 注销: schedule_id={self._schedule_id}")
        except Exception as e:
            logger.error(f"停止 KFC 调度器时出错: {e}")
        finally:
            self._registered = False
            self._schedule_id = None
    
    async def _check_waiting_sessions(self) -> None:
        """检查所有等待中的会话（由 UnifiedScheduler 调用）"""
        session_manager = get_session_manager()
        waiting_sessions = await session_manager.get_all_waiting_sessions()
        
        self._stats["total_checks"] += 1
        self._stats["last_check_time"] = time.time()
        
        if not waiting_sessions:
            return
        
        for session in waiting_sessions:
            try:
                await self._process_waiting_session(session)
            except Exception as e:
                logger.error(f"处理等待会话 {session.user_id} 时出错: {e}")
    
    async def _process_waiting_session(self, session: KokoroSession) -> None:
        """
        处理单个等待中的会话
        
        Args:
            session: 等待中的会话
        """
        if session.status != SessionStatus.WAITING:
            return
        
        if session.waiting_since is None:
            return
        
        wait_duration = session.get_waiting_duration()
        max_wait = session.max_wait_seconds
        
        # max_wait_seconds = 0 表示不等待，直接返回 IDLE
        if max_wait <= 0:
            logger.info(f"会话 {session.user_id} 设置为不等待 (max_wait=0)，返回空闲状态")
            session.status = SessionStatus.IDLE
            session.end_waiting()
            session_manager = get_session_manager()
            await session_manager.save_session(session.user_id)
            return
        
        # 检查是否超时
        if session.is_wait_timeout():
            logger.info(f"会话 {session.user_id} 等待超时，触发决策")
            await self._handle_timeout(session)
            return
        
        # 检查是否需要触发连续思考
        wait_progress = wait_duration / max_wait if max_wait > 0 else 0
        
        for trigger_point in self.CONTINUOUS_THINKING_TRIGGERS:
            if self._should_trigger_continuous_thinking(session, wait_progress, trigger_point):
                logger.debug(
                    f"会话 {session.user_id} 触发连续思考 "
                    f"(进度: {wait_progress:.1%}, 触发点: {trigger_point:.1%})"
                )
                await self._handle_continuous_thinking(session, wait_progress)
                break
    
    def _should_trigger_continuous_thinking(
        self,
        session: KokoroSession,
        current_progress: float,
        trigger_point: float,
    ) -> bool:
        """
        判断是否应该触发连续思考
        """
        if current_progress < trigger_point:
            return False
        
        expected_count = sum(
            1 for tp in self.CONTINUOUS_THINKING_TRIGGERS 
            if current_progress >= tp
        )
        
        if session.continuous_thinking_count < expected_count:
            if session.last_continuous_thinking_at is None:
                return True
            
            time_since_last = time.time() - session.last_continuous_thinking_at
            return time_since_last >= 30.0
        
        return False
    
    async def _handle_timeout(self, session: KokoroSession) -> None:
        """
        处理等待超时
        
        Args:
            session: 超时的会话
        """
        self._stats["timeouts_triggered"] += 1
        
        # 更新会话状态
        session.status = SessionStatus.FOLLOW_UP_PENDING
        session.emotional_state.anxiety_level = 0.8
        
        # 添加超时日志
        timeout_entry = MentalLogEntry(
            event_type=MentalLogEventType.TIMEOUT_DECISION,
            timestamp=time.time(),
            thought=f"等了{session.max_wait_seconds}秒了，对方还是没有回复...",
            content="等待超时",
            emotional_snapshot=session.emotional_state.to_dict(),
        )
        session.add_mental_log_entry(timeout_entry)
        
        # 保存会话状态
        session_manager = get_session_manager()
        await session_manager.save_session(session.user_id)
        
        # 调用超时回调
        if self.on_timeout_callback:
            try:
                await self.on_timeout_callback(session)
            except Exception as e:
                logger.error(f"执行超时回调时出错 (user={session.user_id}): {e}")
    
    async def _handle_continuous_thinking(
        self, 
        session: KokoroSession,
        wait_progress: float,
    ) -> None:
        """
        处理连续思考
        
        Args:
            session: 会话
            wait_progress: 等待进度
        """
        self._stats["continuous_thinking_triggered"] += 1
        
        # 更新焦虑程度
        session.emotional_state.update_anxiety_over_time(
            session.get_waiting_duration(),
            session.max_wait_seconds
        )
        
        # 更新连续思考计数
        session.continuous_thinking_count += 1
        session.last_continuous_thinking_at = time.time()
        
        # 生成基于进度的内心想法
        thought = self._generate_waiting_thought(session, wait_progress)
        
        # 添加连续思考日志
        thinking_entry = MentalLogEntry(
            event_type=MentalLogEventType.CONTINUOUS_THINKING,
            timestamp=time.time(),
            thought=thought,
            content="",
            emotional_snapshot=session.emotional_state.to_dict(),
            metadata={"wait_progress": wait_progress},
        )
        session.add_mental_log_entry(thinking_entry)
        
        # 保存会话状态
        session_manager = get_session_manager()
        await session_manager.save_session(session.user_id)
        
        # 调用连续思考回调
        if self.on_continuous_thinking_callback:
            try:
                await self.on_continuous_thinking_callback(session)
            except Exception as e:
                logger.error(f"执行连续思考回调时出错 (user={session.user_id}): {e}")
    
    def _generate_waiting_thought(
        self, 
        session: KokoroSession, 
        wait_progress: float,
    ) -> str:
        """
        生成等待中的内心想法（简单版本，不调用LLM）
        """
        import random
        
        wait_seconds = session.get_waiting_duration()
        wait_minutes = wait_seconds / 60
        
        if wait_progress < 0.4:
            thoughts = [
                f"已经等了{wait_minutes:.1f}分钟了，对方可能在忙吧...",
                f"嗯...{wait_minutes:.1f}分钟过去了，不知道对方在做什么",
                "对方好像还没看到消息，再等等吧",
            ]
        elif wait_progress < 0.7:
            thoughts = [
                f"等了{wait_minutes:.1f}分钟了，有点担心对方是不是不想回了",
                f"{wait_minutes:.1f}分钟了，对方可能真的很忙？",
                "时间过得好慢啊...不知道对方什么时候会回复",
            ]
        else:
            thoughts = [
                f"已经等了{wait_minutes:.1f}分钟了，感觉有点焦虑...",
                f"快{wait_minutes:.0f}分钟了，对方是不是忘记回复了？",
                "等了这么久，要不要主动说点什么呢...",
            ]
        
        return random.choice(thoughts)
    
    def set_timeout_callback(
        self,
        callback: Callable[[KokoroSession], Coroutine[Any, Any, None]],
    ) -> None:
        """设置超时回调函数"""
        self.on_timeout_callback = callback
    
    def set_continuous_thinking_callback(
        self,
        callback: Callable[[KokoroSession], Coroutine[Any, Any, None]],
    ) -> None:
        """设置连续思考回调函数"""
        self.on_continuous_thinking_callback = callback
    
    def get_stats(self) -> dict[str, Any]:
        """获取统计信息"""
        return {
            **self._stats,
            "is_running": self._registered,
            "check_interval": self.check_interval,
        }
    
    @property
    def is_running(self) -> bool:
        """调度器是否正在运行"""
        return self._registered


# 全局适配器实例
_scheduler_adapter: Optional[KFCSchedulerAdapter] = None


def get_scheduler() -> KFCSchedulerAdapter:
    """获取全局调度器适配器实例"""
    global _scheduler_adapter
    if _scheduler_adapter is None:
        _scheduler_adapter = KFCSchedulerAdapter()
    return _scheduler_adapter


async def initialize_scheduler(
    check_interval: float = 10.0,
    on_timeout_callback: Optional[Callable[[KokoroSession], Coroutine[Any, Any, None]]] = None,
    on_continuous_thinking_callback: Optional[Callable[[KokoroSession], Coroutine[Any, Any, None]]] = None,
) -> KFCSchedulerAdapter:
    """
    初始化并启动调度器
    
    Args:
        check_interval: 检查间隔
        on_timeout_callback: 超时回调
        on_continuous_thinking_callback: 连续思考回调
        
    Returns:
        KFCSchedulerAdapter: 调度器适配器实例
    """
    global _scheduler_adapter
    _scheduler_adapter = KFCSchedulerAdapter(
        check_interval=check_interval,
        on_timeout_callback=on_timeout_callback,
        on_continuous_thinking_callback=on_continuous_thinking_callback,
    )
    await _scheduler_adapter.start()
    return _scheduler_adapter


async def shutdown_scheduler() -> None:
    """关闭调度器"""
    global _scheduler_adapter
    if _scheduler_adapter:
        await _scheduler_adapter.stop()
        _scheduler_adapter = None
