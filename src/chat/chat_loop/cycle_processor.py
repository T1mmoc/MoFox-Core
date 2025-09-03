import asyncio
import time
import traceback
import math
import random
from typing import Optional, Dict, Any, Tuple

from src.chat.message_receive.chat_stream import get_chat_manager
from src.chat.utils.timer_calculator import Timer
from src.common.logger import get_logger
from src.config.config import global_config
from src.chat.planner_actions.planner import ActionPlanner
from src.chat.planner_actions.action_modifier import ActionModifier
from src.person_info.person_info import get_person_info_manager
from src.plugin_system.apis import database_api, generator_api
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
        self.action_modifier = ActionModifier(
            action_manager=self.context.action_manager, chat_id=self.context.stream_id
        )

        self.log_prefix = self.context.log_prefix

    async def _send_and_store_reply(
        self,
        response_set,
        loop_start_time,
        action_message,
        cycle_timers: Dict[str, float],
        thinking_id,
        actions,
    ) -> Tuple[Dict[str, Any], str, Dict[str, float]]:
        with Timer("回复发送", cycle_timers):
            reply_text = await self.response_handler.send_response(response_set, loop_start_time, action_message)

            # 存储reply action信息
        person_info_manager = get_person_info_manager()
        
        # 获取 platform，如果不存在则从 chat_stream 获取，如果还是 None 则使用默认值
        platform = action_message.get("chat_info_platform")
        if platform is None:
            platform = getattr(self.context.chat_stream, "platform", "unknown")
        
        person_id = person_info_manager.get_person_id(
            platform,
            action_message.get("user_id", ""),
        )
        person_name = await person_info_manager.get_value(person_id, "person_name")
        action_prompt_display = f"你对{person_name}进行了回复：{reply_text}"

        await database_api.store_action_info(
            chat_stream=self.context.chat_stream,
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
    
    async def observe(self,interest_value:float = 0.0) -> bool:
        """
        观察和处理单次思考循环的核心方法

        Args:
            interest_value: 兴趣值

        Returns:
            bool: 处理是否成功

        功能说明:
        - 开始新的思考循环并记录计时
        - 修改可用动作并获取动作列表
        - 根据聊天模式和提及情况决定是否跳过规划器
        - 执行动作规划或直接回复
        - 根据动作类型分发到相应的处理方法
        """
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
        
        normal_mode_probability = calculate_normal_mode_probability(interest_value) * 0.5 / global_config.chat.get_current_talk_frequency(self.context.stream_id)
        
        # 根据概率决定使用哪种模式
        if random.random() < normal_mode_probability:
            mode = ChatMode.NORMAL
            logger.info(f"{self.log_prefix} 基于兴趣值 {interest_value:.2f}，概率 {normal_mode_probability:.2f}，选择Normal planner模式")
        else:
            mode = ChatMode.FOCUS
            logger.info(f"{self.log_prefix} 基于兴趣值 {interest_value:.2f}，概率 {normal_mode_probability:.2f}，选择Focus planner模式")

        cycle_timers, thinking_id = self.cycle_tracker.start_cycle()
        logger.info(f"{self.log_prefix} 开始第{self.context.cycle_counter}次思考")

        if ENABLE_S4U:
            await send_typing()

        loop_start_time = time.time()

        # 第一步：动作修改
        with Timer("动作修改", cycle_timers):
            try:
                await self.action_modifier.modify_actions()
                available_actions = self.context.action_manager.get_using_actions()
            except Exception as e:
                logger.error(f"{self.context.log_prefix} 动作修改失败: {e}")
                available_actions = {}

            # 执行planner
            planner_info = self.action_planner.get_necessary_info()
            prompt_info = await self.action_planner.build_planner_prompt(
                is_group_chat=planner_info[0],
                chat_target_info=planner_info[1],
                current_available_actions=planner_info[2],
            )
            from src.plugin_system.core.event_manager import event_manager
            from src.plugin_system import EventType

            result = await event_manager.trigger_event(EventType.ON_PLAN,plugin_name="SYSTEM", stream_id=self.context.chat_stream)
            if not result.all_continue_process():
                raise UserWarning(f"插件{result.get_summary().get('stopped_handlers', '')}于规划前中断了内容生成")
            
            with Timer("规划器", cycle_timers):
                actions, _= await self.action_planner.plan(
                    mode=mode,
                    loop_start_time=loop_start_time,
                    available_actions=available_actions,
                )

        async def execute_action(action_info):
            """执行单个动作的通用函数"""
            try:
                if action_info["action_type"] == "no_reply":
                    # 直接处理no_reply逻辑，不再通过动作系统
                    reason = action_info.get("reasoning", "选择不回复")
                    logger.info(f"{self.log_prefix} 选择不回复，原因: {reason}")
                    
                    # 存储no_reply信息到数据库
                    await database_api.store_action_info(
                        chat_stream=self.context.chat_stream,
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
                        success, response_set, _ = await generator_api.generate_reply(
                            chat_stream=self.context.chat_stream,
                            reply_message = action_info["action_message"],
                            available_actions=available_actions,
                            enable_tool=global_config.tool.enable_tool,
                            request_type="chat.replyer",
                            from_plugin=False,
                        )     
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
                        response_set,
                        loop_start_time,
                        action_info["action_message"],
                        cycle_timers,
                        thinking_id,
                        actions,
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
            
        # 创建所有动作的后台任务
        action_tasks = [asyncio.create_task(execute_action(action)) for action in actions]

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
            
            action_info = actions[i]
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
        
        if ENABLE_S4U:
            await stop_typing()

        self.context.chat_instance.cycle_tracker.end_cycle(loop_info, cycle_timers)
        self.context.chat_instance.cycle_tracker.print_cycle_info(cycle_timers)

        action_type = actions[0]["action_type"] if actions else "no_action"
        # 管理no_reply计数器：当执行了非no_reply动作时，重置计数器
        if action_type != "no_reply":
            # no_reply逻辑已集成到heartFC_chat.py中，直接重置计数器
            self.context.chat_instance.recent_interest_records.clear()
            self.context.no_reply_consecutive = 0
            logger.debug(f"{self.log_prefix} 执行了{action_type}动作，重置no_reply计数器")
            return True
            
        if action_type == "no_reply":
            self.context.no_reply_consecutive += 1
            self.context.chat_instance._determine_form_type()

        # 在一轮动作执行完毕后，增加睡眠压力
        if self.context.energy_manager and global_config.sleep_system.enable_insomnia_system:
            if action_type not in ["no_reply", "no_action"]:
                self.context.energy_manager.increase_sleep_pressure()

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
            await self._handle_reply_action(
                target_message, {}, None, loop_start_time, cycle_timers, thinking_id, {"action_result": action_result}
            )
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
                loop_start_time,
            )

    async def _handle_reply_action(
        self, message_data, available_actions, gen_task, loop_start_time, cycle_timers, thinking_id, plan_result
    ):
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

    async def _handle_other_actions(
        self,
        action_type,
        reasoning,
        action_data,
        is_parallel,
        gen_task,
        action_message,
        cycle_timers,
        thinking_id,
        plan_result,
        loop_start_time,
    ):
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
            background_reply_task = asyncio.create_task(
                self._handle_parallel_reply(
                    gen_task, loop_start_time, action_message, cycle_timers, thinking_id, plan_result
                )
            )

        background_action_task = asyncio.create_task(
            self._handle_action(action_type, reasoning, action_data, cycle_timers, thinking_id, action_message)
        )

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

        loop_info = self._build_final_loop_info(
            reply_loop_info, action_success, action_reply_text, action_command, plan_result
        )
        self.cycle_tracker.end_cycle(loop_info, cycle_timers)

    async def _handle_parallel_reply(
        self, gen_task, loop_start_time, action_message, cycle_timers, thinking_id, plan_result
    ):
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

    async def _handle_action(
        self, action, reasoning, action_data, cycle_timers, thinking_id, action_message
    ) -> tuple[bool, str, str]:
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
            loop_info["loop_action_info"].update(
                {
                    "action_taken": action_success,
                    "command": action_command,
                    "taken_time": time.time(),
                }
            )
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
