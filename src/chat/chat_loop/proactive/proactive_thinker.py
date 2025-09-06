import time
import traceback
import orjson
from typing import TYPE_CHECKING, Dict, Any

from src.common.logger import get_logger
from src.plugin_system.base.component_types import ChatMode
from ..hfc_context import HfcContext
from .events import ProactiveTriggerEvent
from src.plugin_system.apis import generator_api
from src.schedule.schedule_manager import schedule_manager
from src.plugin_system import tool_api
from src.plugin_system.base.component_types import ComponentType
from src.config.config import global_config
from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat, build_readable_messages_with_id
from src.mood.mood_manager import mood_manager

if TYPE_CHECKING:
    from ..cycle_processor import CycleProcessor

logger = get_logger("hfc")


class ProactiveThinker:
    """
    主动思考器，负责处理和执行主动思考事件。
    当接收到 ProactiveTriggerEvent 时，它会根据事件内容进行一系列决策和操作，
    例如调整情绪、调用规划器生成行动，并最终可能产生一个主动的回复。
    """

    def __init__(self, context: HfcContext, cycle_processor: "CycleProcessor"):
        """
        初始化主动思考器。

        Args:
            context (HfcContext): HFC聊天上下文对象，提供了当前聊天会话的所有背景信息。
            cycle_processor (CycleProcessor): 循环处理器，用于执行主动思考后产生的动作。

        功能说明:
        - 接收并处理主动思考事件 (ProactiveTriggerEvent)。
        - 在思考前根据事件类型执行预处理操作，如修改当前情绪状态。
        - 调用行动规划器 (Action Planner) 来决定下一步应该做什么。
        - 如果规划结果是发送消息，则调用生成器API生成回复并发送。
        """
        self.context = context
        self.cycle_processor = cycle_processor

    async def think(self, trigger_event: ProactiveTriggerEvent):
        """
        主动思考的统一入口API。
        这是外部触发主动思考时调用的主要方法。

        Args:
            trigger_event (ProactiveTriggerEvent): 描述触发上下文的事件对象，包含了思考的来源和原因。
        """
        logger.info(
            f"{self.context.log_prefix} 接收到主动思考事件: "
            f"来源='{trigger_event.source}', 原因='{trigger_event.reason}'"
        )

        try:
            # 步骤 1: 根据事件类型执行思考前的准备工作，例如调整情绪。
            await self._prepare_for_thinking(trigger_event)

            # 步骤 2: 执行核心的思考和决策逻辑。
            await self._execute_proactive_thinking(trigger_event)

        except Exception as e:
            # 捕获并记录在思考过程中发生的任何异常。
            logger.error(f"{self.context.log_prefix} 主动思考 think 方法执行异常: {e}")
            logger.error(traceback.format_exc())

    async def _prepare_for_thinking(self, trigger_event: ProactiveTriggerEvent):
        """
        根据事件类型，在正式思考前执行准备工作。
        目前主要是处理来自失眠管理器的事件，并据此调整情绪。

        Args:
            trigger_event (ProactiveTriggerEvent): 触发事件。
        """
        # 目前只处理来自失眠管理器(insomnia_manager)的事件
        if trigger_event.source != "insomnia_manager":
            return

        try:
            # 获取当前聊天的情绪对象
            mood_obj = mood_manager.get_mood_by_chat_id(self.context.stream_id)
            new_mood = None

            # 根据失眠的不同原因设置对应的情绪
            if trigger_event.reason == "low_pressure":
                new_mood = "精力过剩，毫无睡意"
            elif trigger_event.reason == "random":
                new_mood = "深夜emo，胡思乱想"
            elif trigger_event.reason == "goodnight":
                new_mood = "有点困了，准备睡觉了"

            # 如果成功匹配到了新的情绪，则更新情绪状态
            if new_mood:
                mood_obj.mood_state = new_mood
                mood_obj.last_change_time = time.time()
                logger.info(
                    f"{self.context.log_prefix} 因 '{trigger_event.reason}'，"
                    f"情绪状态被强制更新为: {mood_obj.mood_state}"
                )

        except Exception as e:
            logger.error(f"{self.context.log_prefix} 设置失眠情绪时出错: {e}")

    async def _execute_proactive_thinking(self, trigger_event: ProactiveTriggerEvent):
        """
        执行主动思考的核心逻辑。
        它会调用规划器来决定是否要采取行动，以及采取什么行动。

        Args:
            trigger_event (ProactiveTriggerEvent): 触发事件。
        """
        try:
            # 调用规划器的 PROACTIVE 模式，让其决定下一步的行动
            actions, _ = await self.cycle_processor.action_planner.plan(mode=ChatMode.PROACTIVE)

            # 通常只关心规划出的第一个动作
            action_result = actions[0] if actions else {}

            action_type = action_result.get("action_type")

            if action_type == "proactive_reply":
                await self._generate_proactive_content_and_send(action_result)
            elif action_type != "do_nothing":
                logger.warning(f"{self.context.log_prefix} 主动思考返回了未知的动作类型: {action_type}")
            else:
                # 如果规划结果是“什么都不做”，则记录日志
                logger.info(f"{self.context.log_prefix} 主动思考决策: 保持沉默")

        except Exception as e:
            logger.error(f"{self.context.log_prefix} 主动思考执行异常: {e}")
            logger.error(traceback.format_exc())

    async def _generate_proactive_content_and_send(self, action_result: Dict[str, Any]):
        """
        获取实时信息，构建最终的生成提示词，并生成和发送主动回复。

        Args:
            action_result (Dict[str, Any]): 规划器返回的动作结果。
        """
        try:
            topic = action_result.get("action_data", {}).get("topic", "随便聊聊")
            logger.info(f"{self.context.log_prefix} 主动思考确定主题: '{topic}'")

            # 1. 获取日程信息
            schedule_block = "你今天没有日程安排。"
            if global_config.planning_system.schedule_enable:
                if current_activity := schedule_manager.get_current_activity():
                    schedule_block = f"你当前正在：{current_activity}。"

            # 2. 网络搜索
            news_block = "暂时没有获取到最新资讯。"
            try:
                web_search_tool = tool_api.get_tool_instance("web_search")
                if web_search_tool:
                    tool_args = {"query": topic, "max_results": 10}
                    # 调用工具，并传递参数
                    search_result_dict = await web_search_tool.execute(**tool_args)
                    if search_result_dict and not search_result_dict.get("error"):
                        news_block = search_result_dict.get("content", "未能提取有效资讯。")
                    else:
                        logger.warning(f"{self.context.log_prefix} 网络搜索返回错误: {search_result_dict.get('error')}")
                else:
                    logger.warning(f"{self.context.log_prefix} 未找到 web_search 工具实例。")
            except Exception as e:
                logger.error(f"{self.context.log_prefix} 主动思考时网络搜索失败: {e}")

            # 3. 获取最新的聊天上下文
            message_list = get_raw_msg_before_timestamp_with_chat(
                chat_id=self.context.stream_id,
                timestamp=time.time(),
                limit=int(global_config.chat.max_context_size * 0.3),
            )
            chat_context_block, _ = build_readable_messages_with_id(messages=message_list)

            # 4. 构建最终的生成提示词
            bot_name = global_config.bot.nickname
            identity_block = f"你的名字是{bot_name}，你{global_config.personality.personality_core}："
            mood_block = f"你现在的心情是：{mood_manager.get_mood_by_chat_id(self.context.stream_id).mood_state}"

            final_prompt = f"""
# 主动对话生成

## 你的角色
{identity_block}

## 你的心情
{mood_block}

## 你今天的日程安排
{schedule_block}

## 关于你准备讨论的话题“{topic}”的最新信息
{news_block}

## 最近的聊天内容
{chat_context_block}

## 任务
你之前决定要发起一个关于“{topic}”的对话。现在，请结合以上所有信息，自然地开启这个话题。

## 要求
- 你的发言要听起来像是自发的，而不是在念报告。
- 巧妙地将日程安排或最新信息融入到你的开场白中。
- 风格要符合你的角色设定。
- 直接输出你想要说的内容，不要包含其他额外信息。
"""

            # 5. 调用生成器API并发送
            response_text = await generator_api.generate_response_custom(
                chat_stream=self.context.chat_stream,
                prompt=final_prompt,
                request_type="chat.replyer.proactive",
            )

            if response_text:
                # 将纯文本包装成 ResponseSet 格式
                response_set = [{"type": "text", "data": {"text": response_text}}]
                await self.cycle_processor.response_handler.send_response(
                    response_set, time.time(), action_result.get("action_message")
                )
            else:
                logger.error(f"{self.context.log_prefix} 主动思考生成回复失败。")

        except Exception as e:
            logger.error(f"{self.context.log_prefix} 生成主动回复内容时异常: {e}")
            logger.error(traceback.format_exc())
