import time
import traceback
from typing import TYPE_CHECKING

from src.common.logger import get_logger
from src.plugin_system.base.component_types import ChatMode
from ..hfc_context import HfcContext
from .events import ProactiveTriggerEvent
from src.plugin_system.apis import generator_api

if TYPE_CHECKING:
    from ..cycle_processor import CycleProcessor

logger = get_logger("hfc")


class ProactiveThinker:
    def __init__(self, context: HfcContext, cycle_processor: "CycleProcessor"):
        """
        初始化主动思考器

        Args:
            context: HFC聊天上下文对象
            cycle_processor: 循环处理器，用于执行主动思考的结果

        功能说明:
        - 接收主动思考事件并执行思考流程
        - 根据事件类型执行不同的前置操作（如修改情绪）
        - 调用planner进行决策并由cycle_processor执行
        """
        self.context = context
        self.cycle_processor = cycle_processor

    async def think(self, trigger_event: ProactiveTriggerEvent):
        """
        统一的API入口，用于触发主动思考

        Args:
            trigger_event: 描述触发上下文的事件对象
        """
        logger.info(
            f"{self.context.log_prefix} 接收到主动思考事件: "
            f"来源='{trigger_event.source}', 原因='{trigger_event.reason}'"
        )

        try:
            # 1. 根据事件类型执行前置操作
            await self._prepare_for_thinking(trigger_event)

            # 2. 执行核心思考逻辑
            await self._execute_proactive_thinking(trigger_event)

        except Exception as e:
            logger.error(f"{self.context.log_prefix} 主动思考 think 方法执行异常: {e}")
            logger.error(traceback.format_exc())

    async def _prepare_for_thinking(self, trigger_event: ProactiveTriggerEvent):
        """
        根据事件类型，执行思考前的准备工作，例如修改情绪

        Args:
            trigger_event: 触发事件
        """
        if trigger_event.source != "insomnia_manager":
            return

        try:
            from src.mood.mood_manager import mood_manager

            mood_obj = mood_manager.get_mood_by_chat_id(self.context.stream_id)
            new_mood = None

            if trigger_event.reason == "low_pressure":
                new_mood = "精力过剩，毫无睡意"
            elif trigger_event.reason == "random":
                new_mood = "深夜emo，胡思乱想"
            elif trigger_event.reason == "goodnight":
                new_mood = "有点困了，准备睡觉了"

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
        执行主动思考的核心逻辑

        Args:
            trigger_event: 触发事件
        """
        try:
            # 直接调用 planner 的 PROACTIVE 模式
            actions, target_message = await self.cycle_processor.action_planner.plan(mode=ChatMode.PROACTIVE)

            # 获取第一个规划出的动作作为主要决策
            action_result = actions[0] if actions else {}

            # 如果决策不是 do_nothing，则执行
            if action_result and action_result.get("action_type") != "do_nothing":
                if action_result.get("action_type") == "reply":
                    success, response_set, _ = await generator_api.generate_reply(
                        chat_stream=self.context.chat_stream,
                        reply_message=action_result["action_message"],
                        available_actions={},
                        enable_tool=False,
                        request_type="chat.replyer.proactive",
                        from_plugin=False,
                    )
                    if success and response_set:
                        await self.cycle_processor.response_handler.send_response(
                            response_set, time.time(), action_result["action_message"]
                        )
            else:
                logger.info(f"{self.context.log_prefix} 主动思考决策: 保持沉默")

        except Exception as e:
            logger.error(f"{self.context.log_prefix} 主动思考执行异常: {e}")
            logger.error(traceback.format_exc())
