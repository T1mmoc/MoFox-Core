"""
Kokoro Flow Chatter V2 - 主动思考器

独立组件，负责：
1. 等待期间的连续思考（更新心理状态）
2. 等待超时决策（继续等 or 做点什么）
3. 长期沉默后主动发起对话

通过 UnifiedScheduler 定期触发，与 Chatter 解耦
"""

import asyncio
import random
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

from src.common.logger import get_logger
from src.config.config import global_config
from src.plugin_system.apis.unified_scheduler import TriggerType, unified_scheduler

from .action_executor import ActionExecutor
from .models import EventType, SessionStatus
from .replyer import generate_response
from .session import KokoroSession, get_session_manager

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream

logger = get_logger("kfc_v2_proactive_thinker")


class ProactiveThinker:
    """
    主动思考器
    
    独立于 Chatter，负责处理：
    1. 等待期间的连续思考
    2. 等待超时
    3. 长期沉默后主动发起
    
    核心逻辑：
    - 定期检查所有 WAITING 状态的 Session
    - 触发连续思考或超时决策
    - 定期检查长期沉默的 Session，考虑主动发起
    """
    
    # 连续思考触发点（等待进度百分比）
    THINKING_TRIGGERS = [0.3, 0.6, 0.85]
    
    # 任务名称
    TASK_WAITING_CHECK = "kfc_v2_waiting_check"
    TASK_PROACTIVE_CHECK = "kfc_v2_proactive_check"
    
    def __init__(self):
        self.session_manager = get_session_manager()
        
        # 配置
        self._load_config()
        
        # 调度任务 ID
        self._waiting_schedule_id: Optional[str] = None
        self._proactive_schedule_id: Optional[str] = None
        self._running = False
        
        # 统计
        self._stats = {
            "waiting_checks": 0,
            "continuous_thinking_triggered": 0,
            "timeout_decisions": 0,
            "proactive_triggered": 0,
        }
    
    def _load_config(self) -> None:
        """加载配置"""
        # 默认配置
        self.waiting_check_interval = 15.0  # 等待检查间隔（秒）
        self.proactive_check_interval = 300.0  # 主动思考检查间隔（秒）
        self.silence_threshold = 7200  # 沉默阈值（秒）
        self.min_proactive_interval = 1800  # 两次主动思考最小间隔（秒）
        self.quiet_hours_start = "23:00"
        self.quiet_hours_end = "07:00"
        
        # 从全局配置读取
        if global_config and hasattr(global_config, 'kokoro_flow_chatter'):
            kfc_config = global_config.kokoro_flow_chatter
            if hasattr(kfc_config, 'proactive_thinking'):
                proactive_cfg = kfc_config.proactive_thinking
                self.silence_threshold = getattr(proactive_cfg, 'silence_threshold_seconds', 7200)
                self.min_proactive_interval = getattr(proactive_cfg, 'min_interval_between_proactive', 1800)
    
    async def start(self) -> None:
        """启动主动思考器"""
        if self._running:
            logger.warning("[ProactiveThinker] 已在运行中")
            return
        
        self._running = True
        
        # 注册等待检查任务
        self._waiting_schedule_id = await unified_scheduler.create_schedule(
            callback=self._check_waiting_sessions,
            trigger_type=TriggerType.TIME,
            trigger_config={"delay_seconds": self.waiting_check_interval},
            is_recurring=True,
            task_name=self.TASK_WAITING_CHECK,
            force_overwrite=True,
            timeout=60.0,
        )
        
        # 注册主动思考检查任务
        self._proactive_schedule_id = await unified_scheduler.create_schedule(
            callback=self._check_proactive_sessions,
            trigger_type=TriggerType.TIME,
            trigger_config={"delay_seconds": self.proactive_check_interval},
            is_recurring=True,
            task_name=self.TASK_PROACTIVE_CHECK,
            force_overwrite=True,
            timeout=120.0,
        )
        
        logger.info("[ProactiveThinker] 已启动")
    
    async def stop(self) -> None:
        """停止主动思考器"""
        if not self._running:
            return
        
        self._running = False
        
        if self._waiting_schedule_id:
            await unified_scheduler.remove_schedule(self._waiting_schedule_id)
        if self._proactive_schedule_id:
            await unified_scheduler.remove_schedule(self._proactive_schedule_id)
        
        logger.info("[ProactiveThinker] 已停止")
    
    # ========================
    # 等待检查
    # ========================
    
    async def _check_waiting_sessions(self) -> None:
        """检查所有等待中的 Session"""
        self._stats["waiting_checks"] += 1
        
        sessions = await self.session_manager.get_waiting_sessions()
        if not sessions:
            return
        
        # 并行处理
        tasks = [
            asyncio.create_task(self._process_waiting_session(s))
            for s in sessions
        ]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    async def _process_waiting_session(self, session: KokoroSession) -> None:
        """处理单个等待中的 Session"""
        try:
            if session.status != SessionStatus.WAITING:
                return
            
            if not session.waiting_config.is_active():
                return
            
            # 检查是否超时
            if session.waiting_config.is_timeout():
                await self._handle_timeout(session)
                return
            
            # 检查是否需要触发连续思考
            progress = session.waiting_config.get_progress()
            if self._should_trigger_thinking(session, progress):
                await self._handle_continuous_thinking(session, progress)
                
        except Exception as e:
            logger.error(f"[ProactiveThinker] 处理等待 Session 失败 {session.user_id}: {e}")
    
    def _should_trigger_thinking(self, session: KokoroSession, progress: float) -> bool:
        """判断是否应触发连续思考"""
        # 计算应该触发的次数
        expected_count = sum(1 for t in self.THINKING_TRIGGERS if progress >= t)
        
        if session.waiting_config.thinking_count >= expected_count:
            return False
        
        # 确保两次思考之间有间隔
        if session.waiting_config.last_thinking_at > 0:
            elapsed = time.time() - session.waiting_config.last_thinking_at
            if elapsed < 30:  # 至少 30 秒间隔
                return False
        
        return True
    
    async def _handle_continuous_thinking(
        self,
        session: KokoroSession,
        progress: float,
    ) -> None:
        """处理连续思考"""
        self._stats["continuous_thinking_triggered"] += 1
        
        # 生成等待中的想法
        thought = self._generate_waiting_thought(session, progress)
        
        # 记录到 mental_log
        session.add_waiting_update(
            waiting_thought=thought,
            mood="",  # 可以根据进度设置心情
        )
        
        # 更新思考计数
        session.waiting_config.thinking_count += 1
        session.waiting_config.last_thinking_at = time.time()
        
        # 保存
        await self.session_manager.save_session(session.user_id)
        
        logger.debug(
            f"[ProactiveThinker] 连续思考: user={session.user_id}, "
            f"progress={progress:.1%}, thought={thought[:30]}..."
        )
    
    def _generate_waiting_thought(self, session: KokoroSession, progress: float) -> str:
        """生成等待中的想法"""
        elapsed_minutes = session.waiting_config.get_elapsed_minutes()
        
        if progress < 0.4:
            thoughts = [
                f"已经等了 {elapsed_minutes:.0f} 分钟了，对方可能在忙吧...",
                "不知道对方在做什么呢",
                "再等等看吧",
            ]
        elif progress < 0.7:
            thoughts = [
                f"等了 {elapsed_minutes:.0f} 分钟了，有点担心...",
                "对方是不是忘记回复了？",
                "嗯...还是没有消息",
            ]
        else:
            thoughts = [
                f"已经等了 {elapsed_minutes:.0f} 分钟了，感觉有点焦虑",
                "要不要主动说点什么呢...",
                "快到时间了，对方还是没回",
            ]
        
        return random.choice(thoughts)
    
    async def _handle_timeout(self, session: KokoroSession) -> None:
        """处理等待超时"""
        self._stats["timeout_decisions"] += 1
        
        logger.info(f"[ProactiveThinker] 等待超时: user={session.user_id}")
        
        try:
            # 获取聊天流
            chat_stream = await self._get_chat_stream(session.stream_id)
            
            # 加载动作
            action_executor = ActionExecutor(session.stream_id)
            await action_executor.load_actions()
            
            # 调用 Replyer 生成超时决策
            response = await generate_response(
                session=session,
                user_name=session.user_id,  # 这里可以改进，获取真实用户名
                situation_type="timeout",
                chat_stream=chat_stream,
                available_actions=action_executor.get_available_actions(),
            )
            
            # 执行动作
            exec_result = await action_executor.execute(response, chat_stream)
            
            # 记录到 mental_log
            session.add_bot_planning(
                thought=response.thought,
                actions=[a.to_dict() for a in response.actions],
                expected_reaction=response.expected_reaction,
                max_wait_seconds=response.max_wait_seconds,
            )
            
            # 更新状态
            if response.max_wait_seconds > 0:
                # 继续等待
                session.start_waiting(
                    expected_reaction=response.expected_reaction,
                    max_wait_seconds=response.max_wait_seconds,
                )
            else:
                # 不再等待
                session.end_waiting()
            
            # 保存
            await self.session_manager.save_session(session.user_id)
            
            logger.info(
                f"[ProactiveThinker] 超时决策完成: user={session.user_id}, "
                f"actions={[a.type for a in response.actions]}, "
                f"continue_wait={response.max_wait_seconds > 0}"
            )
            
        except Exception as e:
            logger.error(f"[ProactiveThinker] 处理超时失败: {e}")
            # 出错时结束等待
            session.end_waiting()
            await self.session_manager.save_session(session.user_id)
    
    # ========================
    # 主动思考（长期沉默）
    # ========================
    
    async def _check_proactive_sessions(self) -> None:
        """检查是否有需要主动发起对话的 Session"""
        # 检查是否在勿扰时段
        if self._is_quiet_hours():
            return
        
        sessions = await self.session_manager.get_all_sessions()
        current_time = time.time()
        
        for session in sessions:
            try:
                trigger_reason = self._should_trigger_proactive(session, current_time)
                if trigger_reason:
                    await self._handle_proactive(session, trigger_reason)
            except Exception as e:
                logger.error(f"[ProactiveThinker] 检查主动思考失败 {session.user_id}: {e}")
    
    def _is_quiet_hours(self) -> bool:
        """检查是否在勿扰时段"""
        try:
            now = datetime.now()
            current_minutes = now.hour * 60 + now.minute
            
            start_parts = self.quiet_hours_start.split(":")
            start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
            
            end_parts = self.quiet_hours_end.split(":")
            end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])
            
            if start_minutes <= end_minutes:
                return start_minutes <= current_minutes < end_minutes
            else:
                return current_minutes >= start_minutes or current_minutes < end_minutes
        except:
            return False
    
    def _should_trigger_proactive(
        self,
        session: KokoroSession,
        current_time: float,
    ) -> Optional[str]:
        """判断是否应触发主动思考"""
        # 只检查 IDLE 状态的 Session
        if session.status != SessionStatus.IDLE:
            return None
        
        # 检查沉默时长
        silence_duration = current_time - session.last_activity_at
        if silence_duration < self.silence_threshold:
            return None
        
        # 检查距离上次主动思考的间隔
        if session.last_proactive_at:
            time_since_last = current_time - session.last_proactive_at
            if time_since_last < self.min_proactive_interval:
                return None
        
        # 概率触发（避免每次检查都触发）
        if random.random() > 0.3:  # 30% 概率
            return None
        
        silence_hours = silence_duration / 3600
        return f"沉默了 {silence_hours:.1f} 小时"
    
    async def _handle_proactive(
        self,
        session: KokoroSession,
        trigger_reason: str,
    ) -> None:
        """处理主动思考"""
        self._stats["proactive_triggered"] += 1
        
        logger.info(f"[ProactiveThinker] 主动思考触发: user={session.user_id}, reason={trigger_reason}")
        
        try:
            # 获取聊天流
            chat_stream = await self._get_chat_stream(session.stream_id)
            
            # 加载动作
            action_executor = ActionExecutor(session.stream_id)
            await action_executor.load_actions()
            
            # 计算沉默时长
            silence_seconds = time.time() - session.last_activity_at
            if silence_seconds < 3600:
                silence_duration = f"{silence_seconds / 60:.0f} 分钟"
            else:
                silence_duration = f"{silence_seconds / 3600:.1f} 小时"
            
            # 调用 Replyer
            response = await generate_response(
                session=session,
                user_name=session.user_id,
                situation_type="proactive",
                chat_stream=chat_stream,
                available_actions=action_executor.get_available_actions(),
                extra_context={
                    "trigger_reason": trigger_reason,
                    "silence_duration": silence_duration,
                },
            )
            
            # 检查是否决定不打扰
            is_do_nothing = (
                len(response.actions) == 0 or
                (len(response.actions) == 1 and response.actions[0].type == "do_nothing")
            )
            
            if is_do_nothing:
                logger.info(f"[ProactiveThinker] 决定不打扰: user={session.user_id}")
                session.last_proactive_at = time.time()
                await self.session_manager.save_session(session.user_id)
                return
            
            # 执行动作
            exec_result = await action_executor.execute(response, chat_stream)
            
            # 记录到 mental_log
            session.add_bot_planning(
                thought=response.thought,
                actions=[a.to_dict() for a in response.actions],
                expected_reaction=response.expected_reaction,
                max_wait_seconds=response.max_wait_seconds,
            )
            
            # 更新状态
            session.last_proactive_at = time.time()
            if response.max_wait_seconds > 0:
                session.start_waiting(
                    expected_reaction=response.expected_reaction,
                    max_wait_seconds=response.max_wait_seconds,
                )
            
            # 保存
            await self.session_manager.save_session(session.user_id)
            
            logger.info(
                f"[ProactiveThinker] 主动发起完成: user={session.user_id}, "
                f"actions={[a.type for a in response.actions]}"
            )
            
        except Exception as e:
            logger.error(f"[ProactiveThinker] 主动思考失败: {e}")
    
    async def _get_chat_stream(self, stream_id: str):
        """获取聊天流"""
        try:
            from src.chat.message_receive.chat_stream import get_chat_manager
            
            chat_manager = get_chat_manager()
            if chat_manager:
                return await chat_manager.get_stream(stream_id)
        except Exception as e:
            logger.warning(f"[ProactiveThinker] 获取 chat_stream 失败: {e}")
        return None
    
    def get_stats(self) -> dict:
        """获取统计信息"""
        return {
            **self._stats,
            "is_running": self._running,
        }


# 全局单例
_proactive_thinker: Optional[ProactiveThinker] = None


def get_proactive_thinker() -> ProactiveThinker:
    """获取全局主动思考器"""
    global _proactive_thinker
    if _proactive_thinker is None:
        _proactive_thinker = ProactiveThinker()
    return _proactive_thinker


async def start_proactive_thinker() -> ProactiveThinker:
    """启动主动思考器"""
    thinker = get_proactive_thinker()
    await thinker.start()
    return thinker


async def stop_proactive_thinker() -> None:
    """停止主动思考器"""
    global _proactive_thinker
    if _proactive_thinker:
        await _proactive_thinker.stop()
