import asyncio
import time
import traceback
import math
import random
from typing import Dict, Any, Tuple, Optional

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

# 日志记录器
logger = get_logger("hfc.processor")


class CycleProcessor:
    """
    循环处理器类，负责处理单次思考循环的逻辑。
    """
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
        """
        发送并存储回复信息

        Args:
            response_set: 回复内容集合
            loop_start_time: 循环开始时间
            action_message: 动作消息
            cycle_timers: 循环计时器
            thinking_id: 思考ID
            actions: 动作列表

        Returns:
            Tuple[Dict[str, Any], str, Dict[str, float]]: 循环信息, 回复文本, 循环计时器
        """
        # 发送回复
        with Timer("回复发送", cycle_timers):
            reply_text = await self.response_handler.send_response(response_set, loop_start_time, action_message)

        # 存储reply action信息
        person_info_manager = get_person_info_manager()

        # 获取 platform，如果不存在则从 chat_stream 获取，如果还是 None 则使用默认值
        platform = action_message.get("chat_info_platform")
        if platform is None:
            platform = getattr(self.context.chat_stream, "platform", "unknown")

        # 获取用户信息并生成回复提示
        person_id = person_info_manager.get_person_id(
            platform,
            action_message.get("chat_info_user_id", ""),
        )
        person_name = await person_info_manager.get_value(person_id, "person_name")
        action_prompt_display = f"你对{person_name}进行了回复：{reply_text}"

        # 存储动作信息到数据库
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

    async def observe(self, interest_value: float = 0.0) -> str:
        """
        观察和处理单次思考循环的核心方法

        Args:
            interest_value: 兴趣值

        Returns:
            str: 动作类型

        功能说明:
        - 开始新的思考循环并记录计时
        - 修改可用动作并获取动作列表
        - 根据聊天模式和提及情况决定是否跳过规划器
        - 执行动作规划或直接回复
        - 根据动作类型分发到相应的处理方法
        """
        action_type = "no_action"
        reply_text = ""  # 初始化reply_text变量，避免UnboundLocalError


        # 开始新的思考循环
        cycle_timers, thinking_id = self.cycle_tracker.start_cycle()
        logger.info(f"{self.log_prefix} 开始第{self.context.cycle_counter}次思考")

        if ENABLE_S4U and self.context.chat_stream and self.context.chat_stream.user_info:
            await send_typing(self.context.chat_stream.user_info.user_id)

        loop_start_time = time.time()

        # 使用sigmoid函数将interest_value转换为概率
        # 当interest_value为0时，概率接近0（使用Focus模式）
        # 当interest_value很高时，概率接近1（使用Normal模式）
        def calculate_normal_mode_probability(interest_val: float) -> float:
            """
            计算普通模式的概率

            Args:
                interest_val: 兴趣值

            Returns:
                float: 概率
            """
            # 使用sigmoid函数，调整参数使概率分布更合理
            # 当interest_value = 0时，概率约为0.1
            # 当interest_value = 1时，概率约为0.5
            # 当interest_value = 2时，概率约为0.8
            # 当interest_value = 3时，概率约为0.95
            k = 2.0  # 控制曲线陡峭程度
            x0 = 1.0  # 控制曲线中心点
            return 1.0 / (1.0 + math.exp(-k * (interest_val - x0)))

        # 计算普通模式概率
        normal_mode_probability = (
            calculate_normal_mode_probability(interest_value)
            * 0.5
            / global_config.chat.get_current_talk_frequency(self.context.stream_id)
        )

        # 根据概率决定使用哪种模式
        if random.random() < normal_mode_probability:
            mode = ChatMode.NORMAL
            logger.info(
                f"{self.log_prefix} 基于兴趣值 {interest_value:.2f}，概率 {normal_mode_probability:.2f}，选择Normal planner模式"
            )
        else:
            mode = ChatMode.FOCUS
            logger.info(
                f"{self.log_prefix} 基于兴趣值 {interest_value:.2f}，概率 {normal_mode_probability:.2f}，选择Focus planner模式"
            )

        # 开始新的思考循环
        cycle_timers, thinking_id = self.cycle_tracker.start_cycle()
        logger.info(f"{self.log_prefix} 开始第{self.context.cycle_counter}次思考")

        if ENABLE_S4U and self.context.chat_stream and self.context.chat_stream.user_info:
            await send_typing(self.context.chat_stream.user_info.user_id)

        loop_start_time = time.time()

        # 第一步：动作修改
        with Timer("动作修改", cycle_timers):
            try:
                await self.action_modifier.modify_actions(mode=mode)
                available_actions = self.context.action_manager.get_using_actions()
            except Exception as e:
                logger.error(f"{self.context.log_prefix} 动作修改失败: {e}")
                available_actions = {}

            # 规划动作
        from src.plugin_system.core.event_manager import event_manager
        from src.plugin_system import EventType
        
        result = await event_manager.trigger_event(
                        EventType.ON_PLAN, permission_group="SYSTEM", stream_id=self.context.chat_stream
                    )
        if result and not result.all_continue_process():
            raise UserWarning(f"插件{result.get_summary().get('stopped_handlers', '')}于规划前中断了内容生成")
        with Timer("规划器", cycle_timers):
            actions, _ = await self.action_planner.plan(mode=mode)
            
        # 在这里添加日志，清晰地显示最终选择的动作
        if actions:
            chosen_actions = [a.get("action_type", "unknown") for a in actions]
            logger.info(f"{self.log_prefix} LLM最终选择的动作: {chosen_actions}")
        else:
            logger.info(f"{self.log_prefix} LLM最终没有选择任何动作")
        
        async def execute_action(action_info):
            """执行单个动作的通用函数"""
            try:
                if action_info["action_type"] == "no_action":
                    return {"action_type": "no_action", "success": True, "reply_text": "", "command": ""}            
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

                    return {"action_type": "no_reply", "success": True, "reply_text": "", "command": ""}
                elif action_info["action_type"] != "reply" and action_info["action_type"] != "no_action":
                    # 记录并执行普通动作
                    reason = action_info.get("reasoning", f"执行动作 {action_info['action_type']}")
                    logger.info(f"{self.log_prefix} 决定执行动作 '{action_info['action_type']}'，内心思考: {reason}")
                    with Timer("动作执行", cycle_timers):
                        success, reply_text, command = await self._handle_action(
                            action_info["action_type"],
                            reason, # 使用已获取的reason
                            action_info["action_data"],
                            cycle_timers,
                            thinking_id,
                            action_info["action_message"],
                        )
                    return {
                        "action_type": action_info["action_type"],
                        "success": success,
                        "reply_text": reply_text,
                        "command": command,
                    }
                else:
                    # 生成回复
                    try:
                        reason = action_info.get("reasoning", "决定进行回复")
                        logger.info(f"{self.log_prefix} 决定进行回复，内心思考: {reason}")
                        success, response_set, _ = await generator_api.generate_reply(
                            chat_stream=self.context.chat_stream,
                            reply_message=action_info["action_message"],
                            available_actions=available_actions,
                            enable_tool=global_config.tool.enable_tool,
                            request_type="chat.replyer",
                            from_plugin=False,
                        )
                        if not success or not response_set:
                            logger.info(
                                f"对 {action_info['action_message'].get('processed_plain_text')} 的回复生成失败"
                            )
                            return {"action_type": "reply", "success": False, "reply_text": "", "loop_info": None}
                    except asyncio.CancelledError:
                        logger.debug(f"{self.log_prefix} 并行执行：回复生成任务已被取消")
                        return {"action_type": "reply", "success": False, "reply_text": "", "loop_info": None}

                    # 发送并存储回复
                    loop_info, reply_text, cycle_timers_reply = await self._send_and_store_reply(
                        response_set,
                        loop_start_time,
                        action_info["action_message"],
                        cycle_timers,
                        thinking_id,
                        actions,
                    )
                    return {"action_type": "reply", "success": True, "reply_text": reply_text, "loop_info": loop_info}
            except Exception as e:
                logger.error(f"{self.log_prefix} 执行动作时出错: {e}")
                logger.error(f"{self.log_prefix} 错误信息: {traceback.format_exc()}")
                return {
                    "action_type": action_info["action_type"],
                    "success": False,
                    "reply_text": "",
                    "loop_info": None,
                    "error": str(e),
                }

        # 分离 reply 动作和其他动作
        reply_actions = [a for a in actions if a.get("action_type") == "reply"]
        other_actions = [a for a in actions if a.get("action_type") != "reply"]
        
        reply_loop_info = None
        reply_text_from_reply = ""
        other_actions_results = []

        # 1. 首先串行执行所有 reply 动作（通常只有一个）
        if reply_actions:
            logger.info(f"{self.log_prefix} 正在执行文本回复...")
            for action in reply_actions:
                action_message = action.get("action_message")
                if not action_message:
                    logger.warning(f"{self.log_prefix} reply 动作缺少 action_message，跳过")
                    continue
                
                # 检查是否是空的DatabaseMessages对象
                if hasattr(action_message, 'chat_info') and hasattr(action_message.chat_info, 'user_info'):
                    target_user_id = action_message.chat_info.user_info.user_id
                else:
                    # 如果是字典格式，使用原来的方式
                    target_user_id = action_message.get("chat_info_user_id", "")
                
                if not target_user_id:
                    logger.warning(f"{self.log_prefix} reply 动作的 action_message 缺少用户ID，跳过")
                    continue

                if target_user_id == global_config.bot.qq_account and not global_config.chat.allow_reply_self:
                    logger.warning("选取的reply的目标为bot自己，跳过reply action")
                    continue
                result = await execute_action(action)
                if isinstance(result, Exception):
                    logger.error(f"{self.log_prefix} 回复动作执行异常: {result}")
                    continue
                if result.get("success"):
                    reply_loop_info = result.get("loop_info")
                    reply_text_from_reply = result.get("reply_text", "")
                else:
                    logger.warning(f"{self.log_prefix} 回复动作执行失败")

        # 2. 然后并行执行所有其他动作
        if other_actions:
            logger.info(f"{self.log_prefix} 正在执行附加动作: {[a.get('action_type') for a in other_actions]}")
            
            # 从回复文本中提取歌名
            song_name_from_reply = self._extract_song_name_from_reply(reply_text_from_reply)
            
            other_action_tasks = []
            for action in other_actions:
                if action.get("action_type") == "music_search" and song_name_from_reply:
                    action["action_data"]["song_name"] = song_name_from_reply
                other_action_tasks.append(asyncio.create_task(execute_action(action)))

            results = await asyncio.gather(*other_action_tasks, return_exceptions=True)
            for i, result in enumerate(results):
                if isinstance(result, BaseException):
                    logger.error(f"{self.log_prefix} 附加动作执行异常: {result}")
                    continue
                other_actions_results.append(result)

        # 构建最终的循环信息
        if reply_loop_info:
            loop_info = reply_loop_info
            # 将其他动作的结果合并到loop_info中
            if "other_actions" not in loop_info["loop_action_info"]:
                loop_info["loop_action_info"]["other_actions"] = []
            loop_info["loop_action_info"]["other_actions"].extend(other_actions_results)
            reply_text = reply_text_from_reply
        else:
            # 没有回复信息，构建纯动作的loop_info
            # 即使没有回复，也要正确处理其他动作
            final_action_taken = any(res.get("success", False) for res in other_actions_results)
            final_reply_text = " ".join(res.get("reply_text", "") for res in other_actions_results if res.get("reply_text"))
            final_command = " ".join(res.get("command", "") for res in other_actions_results if res.get("command"))

            loop_info = {
                "loop_plan_info": {
                    "action_result": actions,
                },
                "loop_action_info": {
                    "action_taken": final_action_taken,
                    "reply_text": final_reply_text,
                    "command": final_command,
                    "taken_time": time.time(),
                    "other_actions": other_actions_results,
                },
            }
            reply_text = final_reply_text

        # 停止正在输入状态
        if ENABLE_S4U:
            await stop_typing()

        # 结束循环
        self.context.chat_instance.cycle_tracker.end_cycle(loop_info, cycle_timers)
        self.context.chat_instance.cycle_tracker.print_cycle_info(cycle_timers)

        action_type = actions[0]["action_type"] if actions else "no_action"
        return action_type

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
            # 创建动作处理器
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
        
            # 执行动作
            success, reply_text = await action_handler.handle_action()
            return success, reply_text, ""
        except Exception as e:
            logger.error(f"{self.context.log_prefix} 处理{action}时出错: {e}")
            traceback.print_exc()
            return False, "", ""

    def _extract_song_name_from_reply(self, reply_text: str) -> Optional[str]:
        """从回复文本中提取歌名"""
        import re
        # 匹配书名号中的内容
        match = re.search(r"《(.*?)》", reply_text)
        if match:
            return match.group(1)
        return None
