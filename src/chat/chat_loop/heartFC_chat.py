import asyncio
import time
import traceback
import random
from typing import List, Optional, Dict, Any, Tuple
from rich.traceback import install
from collections import deque

from src.config.config import global_config
from src.common.logger import get_logger
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.chat.utils.prompt_builder import global_prompt_manager
from src.chat.utils.timer_calculator import Timer
from src.chat.planner_actions.planner import ActionPlanner
from src.chat.planner_actions.action_modifier import ActionModifier
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.chat_loop.hfc_utils import CycleDetail
from src.person_info.relationship_builder_manager import relationship_builder_manager
from src.chat.express.expression_learner import expression_learner_manager
from src.person_info.person_info import Person
from src.person_info.group_relationship_manager import get_group_relationship_manager
from src.plugin_system.base.component_types import ChatMode, EventType
from src.plugin_system.core import events_manager
from src.plugin_system.apis import generator_api, send_api, message_api, database_api
from src.mais4u.mai_think import mai_thinking_manager
import math
from src.mais4u.s4u_config import s4u_config
# no_reply逻辑已集成到heartFC_chat.py中，不再需要导入
from src.chat.chat_loop.hfc_utils import send_typing, stop_typing

ERROR_LOOP_INFO = {
    "loop_plan_info": {
        "action_result": {
            "action_type": "error",
            "action_data": {},
            "reasoning": "循环处理失败",
        },
    },
    "loop_action_info": {
        "action_taken": False,
        "reply_text": "",
        "command": "",
        "taken_time": time.time(),
    },
}

NO_ACTION = {
    "action_result": {
        "action_type": "no_action",
        "action_data": {},
        "reasoning": "规划器初始化默认",
        "is_parallel": True,
    },
    "chat_context": "",
    "action_prompt": "",
}

install(extra_lines=3)

# 注释：原来的动作修改超时常量已移除，因为改为顺序执行

logger = get_logger("hfc")  # Logger Name Changed


