"""
PlanFilter: 接收 Plan 对象，根据不同模式的逻辑进行筛选，决定最终要执行的动作。
"""
import orjson
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional

from json_repair import repair_json

from src.chat.memory_system.Hippocampus import hippocampus_manager
from src.chat.utils.chat_message_builder import (
    build_readable_actions,
    build_readable_messages_with_id,
    get_actions_by_timestamp_with_chat,
)
from src.chat.utils.prompt import global_prompt_manager
from src.common.data_models.info_data_model import ActionPlannerInfo, Plan
from src.common.logger import get_logger
from src.config.config import global_config, model_config
from src.llm_models.utils_model import LLMRequest
from src.mood.mood_manager import mood_manager
from src.plugin_system.base.component_types import ActionInfo, ChatMode
from src.schedule.schedule_manager import schedule_manager

logger = get_logger("plan_filter")


class PlanFilter:
    """
    根据 Plan 中的模式和信息，筛选并决定最终的动作。
    """

    def __init__(self):
        self.planner_llm = LLMRequest(
            model_set=model_config.model_task_config.planner, request_type="planner"
        )
        self.last_obs_time_mark = 0.0

    async def filter(self, plan: Plan) -> Plan:
        """
        执行筛选逻辑，并填充 Plan 对象的 decided_actions 字段。
        """
        logger.debug(f"墨墨在这里加了日志 -> filter 入口 plan: {plan}")
        try:
            prompt, used_message_id_list = await self._build_prompt(plan)
            plan.llm_prompt = prompt
            logger.info(f"规划器原始提示词: {prompt}")

            llm_content, _ = await self.planner_llm.generate_response_async(prompt=prompt)

            if llm_content:
                logger.info(f"规划器原始返回: {llm_content}")
                parsed_json = orjson.loads(repair_json(llm_content))
                logger.debug(f"墨墨在这里加了日志 -> 解析后的 JSON: {parsed_json}")
                
                if isinstance(parsed_json, dict):
                    parsed_json = [parsed_json]

                if isinstance(parsed_json, list):
                    final_actions = []
                    reply_action_added = False
                    # 定义回复类动作的集合，方便扩展
                    reply_action_types = {"reply", "proactive_reply"}

                    for item in parsed_json:
                        if not isinstance(item, dict):
                            continue

                        # 预解析 action_type 来进行判断
                        action_type = item.get("action", "no_action")

                        if action_type in reply_action_types:
                            if not reply_action_added:
                                final_actions.extend(
                                    await self._parse_single_action(
                                        item, used_message_id_list, plan
                                    )
                                )
                                reply_action_added = True
                        else:
                            # 非回复类动作直接添加
                            final_actions.extend(
                                await self._parse_single_action(
                                    item, used_message_id_list, plan
                                )
                            )
                    
                    plan.decided_actions = self._filter_no_actions(final_actions)

        except Exception as e:
            logger.error(f"筛选 Plan 时出错: {e}\n{traceback.format_exc()}")
            plan.decided_actions = [
                ActionPlannerInfo(action_type="no_action", reasoning=f"筛选时出错: {e}")
            ]
        
        logger.debug(f"墨墨在这里加了日志 -> filter 出口 decided_actions: {plan.decided_actions}")
        return plan

    async def _build_prompt(self, plan: Plan) -> tuple[str, list]:
        """
        根据 Plan 对象构建提示词。
        """
        try:
            time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            bot_name = global_config.bot.nickname
            bot_nickname = (
                f",也有人叫你{','.join(global_config.bot.alias_names)}" if global_config.bot.alias_names else ""
            )
            bot_core_personality = global_config.personality.personality_core
            identity_block = f"你的名字是{bot_name}{bot_nickname}，你{bot_core_personality}："

            schedule_block = ""
            if global_config.planning_system.schedule_enable:
                if current_activity := schedule_manager.get_current_activity():
                    schedule_block = f"你当前正在：{current_activity},但注意它与群聊的聊天无关。"

            mood_block = ""
            if global_config.mood.enable_mood:
                chat_mood = mood_manager.get_mood_by_chat_id(plan.chat_id)
                mood_block = f"你现在的心情是：{chat_mood.mood_state}"

            if plan.mode == ChatMode.PROACTIVE:
                long_term_memory_block = await self._get_long_term_memory_context()
                
                chat_content_block, message_id_list = await build_readable_messages_with_id(
                    messages=[msg.flatten() for msg in plan.chat_history],
                    timestamp_mode="normal",
                    truncate=False,
                    show_actions=False,
                )

                prompt_template = await global_prompt_manager.get_prompt_async("proactive_planner_prompt")
                actions_before_now = await get_actions_by_timestamp_with_chat(
                    chat_id=plan.chat_id,
                    timestamp_start=time.time() - 3600,
                    timestamp_end=time.time(),
                    limit=5,
                )
                actions_before_now_block = build_readable_actions(actions=actions_before_now)
                actions_before_now_block = f"你刚刚选择并执行过的action是：\n{actions_before_now_block}"

                prompt = prompt_template.format(
                    time_block=time_block,
                    identity_block=identity_block,
                    schedule_block=schedule_block,
                    mood_block=mood_block,
                    long_term_memory_block=long_term_memory_block,
                    chat_content_block=chat_content_block or "最近没有聊天内容。",
                    actions_before_now_block=actions_before_now_block,
                )
                return prompt, message_id_list

            chat_content_block, message_id_list = await build_readable_messages_with_id(
                messages=[msg.flatten() for msg in plan.chat_history],
                timestamp_mode="normal",
                read_mark=self.last_obs_time_mark,
                truncate=True,
                show_actions=True,
            )

            actions_before_now = await get_actions_by_timestamp_with_chat(
                chat_id=plan.chat_id,
                timestamp_start=time.time() - 3600,
                timestamp_end=time.time(),
                limit=5,
            )

            actions_before_now_block = build_readable_actions(actions=actions_before_now)
            actions_before_now_block = f"你刚刚选择并执行过的action是：\n{actions_before_now_block}"

            self.last_obs_time_mark = time.time()

            mentioned_bonus = ""
            if global_config.chat.mentioned_bot_inevitable_reply:
                mentioned_bonus = "\n- 有人提到你"
            if global_config.chat.at_bot_inevitable_reply:
                mentioned_bonus = "\n- 有人提到你，或者at你"

            if plan.mode == ChatMode.FOCUS:
                no_action_block = """
动作：no_action
动作描述：不选择任何动作
{{
    "action": "no_action",
    "reason":"不动作的原因"
}}

动作：no_reply
动作描述：不进行回复，等待合适的回复时机
- 当你刚刚发送了消息，没有人回复时，选择no_reply
- 当你一次发送了太多消息，为了避免打扰聊天节奏，选择no_reply
{{
    "action": "no_reply",
    "reason":"不回复的原因"
}}
"""
            else:  # NORMAL Mode
                no_action_block = """重要说明：
- 'reply' 表示只进行普通聊天回复，不执行任何额外动作
- 其他action表示在普通回复的基础上，执行相应的额外动作
{{
    "action": "reply",
    "target_message_id":"触发action的消息id",
    "reason":"回复的原因"
}}"""

            is_group_chat = plan.target_info.platform == "group" if plan.target_info else True
            chat_context_description = "你现在正在一个群聊中"
            if not is_group_chat and plan.target_info:
                chat_target_name = plan.target_info.person_name or plan.target_info.user_nickname or "对方"
                chat_context_description = f"你正在和 {chat_target_name} 私聊"

            action_options_block = await self._build_action_options(plan.available_actions)

            moderation_prompt_block = "请不要输出违法违规内容，不要输出色情，暴力，政治相关内容，如有敏感内容，请规避。"

            custom_prompt_block = ""
            if global_config.custom_prompt.planner_custom_prompt_content:
                custom_prompt_block = global_config.custom_prompt.planner_custom_prompt_content
            
            users_in_chat_str = "" # TODO: Re-implement user list fetching if needed

            planner_prompt_template = await global_prompt_manager.get_prompt_async("planner_prompt")
            prompt = planner_prompt_template.format(
                schedule_block=schedule_block,
                mood_block=mood_block,
                time_block=time_block,
                chat_context_description=chat_context_description,
                chat_content_block=chat_content_block,
                actions_before_now_block=actions_before_now_block,
                mentioned_bonus=mentioned_bonus,
                no_action_block=no_action_block,
                action_options_text=action_options_block,
                moderation_prompt=moderation_prompt_block,
                identity_block=identity_block,
                custom_prompt_block=custom_prompt_block,
                bot_name=bot_name,
                users_in_chat=users_in_chat_str
            )
            return prompt, message_id_list
        except Exception as e:
            logger.error(f"构建 Planner 提示词时出错: {e}")
            logger.error(traceback.format_exc())
            return "构建 Planner Prompt 时出错", []

    async def _parse_single_action(
        self, action_json: dict, message_id_list: list, plan: Plan
    ) -> List[ActionPlannerInfo]:
        parsed_actions = []
        try:
            action = action_json.get("action", "no_action")
            reasoning = action_json.get("reason", "未提供原因")
            action_data = {k: v for k, v in action_json.items() if k not in ["action", "reason"]}

            target_message_obj = None
            if action not in ["no_action", "no_reply", "do_nothing", "proactive_reply"]:
                if target_message_id := action_json.get("target_message_id"):
                    target_message_dict = self._find_message_by_id(target_message_id, message_id_list)
                else:
                    # 如果LLM没有指定target_message_id，我们就默认选择最新的一条消息
                    target_message_dict = self._get_latest_message(message_id_list)

                if target_message_dict:
                    # 直接使用字典作为action_message，避免DatabaseMessages对象创建失败
                    target_message_obj = target_message_dict
                else:
                    # 如果找不到目标消息，对于reply动作来说这是必需的，应该记录警告
                    if action == "reply":
                        logger.warning(f"reply动作找不到目标消息，target_message_id: {action_json.get('target_message_id')}")
                        # 将reply动作改为no_action，避免后续执行时出错
                        action = "no_action"
                        reasoning = f"找不到目标消息进行回复。原始理由: {reasoning}"

            available_action_names = list(plan.available_actions.keys())
            if action not in ["no_action", "no_reply", "reply", "do_nothing", "proactive_reply"] and action not in available_action_names:
                reasoning = f"LLM 返回了当前不可用的动作 '{action}'。原始理由: {reasoning}"
                action = "no_action"

            parsed_actions.append(
                ActionPlannerInfo(
                    action_type=action,
                    reasoning=reasoning,
                    action_data=action_data,
                    action_message=target_message_obj,
                    available_actions=plan.available_actions,
                )
            )
        except Exception as e:
            logger.error(f"解析单个action时出错: {e}")
            parsed_actions.append(
                ActionPlannerInfo(
                    action_type="no_action",
                    reasoning=f"解析action时出错: {e}",
                )
            )
        return parsed_actions

    @staticmethod
    def _filter_no_actions(
            action_list: List[ActionPlannerInfo]
    ) -> List[ActionPlannerInfo]:
        non_no_actions = [a for a in action_list if a.action_type not in ["no_action", "no_reply"]]
        if non_no_actions:
            return non_no_actions
        return action_list[:1] if action_list else []

    @staticmethod
    async def _get_long_term_memory_context() -> str:
        try:
            now = datetime.now()
            keywords = ["今天", "日程", "计划"]
            if 5 <= now.hour < 12:
                keywords.append("早上")
            elif 12 <= now.hour < 18:
                keywords.append("中午")
            else:
                keywords.append("晚上")

            retrieved_memories = await hippocampus_manager.get_memory_from_topic(
                valid_keywords=keywords, max_memory_num=5, max_memory_length=1
            )

            if not retrieved_memories:
                return "最近没有什么特别的记忆。"

            memory_statements = [f"关于'{topic}', 你记得'{memory_item}'。" for topic, memory_item in retrieved_memories]
            return " ".join(memory_statements)
        except Exception as e:
            logger.error(f"获取长期记忆时出错: {e}")
            return "回忆时出现了一些问题。"

    @staticmethod
    async def _build_action_options(current_available_actions: Dict[str, ActionInfo]) -> str:
        action_options_block = ""
        for action_name, action_info in current_available_actions.items():
            param_text = ""
            if action_info.action_parameters:
                param_text = "\n" + "\n".join(
                    f'    "{p_name}":"{p_desc}"' for p_name, p_desc in action_info.action_parameters.items()
                )
            require_text = "\n".join(f"- {req}" for req in action_info.action_require)
            using_action_prompt = await global_prompt_manager.get_prompt_async("action_prompt")
            action_options_block += using_action_prompt.format(
                action_name=action_name,
                action_description=action_info.description,
                action_parameters=param_text,
                action_require=require_text,
            )
        return action_options_block

    @staticmethod
    def _find_message_by_id(message_id: str, message_id_list: list) -> Optional[Dict[str, Any]]:
        if message_id.isdigit():
            message_id = f"m{message_id}"
        for item in message_id_list:
            if item.get("id") == message_id:
                return item.get("message")
        return None

    @staticmethod
    def _get_latest_message(message_id_list: list) -> Optional[Dict[str, Any]]:
        if not message_id_list:
            return None
        return message_id_list[-1].get("message")
