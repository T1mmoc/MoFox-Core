import asyncio
import time
import traceback
from typing import Optional, Dict, Any

from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.planner_actions.planner import ActionPlanner
from src.chat.planner_actions.action_modifier import ActionModifier
from src.plugin_system.base.component_types import ChatMode
from src.mais4u.constant_s4u import ENABLE_S4U
from src.chat.chat_loop.hfc_utils import send_typing, stop_typing
from .hfc_context import HfcContext
from .response_handler import ResponseHandler
from .cycle_tracker import CycleTracker

logger = get_logger("hfc.processor")

class CycleProcessor:
    def __init__(self, context: HfcContext, response_handler: ResponseHandler, cycle_tracker: CycleTracker):
        """
        初始化循环处理器
        
        Args:
            context: HFC聊天上下文对象，包含聊天流、能量值等信息
            response_handler: 响应处理器，负责生成和发送回复
            cycle_tracker: 循环跟踪器，负责记录和管理每次思考循环的信息
        """
        self.context = context
        self.response_handler = response_handler
        self.cycle_tracker = cycle_tracker
        self.action_planner = ActionPlanner(chat_id=self.context.stream_id, action_manager=self.context.action_manager)
        self.action_modifier = ActionModifier(action_manager=self.context.action_manager, chat_id=self.context.stream_id)

    async def observe(self, message_data: Optional[Dict[str, Any]] = None) -> bool:
        """
        观察和处理单次思考循环的核心方法
        
        Args:
            message_data: 可选的消息数据字典，包含用户消息、平台信息等
            
        Returns:
            bool: 处理是否成功
            
        功能说明:
        - 开始新的思考循环并记录计时
        - 修改可用动作并获取动作列表
        - 根据聊天模式和提及情况决定是否跳过规划器
        - 执行动作规划或直接回复
        - 根据动作类型分发到相应的处理方法
        """
        if not message_data:
            message_data = {}
        
        cycle_timers, thinking_id = self.cycle_tracker.start_cycle()
        logger.info(f"{self.context.log_prefix} 开始第{self.context.cycle_counter}次思考[模式：{self.context.loop_mode}]")

        if ENABLE_S4U:
            await send_typing()

        loop_start_time = time.time()
        
        try:
            await self.action_modifier.modify_actions()
            available_actions = self.context.action_manager.get_using_actions()
        except Exception as e:
            logger.error(f"{self.context.log_prefix} 动作修改失败: {e}")
            available_actions = {}

        is_mentioned_bot = message_data.get("is_mentioned", False)
        at_bot_mentioned = (global_config.chat.mentioned_bot_inevitable_reply and is_mentioned_bot) or \
                           (global_config.chat.at_bot_inevitable_reply and is_mentioned_bot)

        if self.context.loop_mode == ChatMode.FOCUS and at_bot_mentioned and "no_reply" in available_actions:
            available_actions = {k: v for k, v in available_actions.items() if k != "no_reply"}

        skip_planner = False
        if self.context.loop_mode == ChatMode.NORMAL:
            non_reply_actions = {k: v for k, v in available_actions.items() if k not in ["reply", "no_reply", "no_action"]}
            if not non_reply_actions:
                skip_planner = True
                plan_result = self._get_direct_reply_plan(loop_start_time)
                target_message = message_data

        gen_task = None
        if not skip_planner and self.context.loop_mode == ChatMode.NORMAL:
            reply_to_str = await self._build_reply_to_str(message_data)
            gen_task = asyncio.create_task(
                self.response_handler.generate_response(
                    message_data=message_data,
                    available_actions=available_actions,
                    reply_to=reply_to_str,
                    request_type="chat.replyer.normal",
                )
            )

        if not skip_planner:
            plan_result, target_message = await self.action_planner.plan(mode=self.context.loop_mode)

        from src.plugin_system.core.event_manager import event_manager
        from src.plugin_system.base.component_types import EventType
        # 触发 ON_PLAN 事件
        result = await event_manager.trigger_event(EventType.ON_PLAN, stream_id=self.chat_stream.stream_id)
        if result and not result.all_continue_process():
            return
            
        action_result = plan_result.get("action_result", {}) if isinstance(plan_result, dict) else {}
        if not isinstance(action_result, dict):
            action_result = {}
        action_type = action_result.get("action_type", "error")
        action_data = action_result.get("action_data", {})
        reasoning = action_result.get("reasoning", "未提供理由")
        is_parallel = action_result.get("is_parallel", True)
        action_data["loop_start_time"] = loop_start_time

        is_private_chat = self.context.chat_stream.group_info is None if self.context.chat_stream else False
        if self.context.loop_mode == ChatMode.FOCUS and is_private_chat and action_type == "no_reply":
            action_type = "reply"

        if action_type == "reply":
            # 使用 action_planner 获取的 target_message，如果为空则使用原始 message_data
            actual_message = target_message or message_data
            await self._handle_reply_action(
                actual_message, available_actions, gen_task, loop_start_time, cycle_timers, thinking_id, plan_result
            )
        else:
            await self._handle_other_actions(
                action_type, reasoning, action_data, is_parallel, gen_task, target_message or message_data,
                cycle_timers, thinking_id, plan_result, loop_start_time
            )

        if ENABLE_S4U:
            await stop_typing()
        
        return True

    async def execute_plan(self, action_result: Dict[str, Any], target_message: Optional[Dict[str, Any]]):
        """
        执行一个已经制定好的计划
        """
        action_type = action_result.get("action_type", "error")
        
        # 这里我们需要为执行计划创建一个新的循环追踪
        cycle_timers, thinking_id = self.cycle_tracker.start_cycle(is_proactive=True)
        loop_start_time = time.time()

        if action_type == "reply":
            # 主动思考不应该直接触发简单回复，但为了逻辑完整性，我们假设它会调用response_handler
            # 注意：这里的 available_actions 和 plan_result 是缺失的，需要根据实际情况处理
            await self._handle_reply_action(target_message, {}, None, loop_start_time, cycle_timers, thinking_id, {"action_result": action_result})
        else:
            await self._handle_other_actions(
                action_type,
                action_result.get("reasoning", ""),
                action_result.get("action_data", {}),
                action_result.get("is_parallel", False),
                None,
                target_message,
                cycle_timers,
                thinking_id,
                {"action_result": action_result},
                loop_start_time
            )

    async def _handle_reply_action(self, message_data, available_actions, gen_task, loop_start_time, cycle_timers, thinking_id, plan_result):
        """
        处理回复类型的动作
        
        Args:
            message_data: 消息数据
            available_actions: 可用动作列表
            gen_task: 预先创建的生成任务（可能为None）
            loop_start_time: 循环开始时间
            cycle_timers: 循环计时器
            thinking_id: 思考ID
            plan_result: 规划结果
            
        功能说明:
        - 根据聊天模式决定是否使用预生成的回复或实时生成
        - 在NORMAL模式下使用异步生成提高效率
        - 在FOCUS模式下同步生成确保及时响应
        - 发送生成的回复并结束循环
        """
        # 初始化reply_to_str以避免UnboundLocalError
        reply_to_str = None
        
        if self.context.loop_mode == ChatMode.NORMAL:
            if not gen_task:
                reply_to_str = await self._build_reply_to_str(message_data)
                gen_task = asyncio.create_task(
                    self.response_handler.generate_response(
                        message_data=message_data,
                        available_actions=available_actions,
                        reply_to=reply_to_str,
                        request_type="chat.replyer.normal",
                    )
                )
            else:
                # 如果gen_task已存在但reply_to_str还未构建，需要构建它
                if reply_to_str is None:
                    reply_to_str = await self._build_reply_to_str(message_data)
            
            try:
                response_set = await asyncio.wait_for(gen_task, timeout=global_config.chat.thinking_timeout)
            except asyncio.TimeoutError:
                response_set = None
        else:
            reply_to_str = await self._build_reply_to_str(message_data)
            response_set = await self.response_handler.generate_response(
                message_data=message_data,
                available_actions=available_actions,
                reply_to=reply_to_str,
                request_type="chat.replyer.focus",
            )

        if response_set:
            loop_info, _, _ = await self.response_handler.generate_and_send_reply(
                response_set, reply_to_str, loop_start_time, message_data, cycle_timers, thinking_id, plan_result
            )
            self.cycle_tracker.end_cycle(loop_info, cycle_timers)

    async def _handle_other_actions(self, action_type, reasoning, action_data, is_parallel, gen_task, action_message, cycle_timers, thinking_id, plan_result, loop_start_time):
        """
        处理非回复类型的动作（如no_reply、自定义动作等）
        
        Args:
            action_type: 动作类型
            reasoning: 动作理由
            action_data: 动作数据
            is_parallel: 是否并行执行
            gen_task: 生成任务
            action_message: 动作消息
            cycle_timers: 循环计时器
            thinking_id: 思考ID
            plan_result: 规划结果
            loop_start_time: 循环开始时间
            
        功能说明:
        - 在NORMAL模式下可能并行执行回复生成和动作处理
        - 等待所有异步任务完成
        - 整合回复和动作的执行结果
        - 构建最终循环信息并结束循环
        """
        background_reply_task = None
        if self.context.loop_mode == ChatMode.NORMAL and is_parallel and gen_task:
            background_reply_task = asyncio.create_task(self._handle_parallel_reply(gen_task, loop_start_time, action_message, cycle_timers, thinking_id, plan_result))

        background_action_task = asyncio.create_task(self._handle_action(action_type, reasoning, action_data, cycle_timers, thinking_id, action_message))

        reply_loop_info, action_success, action_reply_text, action_command = None, False, "", ""
        
        if background_reply_task:
            results = await asyncio.gather(background_reply_task, background_action_task, return_exceptions=True)
            reply_result, action_result_val = results
            if not isinstance(reply_result, BaseException) and reply_result is not None:
                reply_loop_info, _, _ = reply_result
            else:
                reply_loop_info = None
                
            if not isinstance(action_result_val, BaseException) and action_result_val is not None:
                action_success, action_reply_text, action_command = action_result_val
            else:
                action_success, action_reply_text, action_command = False, "", ""
        else:
            results = await asyncio.gather(background_action_task, return_exceptions=True)
            if results and len(results) > 0:
                action_result_val = results[0]  # Get the actual result from the tuple
            else:
                action_result_val = (False, "", "")
            
            if not isinstance(action_result_val, BaseException) and action_result_val is not None:
                action_success, action_reply_text, action_command = action_result_val
            else:
                action_success, action_reply_text, action_command = False, "", ""

        loop_info = self._build_final_loop_info(reply_loop_info, action_success, action_reply_text, action_command, plan_result)
        self.cycle_tracker.end_cycle(loop_info, cycle_timers)

    async def _handle_parallel_reply(self, gen_task, loop_start_time, action_message, cycle_timers, thinking_id, plan_result):
        """
        处理并行回复生成
        
        Args:
            gen_task: 回复生成任务
            loop_start_time: 循环开始时间
            action_message: 动作消息
            cycle_timers: 循环计时器
            thinking_id: 思考ID
            plan_result: 规划结果
            
        Returns:
            tuple: (循环信息, 回复文本, 计时器信息) 或 None
            
        功能说明:
        - 等待并行回复生成任务完成（带超时）
        - 构建回复目标字符串
        - 发送生成的回复
        - 返回循环信息供上级方法使用
        """
        try:
            response_set = await asyncio.wait_for(gen_task, timeout=global_config.chat.thinking_timeout)
        except asyncio.TimeoutError:
            return None, "", {}
        
        if not response_set:
            return None, "", {}

        reply_to_str = await self._build_reply_to_str(action_message)
        return await self.response_handler.generate_and_send_reply(
            response_set, reply_to_str, loop_start_time, action_message, cycle_timers, thinking_id, plan_result
        )

    async def _handle_action(self, action, reasoning, action_data, cycle_timers, thinking_id, action_message) -> tuple[bool, str, str]:
        """
        处理具体的动作执行
        
        Args:
            action: 动作名称
            reasoning: 执行理由
            action_data: 动作数据
            cycle_timers: 循环计时器
            thinking_id: 思考ID
            action_message: 动作消息
            
        Returns:
            tuple: (执行是否成功, 回复文本, 命令文本)
            
        功能说明:
        - 创建对应的动作处理器
        - 执行动作并捕获异常
        - 返回执行结果供上级方法整合
        """
        if not self.context.chat_stream:
            return False, "", ""
        try:
            action_handler = self.context.action_manager.create_action(
                action_name=action,
                action_data=action_data,
                reasoning=reasoning,
                cycle_timers=cycle_timers,
                thinking_id=thinking_id,
                chat_stream=self.context.chat_stream,
                log_prefix=self.context.log_prefix,
                action_message=action_message,
            )
            if not action_handler:
                # 动作处理器创建失败，尝试回退机制
                logger.warning(f"{self.context.log_prefix} 创建动作处理器失败: {action}，尝试回退方案")
                
                # 获取当前可用的动作
                available_actions = self.context.action_manager.get_using_actions()
                fallback_action = None
                
                # 回退优先级：reply > 第一个可用动作
                if "reply" in available_actions:
                    fallback_action = "reply"
                elif available_actions:
                    fallback_action = list(available_actions.keys())[0]
                
                if fallback_action and fallback_action != action:
                    logger.info(f"{self.context.log_prefix} 使用回退动作: {fallback_action}")
                    action_handler = self.context.action_manager.create_action(
                        action_name=fallback_action,
                        action_data=action_data,
                        reasoning=f"原动作'{action}'不可用，自动回退。{reasoning}",
                        cycle_timers=cycle_timers,
                        thinking_id=thinking_id,
                        chat_stream=self.context.chat_stream,
                        log_prefix=self.context.log_prefix,
                        action_message=action_message,
                    )
                
                if not action_handler:
                    logger.error(f"{self.context.log_prefix} 回退方案也失败，无法创建任何动作处理器")
                    return False, "", ""
            
            success, reply_text = await action_handler.handle_action()
            return success, reply_text, ""
        except Exception as e:
            logger.error(f"{self.context.log_prefix} 处理{action}时出错: {e}")
            traceback.print_exc()
            return False, "", ""

    def _get_direct_reply_plan(self, loop_start_time):
        """
        获取直接回复的规划结果
        
        Args:
            loop_start_time: 循环开始时间
            
        Returns:
            dict: 包含直接回复动作的规划结果
            
        功能说明:
        - 在某些情况下跳过复杂规划，直接返回回复动作
        - 主要用于NORMAL模式下没有其他可用动作时的简化处理
        """
        return {
            "action_result": {
                "action_type": "reply",
                "action_data": {"loop_start_time": loop_start_time},
                "reasoning": "",
                "timestamp": time.time(),
                "is_parallel": False,
            },
            "action_prompt": "",
        }

    async def _build_reply_to_str(self, message_data: dict):
        """
        构建回复目标字符串
        
        Args:
            message_data: 消息数据字典
            
        Returns:
            str: 格式化的回复目标字符串，格式为"用户名:消息内容"
            
        功能说明:
        - 从消息数据中提取平台和用户ID信息
        - 通过人员信息管理器获取用户昵称
        - 构建用于回复显示的格式化字符串
        """
        from src.person_info.person_info import get_person_info_manager
        person_info_manager = get_person_info_manager()
        platform = message_data.get("chat_info_platform") or message_data.get("user_platform") or (self.context.chat_stream.platform if self.context.chat_stream else "default")
        user_id = message_data.get("user_id", "")
        person_id = person_info_manager.get_person_id(platform, user_id)
        person_name = await person_info_manager.get_value(person_id, "person_name")
        return f"{person_name}:{message_data.get('processed_plain_text')}"

    def _build_final_loop_info(self, reply_loop_info, action_success, action_reply_text, action_command, plan_result):
        """
        构建最终的循环信息
        
        Args:
            reply_loop_info: 回复循环信息（可能为None）
            action_success: 动作执行是否成功
            action_reply_text: 动作回复文本
            action_command: 动作命令
            plan_result: 规划结果
            
        Returns:
            dict: 完整的循环信息，包含规划信息和动作信息
            
        功能说明:
        - 如果有回复循环信息，则在其基础上添加动作信息
        - 如果没有回复信息，则创建新的循环信息结构
        - 整合所有执行结果供循环跟踪器记录
        """
        if reply_loop_info:
            loop_info = reply_loop_info
            loop_info["loop_action_info"].update({
                "action_taken": action_success,
                "command": action_command,
                "taken_time": time.time(),
            })
        else:
            loop_info = {
                "loop_plan_info": {"action_result": plan_result.get("action_result", {})},
                "loop_action_info": {
                    "action_taken": action_success,
                    "reply_text": action_reply_text,
                    "command": action_command,
                    "taken_time": time.time(),
                },
            }
        return loop_info