class HeartFChatting:
    """
    管理一个连续的Focus Chat循环
    用于在特定聊天流中生成回复。
    其生命周期现在由其关联的 SubHeartflow 的 FOCUSED 状态控制。
    """

    def __init__(
        self,
        chat_id: str,
    ):
        """
        HeartFChatting 初始化函数

        参数:
            chat_id: 聊天流唯一标识符(如stream_id)
            on_stop_focus_chat: 当收到stop_focus_chat命令时调用的回调函数
            performance_version: 性能记录版本号，用于区分不同启动版本
        """
        # 基础属性
        self.stream_id: str = chat_id  # 聊天流ID
        self.chat_stream: ChatStream = get_chat_manager().get_stream(self.stream_id)  # type: ignore
        if not self.chat_stream:
            raise ValueError(f"无法找到聊天流: {self.stream_id}")
        self.log_prefix = f"[{get_chat_manager().get_stream_name(self.stream_id) or self.stream_id}]"

        self.relationship_builder = relationship_builder_manager.get_or_create_builder(self.stream_id)
        self.expression_learner = expression_learner_manager.get_expression_learner(self.stream_id)
        self.group_relationship_manager = get_group_relationship_manager()
        

        self.action_manager = ActionManager()
        self.action_planner = ActionPlanner(chat_id=self.stream_id, action_manager=self.action_manager)
        self.action_modifier = ActionModifier(action_manager=self.action_manager, chat_id=self.stream_id)

        # 循环控制内部状态
        self.running: bool = False
        self._loop_task: Optional[asyncio.Task] = None  # 主循环任务

        # 添加循环信息管理相关的属性
        self.history_loop: List[CycleDetail] = []
        self._cycle_counter = 0
        self._current_cycle_detail: CycleDetail = None  # type: ignore

        self.reply_timeout_count = 0
        self.plan_timeout_count = 0

        self.last_read_time = time.time() - 1
        
        # 根据配置初始化聊天模式和能量值
        is_group_chat = self.chat_stream.group_info is not None
        if is_group_chat and global_config.chat.group_chat_mode != "auto":
            if global_config.chat.group_chat_mode == "focus":
                self.loop_mode = ChatMode.FOCUS
                self.energy_value = 35
                logger.info(f"{self.log_prefix} 群聊强制专注模式已启用，能量值设置为35")
            elif global_config.chat.group_chat_mode == "normal":
                self.loop_mode = ChatMode.NORMAL
                self.energy_value = 15
                logger.info(f"{self.log_prefix} 群聊强制普通模式已启用，能量值设置为15")
        
        self.focus_energy = 1
        self.no_reply_consecutive = 0
        
        # 能量值日志时间控制
        self.last_energy_log_time = 0  # 上次记录能量值日志的时间
        self.energy_log_interval = 90  # 能量值日志间隔（秒）

        # 主动思考功能相关属性
        self.last_message_time = time.time()  # 最后一条消息的时间
        self._proactive_thinking_task: Optional[asyncio.Task] = None  # 主动思考任务

    async def start(self):
        """检查是否需要启动主循环，如果未激活则启动。"""

        # 如果循环已经激活，直接返回
        if self.running:
            logger.debug(f"{self.log_prefix} HeartFChatting 已激活，无需重复启动")
            return

        try:
            # 标记为活动状态，防止重复启动
            self.running = True

            self._energy_task = asyncio.create_task(self._energy_loop())
            self._energy_task.add_done_callback(self._handle_energy_completion)

            # 启动主动思考任务（仅在群聊且启用的情况下）
            if (global_config.chat.enable_proactive_thinking and 
                self.chat_stream.group_info is not None):
                self._proactive_thinking_task = asyncio.create_task(self._proactive_thinking_loop())
                self._proactive_thinking_task.add_done_callback(self._handle_proactive_thinking_completion)

            self._loop_task = asyncio.create_task(self._main_chat_loop())
            self._loop_task.add_done_callback(self._handle_loop_completion)
            logger.info(f"{self.log_prefix} HeartFChatting 启动完成")

        except Exception as e:
            # 启动失败时重置状态
            self.running = False
            self._loop_task = None
            logger.error(f"{self.log_prefix} HeartFChatting 启动失败: {e}")
            raise

    def _handle_loop_completion(self, task: asyncio.Task):
        """当 _hfc_loop 任务完成时执行的回调。"""
        try:
            if exception := task.exception():
                logger.error(f"{self.log_prefix} HeartFChatting: 脱离了聊天(异常): {exception}")
                logger.error(traceback.format_exc())  # Log full traceback for exceptions
            else:
                logger.info(f"{self.log_prefix} HeartFChatting: 脱离了聊天 (外部停止)")
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} HeartFChatting: 结束了聊天")

    def start_cycle(self):
        self._cycle_counter += 1
        self._current_cycle_detail = CycleDetail(self._cycle_counter)
        self._current_cycle_detail.thinking_id = f"tid{str(round(time.time(), 2))}"
        cycle_timers = {}
        return cycle_timers, self._current_cycle_detail.thinking_id

    def end_cycle(self, loop_info, cycle_timers):
        self._current_cycle_detail.set_loop_info(loop_info)
        self.history_loop.append(self._current_cycle_detail)
        self._current_cycle_detail.timers = cycle_timers
        self._current_cycle_detail.end_time = time.time()

    def _handle_energy_completion(self, task: asyncio.Task):
        """当 energy_loop 任务完成时执行的回调。"""
        try:
            if exception := task.exception():
                logger.error(f"{self.log_prefix} 能量循环异常: {exception}")
            else:
                logger.info(f"{self.log_prefix} 能量循环正常结束")
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} 能量循环被取消")

    def _handle_proactive_thinking_completion(self, task: asyncio.Task):
        """当 proactive_thinking_loop 任务完成时执行的回调。"""
        try:
            if exception := task.exception():
                logger.error(f"{self.log_prefix} 主动思考循环异常: {exception}")
            else:
                logger.info(f"{self.log_prefix} 主动思考循环正常结束")
        except asyncio.CancelledError:
            logger.info(f"{self.log_prefix} 主动思考循环被取消")
        """处理能量循环任务的完成"""
        if task.cancelled():
            logger.info(f"{self.log_prefix} 能量循环任务被取消")
        elif task.exception():
            logger.error(f"{self.log_prefix} 能量循环任务发生异常: {task.exception()}")

    def _should_log_energy(self) -> bool:
        """判断是否应该记录能量值日志（基于时间间隔控制）"""
        current_time = time.time()
        if current_time - self.last_energy_log_time >= self.energy_log_interval:
            self.last_energy_log_time = current_time
            return True
        return False

    def _log_energy_change(self, action: str, reason: str = ""):
        """记录能量值变化日志（受时间间隔控制）"""
        if self._should_log_energy():
            if reason:
                logger.info(f"{self.log_prefix} {action}，{reason}，当前能量值：{self.energy_value:.1f}")
            else:
                logger.info(f"{self.log_prefix} {action}，当前能量值：{self.energy_value:.1f}")
        else:
            # 仍然以debug级别记录，便于调试
            if reason:
                logger.debug(f"{self.log_prefix} {action}，{reason}，当前能量值：{self.energy_value:.1f}")
            else:
                logger.debug(f"{self.log_prefix} {action}，当前能量值：{self.energy_value:.1f}")

    async def _energy_loop(self):
        while self.running:
            await asyncio.sleep(10)
            
            # 检查是否为群聊且配置了强制模式
            is_group_chat = self.chat_stream.group_info is not None
            if is_group_chat and global_config.chat.group_chat_mode != "auto":
                # 强制模式下固定能量值和聊天模式
                if global_config.chat.group_chat_mode == "focus":
                    self.loop_mode = ChatMode.FOCUS
                    self.energy_value = 35  # 强制设置为35
                elif global_config.chat.group_chat_mode == "normal":
                    self.loop_mode = ChatMode.NORMAL
                    self.energy_value = 15  # 强制设置为15
                continue  # 跳过正常的能量值衰减逻辑
            
            # 原有的自动模式逻辑
            if self.loop_mode == ChatMode.NORMAL:
                self.energy_value -= 0.3
                self.energy_value = max(self.energy_value, 0.3)
            if self.loop_mode == ChatMode.FOCUS:
                self.energy_value -= 0.6
                self.energy_value = max(self.energy_value, 0.3)

    async def _proactive_thinking_loop(self):
        """主动思考循环，仅在focus模式下生效"""
        while self.running:
            await asyncio.sleep(30)  # 每30秒检查一次
            
            # 只在focus模式下进行主动思考
            if self.loop_mode != ChatMode.FOCUS:
                continue
                
            current_time = time.time()
            silence_duration = current_time - self.last_message_time
            
            # 检查是否达到主动思考的时间间隔
            if silence_duration >= global_config.chat.proactive_thinking_interval:
                try:
                    await self._execute_proactive_thinking(silence_duration)
                    # 重置计时器，避免频繁触发
                    self.last_message_time = current_time
                except Exception as e:
                    logger.error(f"{self.log_prefix} 主动思考执行出错: {e}")
                    logger.error(traceback.format_exc())

    def _format_duration(self, seconds: float) -> str:
        """格式化时间间隔为易读格式"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = int(seconds % 60)
        
        parts = []
        if hours > 0:
            parts.append(f"{hours}小时")
        if minutes > 0:
            parts.append(f"{minutes}分")
        if secs > 0 or not parts:  # 如果没有小时和分钟，显示秒
            parts.append(f"{secs}秒")
            
        return "".join(parts)

    async def _execute_proactive_thinking(self, silence_duration: float):
        """执行主动思考"""
        formatted_time = self._format_duration(silence_duration)
        logger.info(f"{self.log_prefix} 触发主动思考，已沉默{formatted_time}")
        
        try:
            # 构建主动思考的prompt
            proactive_prompt = global_config.chat.proactive_thinking_prompt_template.format(
                time=formatted_time
            )
            
            # 创建一个虚拟的消息数据用于主动思考
            """
            因为主动思考是在没有用户消息的情况下触发的
            但规划器仍然需要一个"消息"作为输入来工作
            所以需要"伪造"一个消息来触发思考流程，本质上是系统与自己的对话，让AI能够主动思考和决策。
            """
            thinking_message = {
                "processed_plain_text": proactive_prompt,
                "user_id": "system_proactive_thinking",
                "user_platform": "system",
                "timestamp": time.time(),
                "message_type": "proactive_thinking",
                "user_nickname": "系统主动思考",
                "chat_info_platform": "system",
                "message_id": f"proactive_{int(time.time())}"
            }
            
            # 使用现有的_observe方法来处理主动思考
            # 这样可以复用现有的完整思考流程
            logger.info(f"{self.log_prefix} 开始主动思考...")
            await self._observe(message_data=thinking_message)
            logger.info(f"{self.log_prefix} 主动思考完成")
                
        except Exception as e:
            logger.error(f"{self.log_prefix} 主动思考执行异常: {e}")
            logger.error(traceback.format_exc())

    def print_cycle_info(self, cycle_timers):
        # 记录循环信息和计时器结果
        timer_strings = []
        for name, elapsed in cycle_timers.items():
            formatted_time = f"{elapsed * 1000:.2f}毫秒" if elapsed < 1 else f"{elapsed:.2f}秒"
            timer_strings.append(f"{name}: {formatted_time}")

        # 获取动作类型，兼容新旧格式
        action_type = "未知动作"
        if hasattr(self, '_current_cycle_detail') and self._current_cycle_detail:
            loop_plan_info = self._current_cycle_detail.loop_plan_info
            if isinstance(loop_plan_info, dict):
                action_result = loop_plan_info.get('action_result', {})
                if isinstance(action_result, dict):
                    # 旧格式：action_result是字典
                    action_type = action_result.get('action_type', '未知动作')
                elif isinstance(action_result, list) and action_result:
                    # 新格式：action_result是actions列表
                    action_type = action_result[0].get('action_type', '未知动作')
            elif isinstance(loop_plan_info, list) and loop_plan_info:
                # 直接是actions列表的情况
                action_type = loop_plan_info[0].get('action_type', '未知动作')

        logger.info(
            f"{self.log_prefix} 第{self._current_cycle_detail.cycle_id}次思考,"
            f"耗时: {self._current_cycle_detail.end_time - self._current_cycle_detail.start_time:.1f}秒, "  # type: ignore
            f"选择动作: {action_type}"
            + (f"\n详情: {'; '.join(timer_strings)}" if timer_strings else "")
        )
        
    def _determine_form_type(self) -> None:
        """判断使用哪种形式的no_reply"""
        # 如果连续no_reply次数少于3次，使用waiting形式
        if self.no_reply_consecutive <= 3:
            self.focus_energy = 1
        else:
            # 计算最近三次记录的兴趣度总和
            total_recent_interest = sum(self.recent_interest_records)
    
            # 计算调整后的阈值
            adjusted_threshold = 1 / global_config.chat.get_current_talk_frequency(self.stream_id)
            
            logger.info(f"{self.log_prefix} 最近三次兴趣度总和: {total_recent_interest:.2f}, 调整后阈值: {adjusted_threshold:.2f}")
        
            # 如果兴趣度总和小于阈值，进入breaking形式
            if total_recent_interest < adjusted_threshold:
                logger.info(f"{self.log_prefix} 兴趣度不足，进入休息")
                self.focus_energy = random.randint(3, 6)
            else:
                logger.info(f"{self.log_prefix} 兴趣度充足，等待新消息")
                self.focus_energy = 1      
            
    async def _should_process_messages(self, new_message: List[Dict[str, Any]]) -> tuple[bool,float]:
        """
        判断是否应该处理消息
        
        Args:
            new_message: 新消息列表
            mode: 当前聊天模式
            
        Returns:
            bool: 是否应该处理消息
        """
        new_message_count = len(new_message)
        talk_frequency = global_config.chat.get_current_talk_frequency(self.stream_id)
        
        modified_exit_count_threshold = self.focus_energy * 0.5 / talk_frequency
        modified_exit_interest_threshold = 1.5 / talk_frequency
        total_interest = 0.0
        for msg_dict in new_message:
            interest_value = msg_dict.get("interest_value")
            if interest_value is not None and msg_dict.get("processed_plain_text", ""):
                total_interest += float(interest_value)
        
        if new_message_count >= modified_exit_count_threshold:
            # 记录兴趣度到列表
            total_interest = 0.0
            for msg_dict in new_message:
                interest_value = msg_dict.get("interest_value", 0.0)
                # 确保 interest_value 不为 None
                if interest_value is None:
                    interest_value = 0.0
                if msg_dict.get("processed_plain_text", ""):
                    total_interest += interest_value
            
            NoReplyAction._recent_interest_records.append(total_interest)
            
            logger.info(
                f"{self.log_prefix} 累计消息数量达到{new_message_count}条(>{modified_exit_count_threshold:.1f})，结束等待"
            )
            # logger.info(self.last_read_time)
            # logger.info(new_message)
            return True, total_interest / new_message_count if new_message_count > 0 else 0.0

        # 检查累计兴趣值
        if new_message_count > 0:
            accumulated_interest = 0.0
            for msg_dict in new_message:
                text = msg_dict.get("processed_plain_text", "")
                interest_value = msg_dict.get("interest_value", 0.0)
                # 确保 interest_value 不为 None
                if interest_value is None:
                    interest_value = 0.0
                if text:
                    accumulated_interest += interest_value
            
            # 只在兴趣值变化时输出log
            if not hasattr(self, "_last_accumulated_interest") or total_interest != self._last_accumulated_interest:
                logger.info(f"{self.log_prefix} 休息中，新消息：{new_message_count}条，累计兴趣值: {total_interest:.2f}, 活跃度: {talk_frequency:.1f}")
                self._last_accumulated_interest = total_interest
            
            if total_interest >= modified_exit_interest_threshold:
                # 记录兴趣度到列表
                self.recent_interest_records.append(total_interest)
                logger.info(
                    f"{self.log_prefix} 累计兴趣值达到{total_interest:.2f}(>{modified_exit_interest_threshold:.1f})，结束等待"
                )
                return True, total_interest / new_message_count if new_message_count > 0 else 0.0

        # 每10秒输出一次等待状态，仅在debug模式下启用
        if int(time.time() - self.last_read_time) > 0 and int(time.time() - self.last_read_time) % 10 == 0:
            logger.debug(
                f"{self.log_prefix} 已等待{time.time() - self.last_read_time:.0f}秒，累计{new_message_count}条消息，继续等待..."
            )
            await asyncio.sleep(0.5)
        
        return False,0.0


    async def _loopbody(self):
        recent_messages_dict = message_api.get_messages_by_time_in_chat(
            chat_id=self.stream_id,
            start_time=self.last_read_time,
            end_time=time.time(),
            limit = 10,
            limit_mode="latest",
            filter_mai=True,
            filter_command=True,
        )   
        
        # 如果有新消息，更新最后消息时间（用于主动思考计时）
        if new_message_count > 0:
            current_time = time.time()
            self.last_message_time = current_time
        
        
        if self.loop_mode == ChatMode.FOCUS:
            
            if self.last_action == "no_reply":
                if not await self._execute_no_reply(recent_messages_dict):
                    # 在强制模式下，能量值不会随时间减少
                    is_group_chat = self.chat_stream.group_info is not None
                    if not (is_group_chat and global_config.chat.group_chat_mode != "auto"):
                        self.energy_value -= 0.3 / global_config.chat.focus_value
                        self._log_energy_change("能量值减少")
                    await asyncio.sleep(0.5)
                    return True
            
            self.last_read_time = time.time()
            
            if await self._observe():
                # 在强制模式下，能量值不会因观察而增加
                is_group_chat = self.chat_stream.group_info is not None
                if not (is_group_chat and global_config.chat.group_chat_mode != "auto"):
                    self.energy_value += 1 / global_config.chat.focus_value
                    self._log_energy_change("能量值增加")

            # 检查是否应该退出专注模式
            # 如果开启了强制私聊专注模式且当前为私聊，则不允许退出专注状态
            is_private_chat = self.chat_stream.group_info is None
            is_group_chat = self.chat_stream.group_info is not None
            
            if global_config.chat.force_focus_private and is_private_chat:
                # 强制私聊专注模式下，保持专注状态，但重置能量值防止过低
                if self.energy_value <= 1:
                    self.energy_value = 5  # 重置为较低但足够的能量值
                return True
            
            # 群聊强制专注模式下，不允许退出专注状态
            if is_group_chat and global_config.chat.group_chat_mode == "focus":
                return True
            
            if self.energy_value <= 1:
                self.energy_value = 1
                self.loop_mode = ChatMode.NORMAL
                return True

            return True
        elif self.loop_mode == ChatMode.NORMAL:
            # 检查是否应该强制进入专注模式（私聊且开启强制专注）
            is_private_chat = self.chat_stream.group_info is None
            is_group_chat = self.chat_stream.group_info is not None
            
            if global_config.chat.force_focus_private and is_private_chat:
                self.loop_mode = ChatMode.FOCUS
                self.energy_value = 10  # 设置初始能量值
                return True
            
            # 群聊强制普通模式下，不允许进入专注状态
            if is_group_chat and global_config.chat.group_chat_mode == "normal":
                # 在强制普通模式下，即使满足条件也不进入专注模式
                pass
            elif global_config.chat.focus_value != 0:
                if new_message_count > 3 / pow(global_config.chat.focus_value, 0.5):
                    self.loop_mode = ChatMode.FOCUS
                    self.energy_value = (
                        10 + (new_message_count / (3 / pow(global_config.chat.focus_value, 0.5))) * 10
                    )
                    return True

                if self.energy_value >= 30:
                    self.loop_mode = ChatMode.FOCUS
                    return True

            if new_message_count >= self.focus_energy:
                earliest_messages_data = recent_messages_dict[0]
                self.last_read_time = earliest_messages_data.get("time")

                if_think = await self.normal_response(earliest_messages_data)
                
                # 在强制模式下，能量值变化逻辑需要特殊处理
                is_group_chat = self.chat_stream.group_info is not None
                if is_group_chat and global_config.chat.group_chat_mode != "auto":
                    # 强制模式下不改变能量值
                    pass
                elif if_think:
                    factor = max(global_config.chat.focus_value, 0.1)
                    self.energy_value *= 1.1 * factor
                    self._log_energy_change("进行了思考，能量值按倍数增加")
                else:
                    self.energy_value += 0.1 * global_config.chat.focus_value
                    self._log_energy_change("没有进行思考，能量值线性增加")

                # 这个可以保持debug级别，因为它是总结性信息
                logger.debug(f"{self.log_prefix} 当前能量值：{self.energy_value:.1f}")
                return True

        else:
            # Normal模式：消息数量不足，等待
            await asyncio.sleep(0.5)
            return True
        return True

    async def _send_and_store_reply(
        self,
        response_set,
        action_message,
        cycle_timers: Dict[str, float],
        thinking_id,
        actions,
        selected_expressions:List[int] = None,
    ) -> Tuple[Dict[str, Any], str, Dict[str, float]]:
        
        with Timer("回复发送", cycle_timers):
            reply_text = await self._send_response(
                reply_set=response_set,
                message_data=action_message,
                selected_expressions=selected_expressions,
            )
        
        # 获取 platform，如果不存在则从 chat_stream 获取，如果还是 None 则使用默认值
        platform = action_message.get("chat_info_platform")
        if platform is None:
            platform = getattr(self.chat_stream, "platform", "unknown")
        
        person = Person(platform = platform ,user_id = action_message.get("user_id", ""))
        person_name = person.person_name
        action_prompt_display = f"你对{person_name}进行了回复：{reply_text}"

        await database_api.store_action_info(
            chat_stream=self.chat_stream,
            action_build_into_prompt=False,
            action_prompt_display=action_prompt_display,
            action_done=True,
            thinking_id=thinking_id,
            action_data={"reply_text": reply_text},
            action_name="reply",
        )

        # 构建循环信息
        loop_info: Dict[str, Any] = {
            "loop_plan_info": {
                "action_result": actions,
            },
            "loop_action_info": {
                "action_taken": True,
                "reply_text": reply_text,
                "command": "",
                "taken_time": time.time(),
            },
        }

        return loop_info, reply_text, cycle_timers

    async def _observe(self,interest_value:float = 0.0) -> bool:

        action_type = "no_action"
        reply_text = ""  # 初始化reply_text变量，避免UnboundLocalError

        
        # 使用sigmoid函数将interest_value转换为概率
        # 当interest_value为0时，概率接近0（使用Focus模式）
        # 当interest_value很高时，概率接近1（使用Normal模式）
        def calculate_normal_mode_probability(interest_val: float) -> float:
            # 使用sigmoid函数，调整参数使概率分布更合理
            # 当interest_value = 0时，概率约为0.1
            # 当interest_value = 1时，概率约为0.5
            # 当interest_value = 2时，概率约为0.8
            # 当interest_value = 3时，概率约为0.95
            k = 2.0  # 控制曲线陡峭程度
            x0 = 1.0  # 控制曲线中心点
            return 1.0 / (1.0 + math.exp(-k * (interest_val - x0)))
        
        normal_mode_probability = calculate_normal_mode_probability(interest_value) * 0.5 / global_config.chat.get_current_talk_frequency(self.stream_id)
        
        # 根据概率决定使用哪种模式
        if random.random() < normal_mode_probability:
            mode = ChatMode.NORMAL
            logger.info(f"{self.log_prefix} 有兴趣({interest_value:.2f})，在{normal_mode_probability*100:.0f}%概率下选择回复")
        else:
            mode = ChatMode.FOCUS

        # 创建新的循环信息
        cycle_timers, thinking_id = self.start_cycle()

        logger.info(f"{self.log_prefix} 开始第{self._cycle_counter}次思考")

        if s4u_config.enable_s4u:
            await send_typing()

        async with global_prompt_manager.async_message_scope(self.chat_stream.context.get_template_name()):
            await self.relationship_builder.build_relation()
            await self.expression_learner.trigger_learning_for_chat()

            # 群印象构建：仅在群聊中触发
            # if self.chat_stream.group_info and getattr(self.chat_stream.group_info, "group_id", None):
            #     await self.group_relationship_manager.build_relation(
            #         chat_id=self.stream_id,
            #         platform=self.chat_stream.platform
            #     )


            if random.random() > global_config.chat.focus_value and mode == ChatMode.FOCUS:
                #如果激活度没有激活，并且聊天活跃度低，有可能不进行plan，相当于不在电脑前，不进行认真思考
                actions = [
                    {
                        "action_type": "no_reply",
                        "reasoning": "选择不回复",
                        "action_data": {},
                    }
                ]
            else:
                available_actions = {}
                # 第一步：动作修改
                with Timer("动作修改", cycle_timers):
                    try:
                        await self.action_modifier.modify_actions()
                        available_actions = self.action_manager.get_using_actions()
                    except Exception as e:
                        logger.error(f"{self.log_prefix} 动作修改失败: {e}")

                # 执行planner
                planner_info = self.action_planner.get_necessary_info()
                prompt_info = await self.action_planner.build_planner_prompt(
                    is_group_chat=planner_info[0],
                    chat_target_info=planner_info[1],
                    current_available_actions=planner_info[2],
                )
                if not await events_manager.handle_mai_events(
                    EventType.ON_PLAN, None, prompt_info[0], None, self.chat_stream.stream_id
                ):
                    return False
                with Timer("规划器", cycle_timers):
                    actions, _= await self.action_planner.plan(
                        mode=mode,
                        loop_start_time=self.last_read_time,
                        available_actions=available_actions,
                    )


            
            # 3. 并行执行所有动作
            async def execute_action(action_info,actions):
                """执行单个动作的通用函数"""
                try:
                    if action_info["action_type"] == "no_reply":
                        # 直接处理no_reply逻辑，不再通过动作系统
                        reason = action_info.get("reasoning", "选择不回复")
                        logger.info(f"{self.log_prefix} 选择不回复，原因: {reason}")
                        
                        # 存储no_reply信息到数据库
                        await database_api.store_action_info(
                            chat_stream=self.chat_stream,
                            action_build_into_prompt=False,
                            action_prompt_display=reason,
                            action_done=True,
                            thinking_id=thinking_id,
                            action_data={"reason": reason},
                            action_name="no_reply",
                        )
                        
                        return {
                            "action_type": "no_reply",
                            "success": True,
                            "reply_text": "",
                            "command": ""
                        }
                    elif action_info["action_type"] != "reply":
                        # 执行普通动作
                        with Timer("动作执行", cycle_timers):
                            success, reply_text, command = await self._handle_action(
                                action_info["action_type"],
                                action_info["reasoning"],
                                action_info["action_data"],
                                cycle_timers,
                                thinking_id,
                                action_info["action_message"]
                            )
                        return {
                            "action_type": action_info["action_type"],
                            "success": success,
                            "reply_text": reply_text,
                            "command": command
                        }
                    else:
                        
                        try:
                            success, response_set, prompt_selected_expressions = await generator_api.generate_reply(
                                chat_stream=self.chat_stream,
                                reply_message = action_info["action_message"],
                                available_actions=available_actions,
                                choosen_actions=actions,
                                reply_reason=action_info.get("reasoning", ""),
                                enable_tool=global_config.tool.enable_tool,
                                request_type="replyer",
                                from_plugin=False,
                                return_expressions=True,
                            )
                            
                            if prompt_selected_expressions and len(prompt_selected_expressions) > 1:
                                _,selected_expressions = prompt_selected_expressions
                            else:
                                selected_expressions = []

                            if not success or not response_set:
                                logger.info(f"对 {action_info['action_message'].get('processed_plain_text')} 的回复生成失败")
                                return {
                                    "action_type": "reply",
                                    "success": False,
                                    "reply_text": "",
                                    "loop_info": None
                                }
                            
                        except asyncio.CancelledError:
                            logger.debug(f"{self.log_prefix} 并行执行：回复生成任务已被取消")
                            return {
                                "action_type": "reply",
                                "success": False,
                                "reply_text": "",
                                "loop_info": None
                            }

                        loop_info, reply_text, cycle_timers_reply = await self._send_and_store_reply(
                            response_set=response_set,
                            action_message=action_info["action_message"],
                            cycle_timers=cycle_timers,
                            thinking_id=thinking_id,
                            actions=actions,
                            selected_expressions=selected_expressions,
                        )
                        return {
                            "action_type": "reply",
                            "success": True,
                            "reply_text": reply_text,
                            "loop_info": loop_info
                        }
                except Exception as e:
                    logger.error(f"{self.log_prefix} 执行动作时出错: {e}")
                    logger.error(f"{self.log_prefix} 错误信息: {traceback.format_exc()}")
                    return {
                        "action_type": action_info["action_type"],
                        "success": False,
                        "reply_text": "",
                        "loop_info": None,
                        "error": str(e)
                    }
                  
            action_tasks = [asyncio.create_task(execute_action(action,actions)) for action in actions]
            
            # 并行执行所有任务
            results = await asyncio.gather(*action_tasks, return_exceptions=True)
            
            # 处理执行结果
            reply_loop_info = None
            reply_text_from_reply = ""
            action_success = False
            action_reply_text = ""
            action_command = ""
            
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    logger.error(f"{self.log_prefix} 动作执行异常: {result}")
                    continue
                
                _cur_action = actions[i]
                if result["action_type"] != "reply":
                    action_success = result["success"]
                    action_reply_text = result["reply_text"]
                    action_command = result.get("command", "")
                elif result["action_type"] == "reply":
                    if result["success"]:
                        reply_loop_info = result["loop_info"]
                        reply_text_from_reply = result["reply_text"]
                    else:
                        logger.warning(f"{self.log_prefix} 回复动作执行失败")

            # 构建最终的循环信息
            if reply_loop_info:
                # 如果有回复信息，使用回复的loop_info作为基础
                loop_info = reply_loop_info
                # 更新动作执行信息
                loop_info["loop_action_info"].update(
                    {
                        "action_taken": action_success,
                        "command": action_command,
                        "taken_time": time.time(),
                    }
                )
                reply_text = reply_text_from_reply
            else:
                # 没有回复信息，构建纯动作的loop_info
                loop_info = {
                    "loop_plan_info": {
                        "action_result": actions,
                    },
                    "loop_action_info": {
                        "action_taken": action_success,
                        "reply_text": action_reply_text,
                        "command": action_command,
                        "taken_time": time.time(),
                    },
                }
                reply_text = action_reply_text
                    

        if s4u_config.enable_s4u:
            await stop_typing()
            await mai_thinking_manager.get_mai_think(self.stream_id).do_think_after_response(reply_text)

        self.end_cycle(loop_info, cycle_timers)
        self.print_cycle_info(cycle_timers)

        # await self.willing_manager.after_generate_reply_handle(message_data.get("message_id", ""))

        action_type = actions[0]["action_type"] if actions else "no_action"
        
        # 管理no_reply计数器：当执行了非no_reply动作时，重置计数器
        if action_type != "no_reply":
            # no_reply逻辑已集成到heartFC_chat.py中，直接重置计数器
            self.recent_interest_records.clear()
            self.no_reply_consecutive = 0
            logger.debug(f"{self.log_prefix} 执行了{action_type}动作，重置no_reply计数器")
            return True
            
        if action_type == "no_reply":
            self.no_reply_consecutive += 1
            self._determine_form_type()

        return True

    async def _main_chat_loop(self):
        """主循环，持续进行计划并可能回复消息，直到被外部取消。"""
        try:
            while self.running:
                # 主循环
                success = await self._loopbody()
                await asyncio.sleep(0.1)
                if not success:
                    break
        except asyncio.CancelledError:
            # 设置了关闭标志位后被取消是正常流程
            logger.info(f"{self.log_prefix} 麦麦已关闭聊天")
        except Exception:
            logger.error(f"{self.log_prefix} 麦麦聊天意外错误，将于3s后尝试重新启动")
            print(traceback.format_exc())
            await asyncio.sleep(3)
            self._loop_task = asyncio.create_task(self._main_chat_loop())
        logger.error(f"{self.log_prefix} 结束了当前聊天循环")

    async def _handle_action(
        self,
        action: str,
        reasoning: str,
        action_data: dict,
        cycle_timers: Dict[str, float],
        thinking_id: str,
        action_message: dict,
    ) -> tuple[bool, str, str]:
        """
        处理规划动作，使用动作工厂创建相应的动作处理器

        参数:
            action: 动作类型
            reasoning: 决策理由
            action_data: 动作数据，包含不同动作需要的参数
            cycle_timers: 计时器字典
            thinking_id: 思考ID

        返回:
            tuple[bool, str, str]: (是否执行了动作, 思考消息ID, 命令)
        """
        try:
            # 使用工厂创建动作处理器实例
            try:
                action_handler = self.action_manager.create_action(
                    action_name=action,
                    action_data=action_data,
                    reasoning=reasoning,
                    cycle_timers=cycle_timers,
                    thinking_id=thinking_id,
                    chat_stream=self.chat_stream,
                    log_prefix=self.log_prefix,
                    action_message=action_message,
                )
            except Exception as e:
                logger.error(f"{self.log_prefix} 创建动作处理器时出错: {e}")
                traceback.print_exc()
                return False, "", ""

            if not action_handler:
                logger.warning(f"{self.log_prefix} 未能创建动作处理器: {action}")
                return False, "", ""

            # 处理动作并获取结果
            result = await action_handler.handle_action()
            success, action_text = result
            command = ""

            return success, action_text, command

        except Exception as e:
            logger.error(f"{self.log_prefix} 处理{action}时出错: {e}")
            traceback.print_exc()
            return False, "", ""

    async def _send_response(self, 
                             reply_set, 
                             message_data,
                             selected_expressions:List[int] = None,
                             ) -> str:
        new_message_count = message_api.count_new_messages(
            chat_id=self.chat_stream.stream_id, start_time=self.last_read_time, end_time=time.time()
        )

        need_reply = new_message_count >= random.randint(2, 4)

        if need_reply:
            logger.info(f"{self.log_prefix} 从思考到回复，共有{new_message_count}条新消息，使用引用回复")

        reply_text = ""
        
        # 检查是否为主动思考且决定沉默
        is_proactive_thinking = message_data.get("message_type") == "proactive_thinking"
        
        first_replied = False
        for reply_seg in reply_set:
            data = reply_seg[1]
            reply_text += data
            
            # 如果是主动思考且回复内容是"沉默"，则不发送消息
            if is_proactive_thinking and data.strip() == "沉默":
                logger.info(f"{self.log_prefix} 主动思考决定保持沉默，不发送消息")
                continue
            
            if not first_replied:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message = message_data,
                    set_reply=need_reply,
                    typing=False,
                    selected_expressions=selected_expressions,
                )
                first_replied = True
            else:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=self.chat_stream.stream_id,
                    reply_message = message_data,
                    set_reply=False,
                    typing=True,
                    selected_expressions=selected_expressions,
                )

        return reply_text
