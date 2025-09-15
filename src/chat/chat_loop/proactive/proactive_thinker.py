import time
import traceback
from typing import TYPE_CHECKING, Dict, Any

from src.common.logger import get_logger
from src.plugin_system.base.component_types import ChatMode
from ..hfc_context import HfcContext
from .events import ProactiveTriggerEvent
from src.plugin_system.apis import generator_api
from src.plugin_system.apis.generator_api import process_human_text
from src.schedule.schedule_manager import schedule_manager
from src.plugin_system import tool_api
from src.config.config import global_config
from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat, build_readable_messages_with_id
from src.mood.mood_manager import mood_manager
from src.common.database.sqlalchemy_database_api import store_action_info, db_get
from src.common.database.sqlalchemy_models import Messages

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
                actions, _ = await self.cycle_processor.action_planner.plan(mode=ChatMode.PROACTIVE)
                action_result = actions[0] if actions else {}
                action_type = action_result.get("action_type")

                if action_type == "proactive_reply":
                    await self._generate_proactive_content_and_send(action_result, trigger_event)
                elif action_type not in ["do_nothing", "no_action"]:
                    await self.cycle_processor._handle_action(
                        action=action_result["action_type"],
                        reasoning=action_result.get("reasoning", ""),
                        action_data=action_result.get("action_data", {}),
                        cycle_timers={},
                        thinking_id="",
                        action_message=action_result.get("action_message")
                    )
                else:
                    logger.info(f"{self.context.log_prefix} 主动思考决策: 保持沉默")
        
        except Exception as e:
            logger.error(f"{self.context.log_prefix} 主动思考执行异常: {e}")
            logger.error(traceback.format_exc())
    

    async def _generate_proactive_content_and_send(self, action_result: Dict[str, Any], trigger_event: ProactiveTriggerEvent):
        """
        获取实时信息，构建最终的生成提示词，并生成和发送主动回复。

        Args:
            action_result (Dict[str, Any]): 规划器返回的动作结果。
            trigger_event (ProactiveTriggerEvent): 触发事件。
        """
        try:
            topic = action_result.get("action_data", {}).get("topic", "随便聊聊")
            logger.info(f"{self.context.log_prefix} 主动思考确定主题: '{topic}'")

            schedule_block = "你今天没有日程安排。"
            if global_config.planning_system.schedule_enable:
                if current_activity := schedule_manager.get_current_activity():
                    schedule_block = f"你当前正在：{current_activity}。"

            news_block = "暂时没有获取到最新资讯。"
            if trigger_event.source != "reminder_system":
                # 升级决策模型
                should_search_prompt = f"""
# 搜索决策

## 任务
分析话题“{topic}”，判断它的展开更依赖于“外部信息”还是“内部信息”，并决定是否需要进行网络搜索。

## 判断原则
- **需要搜索 (SEARCH)**：当话题的有效讨论**必须**依赖于现实世界的、客观的、可被检索的外部信息时。这包括但不限于：
    - 新闻时事、公共事件
    - 专业知识、科学概念
    - 天气、股价等实时数据
    - 对具体实体（如电影、书籍、地点）的客观描述查询

- **无需搜索 (SKIP)**：当话题的展开主要依赖于**已有的对话上下文、个人情感、主观体验或社交互动**时。这包括但不限于：
    - 延续之前的对话、追问细节
    - 表达关心、问候或个人感受
    - 分享主观看法或经历
    - 纯粹的社交性互动

## 你的决策
根据以上原则，对“{topic}”这个话题进行分析，并严格输出`SEARCH`或`SKIP`。
"""
                from src.llm_models.utils_model import LLMRequest
                from src.config.config import model_config
                
                decision_llm = LLMRequest(
                    model_set=model_config.model_task_config.planner,
                    request_type="planner"
                )
                
                decision, _ = await decision_llm.generate_response_async(prompt=should_search_prompt)

                if "SEARCH" in decision:
                    try:
                        if topic and topic.strip():
                            web_search_tool = tool_api.get_tool_instance("web_search")
                            if web_search_tool:
                                try:
                                    search_result_dict = await web_search_tool.execute(
                                        function_args={"query": topic, "max_results": 10}
                                    )
                                    if search_result_dict and not search_result_dict.get("error"):
                                        news_block = search_result_dict.get("content", "未能提取有效资讯。")
                                    elif search_result_dict:
                                        logger.warning(f"{self.context.log_prefix} 网络搜索返回错误: {search_result_dict.get('error')}")
                                except Exception as e:
                                    logger.error(f"{self.context.log_prefix} 网络搜索执行失败: {e}")
                            else:
                                logger.warning(f"{self.context.log_prefix} 未找到 web_search 工具实例。")
                        else:
                            logger.warning(f"{self.context.log_prefix} 主题为空，跳过网络搜索。")
                    except Exception as e:
                        logger.error(f"{self.context.log_prefix} 主动思考时网络搜索失败: {e}")
                message_list = get_raw_msg_before_timestamp_with_chat(
                    chat_id=self.context.stream_id,
                    timestamp=time.time(),
                    limit=int(global_config.chat.max_context_size * 0.3),
                )
                chat_context_block, _ = build_readable_messages_with_id(messages=message_list)

            from src.llm_models.utils_model import LLMRequest
            from src.config.config import model_config
            
            bot_name = global_config.bot.nickname
            
            confirmation_prompt = f"""# 主动回复二次确认

## 基本信息
你的名字是{bot_name}，准备主动发起关于"{topic}"的话题。

## 最近的聊天内容
{chat_context_block}

## 合理判断标准
请检查以下条件，如果**所有条件都合理**就可以回复：

1. **回应检查**：检查你（{bot_name}）发送的最后一条消息之后，是否有其他人发言。如果没有，则大概率应该保持沉默。
2. **话题补充**：只有当你认为准备发起的话题是对上一条无人回应消息的**有价值的补充**时，才可以在上一条消息无人回应的情况下继续发言。
3. **时间合理性**：当前时间是否在深夜（凌晨2点-6点）这种不适合主动聊天的时段？
4. **内容价值**：这个话题"{topic}"是否有意义，不是完全无关紧要的内容？
5. **重复避免**：你准备说的话题是否与你自己的上一条消息明显重复？
6. **自然性**：在当前上下文中主动提起这个话题是否自然合理？

## 输出要求
如果判断应该跳过（比如上一条消息无人回应、深夜时段、无意义话题、重复内容），输出：SKIP_PROACTIVE_REPLY
其他情况都应该输出：PROCEED_TO_REPLY

请严格按照上述格式输出，不要添加任何解释。"""

            planner_llm = LLMRequest(
                model_set=model_config.model_task_config.planner,
                request_type="planner"
            )
            
            confirmation_result, _ = await planner_llm.generate_response_async(prompt=confirmation_prompt)
            
            if not confirmation_result or "SKIP_PROACTIVE_REPLY" in confirmation_result:
                logger.info(f"{self.context.log_prefix} 决策模型二次确认决定跳过主动回复")
                return
                
            bot_name = global_config.bot.nickname
            personality = global_config.personality
            identity_block = (
                f"你的名字是{bot_name}。\n"
                f"关于你：{personality.personality_core}，并且{personality.personality_side}。\n"
                f"你的身份是{personality.identity}，平时说话风格是{personality.reply_style}。"
            )
            mood_block = f"你现在的心情是：{mood_manager.get_mood_by_chat_id(self.context.stream_id).mood_state}"

            final_prompt = f"""
## 你的角色
{identity_block}

## 你的心情
{mood_block}

## 你今天的日程安排
{schedule_block}

## 关于你准备讨论的话题"{topic}"的最新信息
{news_block}

## 最近的聊天内容
{chat_context_block}

## 任务
你现在想要主动说些什么。话题是"{topic}"，但这只是一个参考方向。

根据最近的聊天内容，你可以：
- 如果是想关心朋友，就自然地询问他们的情况
- 如果想起了之前的话题，就问问后来怎么样了
- 如果有什么想分享的想法，就自然地开启话题
- 如果只是想闲聊，就随意地说些什么

**重要**：如果获取到了最新的网络信息（news_block不为空），请**自然地**将这些信息融入你的回复中，作为话题的补充或引子，而不是生硬地复述。

## 要求
- 像真正的朋友一样，自然地表达关心或好奇
- 不要过于正式，要口语化和亲切
- 结合你的角色设定，保持温暖的风格
- 直接输出你想说的话，不要解释为什么要说

请输出一条简短、自然的主动发言。
"""

            response_text = await generator_api.generate_response_custom(
                chat_stream=self.context.chat_stream,
                prompt=final_prompt,
                request_type="chat.replyer.proactive",
            )

            if response_text:
                response_set = process_human_text(
                    content=response_text,
                    enable_splitter=global_config.response_splitter.enable,
                    enable_chinese_typo=global_config.chinese_typo.enable,
                )
                await self.cycle_processor.response_handler.send_response(
                    response_set, time.time(), action_result.get("action_message")
                )
                await store_action_info(
                    chat_stream=self.context.chat_stream,
                    action_name="proactive_reply",
                    action_data={"topic": topic, "response": response_text},
                    action_prompt_display=f"主动发起对话: {topic}",
                    action_done=True,
                )
            else:
                logger.error(f"{self.context.log_prefix} 主动思考生成回复失败。")

        except Exception as e:
            logger.error(f"{self.context.log_prefix} 生成主动回复内容时异常: {e}")
            logger.error(traceback.format_exc())
