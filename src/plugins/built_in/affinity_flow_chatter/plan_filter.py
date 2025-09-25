"""
PlanFilter: 接收 Plan 对象，根据不同模式的逻辑进行筛选，决定最终要执行的动作。
"""

import orjson
import time
import traceback
import re
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
from src.plugin_system.base.component_types import ActionInfo, ChatMode, ChatType
from src.schedule.schedule_manager import schedule_manager

logger = get_logger("plan_filter")

SAKURA_PINK = "\033[38;5;175m"
SKY_BLUE = "\033[38;5;117m"
RESET_COLOR = "\033[0m"


class ChatterPlanFilter:
    """
    根据 Plan 中的模式和信息，筛选并决定最终的动作。
    """

    def __init__(self, chat_id: str, available_actions: List[str]):
        """
        初始化动作计划筛选器。

        Args:
            chat_id (str): 当前聊天的唯一标识符。
            available_actions (List[str]): 当前可用的动作列表。
        """
        self.chat_id = chat_id
        self.available_actions = available_actions
        self.planner_llm = LLMRequest(model_set=model_config.model_task_config.planner, request_type="planner")
        self.last_obs_time_mark = 0.0

    async def filter(self, reply_not_available: bool, plan: Plan) -> Plan:
        """
        执行筛选逻辑，并填充 Plan 对象的 decided_actions 字段。
        """
        try:
            prompt, used_message_id_list = await self._build_prompt(plan)
            plan.llm_prompt = prompt

            llm_content, _ = await self.planner_llm.generate_response_async(prompt=prompt)

            if llm_content:
                try:
                    parsed_json = orjson.loads(repair_json(llm_content))
                except orjson.JSONDecodeError:
                    parsed_json = {
                        "thinking": "",
                        "actions": {"action_type": "no_action", "reason": "返回内容无法解析为JSON"},
                    }

                if "reply" in plan.available_actions and reply_not_available:
                    # 如果reply动作不可用，但llm返回的仍然有reply，则改为no_reply
                    if (
                        isinstance(parsed_json, dict)
                        and parsed_json.get("actions", {}).get("action_type", "") == "reply"
                    ):
                        parsed_json["actions"]["action_type"] = "no_reply"
                    elif isinstance(parsed_json, list):
                        for item in parsed_json:
                            if isinstance(item, dict) and item.get("actions", {}).get("action_type", "") == "reply":
                                item["actions"]["action_type"] = "no_reply"
                                item["actions"]["reason"] += " (但由于兴趣度不足，reply动作不可用，已改为no_reply)"

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
                        thinking = item.get("thinking", "未提供思考过程")
                        actions_obj = item.get("actions", {})
                        
                        # 处理actions字段可能是字典或列表的情况
                        if isinstance(actions_obj, dict):
                            action_type = actions_obj.get("action_type", "no_action")
                        elif isinstance(actions_obj, list) and actions_obj:
                            # 如果是列表，取第一个元素的action_type
                            first_action = actions_obj[0]
                            if isinstance(first_action, dict):
                                action_type = first_action.get("action_type", "no_action")
                            else:
                                action_type = "no_action"
                        else:
                            action_type = "no_action"

                        if action_type in reply_action_types:
                            if not reply_action_added:
                                final_actions.extend(
                                    await self._parse_single_action(item, used_message_id_list, plan)
                                )
                                reply_action_added = True
                        else:
                            # 非回复类动作直接添加
                            final_actions.extend(await self._parse_single_action(item, used_message_id_list, plan))
                        
                        if thinking and thinking != "未提供思考过程":
                            logger.info(f"\n{SAKURA_PINK}思考: {thinking}{RESET_COLOR}\n")
                        plan.decided_actions = self._filter_no_actions(final_actions)

        except Exception as e:
            logger.error(f"筛选 Plan 时出错: {e}\n{traceback.format_exc()}")
            plan.decided_actions = [ActionPlannerInfo(action_type="no_action", reasoning=f"筛选时出错: {e}")]

        # 在返回最终计划前，打印将要执行的动作
        action_types = [action.action_type for action in plan.decided_actions]
        logger.info(f"选择动作: [{SKY_BLUE}{', '.join(action_types) if action_types else '无'}{RESET_COLOR}]")

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

                chat_content_block, message_id_list = build_readable_messages_with_id(
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

            # 构建已读/未读历史消息
            read_history_block, unread_history_block, message_id_list = await self._build_read_unread_history_blocks(
                plan
            )

            # 为了兼容性，保留原有的chat_content_block
            chat_content_block, _ = build_readable_messages_with_id(
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
            else:  # normal Mode
                no_action_block = """重要说明：
- 'reply' 表示只进行普通聊天回复，不执行任何额外动作
- 其他action表示在普通回复的基础上，执行相应的额外动作
{{
    "action": "reply",
    "target_message_id":"触发action的消息id",
    "reason":"回复的原因"
}}"""

            is_group_chat = plan.chat_type == ChatType.GROUP
            chat_context_description = "你现在正在一个群聊中"
            if not is_group_chat and plan.target_info:
                chat_target_name = plan.target_info.get("person_name") or plan.target_info.get("user_nickname") or "对方"
                chat_context_description = f"你正在和 {chat_target_name} 私聊"

            action_options_block = await self._build_action_options(plan.available_actions)

            moderation_prompt_block = "请不要输出违法违规内容，不要输出色情，暴力，政治相关内容，如有敏感内容，请规避。"

            custom_prompt_block = ""
            if global_config.custom_prompt.planner_custom_prompt_content:
                custom_prompt_block = global_config.custom_prompt.planner_custom_prompt_content

            users_in_chat_str = ""  # TODO: Re-implement user list fetching if needed

            planner_prompt_template = await global_prompt_manager.get_prompt_async("planner_prompt")
            prompt = planner_prompt_template.format(
                schedule_block=schedule_block,
                mood_block=mood_block,
                time_block=time_block,
                chat_context_description=chat_context_description,
                read_history_block=read_history_block,
                unread_history_block=unread_history_block,
                actions_before_now_block=actions_before_now_block,
                mentioned_bonus=mentioned_bonus,
                no_action_block=no_action_block,
                action_options_text=action_options_block,
                moderation_prompt=moderation_prompt_block,
                identity_block=identity_block,
                custom_prompt_block=custom_prompt_block,
                bot_name=bot_name,
                users_in_chat=users_in_chat_str,
            )
            return prompt, message_id_list
        except Exception as e:
            logger.error(f"构建 Planner 提示词时出错: {e}")
            logger.error(traceback.format_exc())
            return "构建 Planner Prompt 时出错", []

    async def _build_read_unread_history_blocks(self, plan: Plan) -> tuple[str, str, list]:
        """构建已读/未读历史消息块"""
        try:
            # 从message_manager获取真实的已读/未读消息
            from src.chat.message_manager.message_manager import message_manager
            from src.chat.utils.utils import assign_message_ids
            from src.chat.utils.chat_message_builder import get_raw_msg_before_timestamp_with_chat

            # 获取聊天流的上下文
            stream_context = message_manager.stream_contexts.get(plan.chat_id)

            # 获取真正的已读和未读消息
            read_messages = stream_context.history_messages  # 已读消息存储在history_messages中
            if not read_messages:
                from src.common.data_models.database_data_model import DatabaseMessages
                # 如果内存中没有已读消息（比如刚启动），则从数据库加载最近的上下文
                fallback_messages_dicts = get_raw_msg_before_timestamp_with_chat(
                    chat_id=plan.chat_id,
                    timestamp=time.time(),
                    limit=global_config.chat.max_context_size,
                )
                # 将字典转换为DatabaseMessages对象
                read_messages = [DatabaseMessages(**msg_dict) for msg_dict in fallback_messages_dicts]

            unread_messages = stream_context.get_unread_messages()  # 获取未读消息

            # 构建已读历史消息块
            if read_messages:
                read_content, read_ids = build_readable_messages_with_id(
                    messages=[msg.flatten() for msg in read_messages[-50:]],  # 限制数量
                    timestamp_mode="normal_no_YMD",
                    truncate=False,
                    show_actions=False,
                )
                read_history_block = f"{read_content}"
            else:
                read_history_block = "暂无已读历史消息"

            # 构建未读历史消息块（包含兴趣度）
            if unread_messages:
                # 扁平化未读消息用于计算兴趣度和格式化
                flattened_unread = [msg.flatten() for msg in unread_messages]

                # 尝试获取兴趣度评分（返回以真实 message_id 为键的字典）
                interest_scores = await self._get_interest_scores_for_messages(flattened_unread)

                # 为未读消息分配短 id（保持与 build_readable_messages_with_id 的一致结构）
                message_id_list = assign_message_ids(flattened_unread)

                unread_lines = []
                for idx, msg in enumerate(flattened_unread):
                    mapped = message_id_list[idx]
                    synthetic_id = mapped.get("id")
                    original_msg_id = msg.get("message_id") or msg.get("id")
                    msg_time = time.strftime("%H:%M:%S", time.localtime(msg.get("time", time.time())))
                    user_nickname = msg.get("user_nickname", "未知用户")
                    msg_content = msg.get("processed_plain_text", "")

                    # 不再显示兴趣度，但保留合成ID供模型内部使用
                    # 同时，为了让模型更好地理解上下文，我们显示用户名
                    unread_lines.append(f"<{synthetic_id}> {msg_time} {user_nickname}: {msg_content}")

                unread_history_block = "\n".join(unread_lines)
            else:
                unread_history_block = "暂无未读历史消息"

            return read_history_block, unread_history_block, message_id_list

        except Exception as e:
            logger.error(f"构建已读/未读历史消息块时出错: {e}")
            return "构建已读历史消息时出错", "构建未读历史消息时出错", []

    async def _get_interest_scores_for_messages(self, messages: List[dict]) -> dict[str, float]:
        """为消息获取兴趣度评分"""
        interest_scores = {}

        try:
            from src.plugins.built_in.affinity_flow_chatter.interest_scoring import (
                chatter_interest_scoring_system as interest_scoring_system,
            )
            from src.common.data_models.database_data_model import DatabaseMessages

            # 转换消息格式
            db_messages = []
            for msg_dict in messages:
                try:
                    db_msg = DatabaseMessages(
                        message_id=msg_dict.get("message_id", ""),
                        time=msg_dict.get("time", time.time()),
                        chat_id=msg_dict.get("chat_id", ""),
                        processed_plain_text=msg_dict.get("processed_plain_text", ""),
                        user_id=msg_dict.get("user_id", ""),
                        user_nickname=msg_dict.get("user_nickname", ""),
                        user_platform=msg_dict.get("platform", "qq"),
                        chat_info_group_id=msg_dict.get("group_id", ""),
                        chat_info_group_name=msg_dict.get("group_name", ""),
                        chat_info_group_platform=msg_dict.get("platform", "qq"),
                    )
                    db_messages.append(db_msg)
                except Exception as e:
                    logger.warning(f"转换消息格式失败: {e}")
                    continue

            # 计算兴趣度评分
            if db_messages:
                bot_nickname = global_config.bot.nickname or "麦麦"
                scores = await interest_scoring_system.calculate_interest_scores(db_messages, bot_nickname)

                # 构建兴趣度字典
                for score in scores:
                    interest_scores[score.message_id] = score.total_score

        except Exception as e:
            logger.warning(f"获取兴趣度评分失败: {e}")

        return interest_scores

    async def _parse_single_action(
        self, action_json: dict, message_id_list: list, plan: Plan
    ) -> List[ActionPlannerInfo]:
        parsed_actions = []
        try:
            # 从新的actions结构中获取动作信息
            actions_obj = action_json.get("actions", {})
            
            # 处理actions字段可能是字典或列表的情况
            actions_to_process = []
            if isinstance(actions_obj, dict):
                actions_to_process.append(actions_obj)
            elif isinstance(actions_obj, list):
                actions_to_process.extend(actions_obj)

            if not actions_to_process:
                 actions_to_process.append({"action_type": "no_action", "reason": "actions格式错误"})

            for single_action_obj in actions_to_process:
                if not isinstance(single_action_obj, dict):
                    continue

                action = single_action_obj.get("action_type", "no_action")
                reasoning = single_action_obj.get("reasoning", "未提供原因") # 兼容旧的reason字段
                action_data = single_action_obj.get("action_data", {})
                
                # 为了向后兼容，如果action_data不存在，则从顶层字段获取
                if not action_data:
                    action_data = {k: v for k, v in single_action_obj.items() if k not in ["action_type", "reason", "reasoning", "thinking"]}

                # 保留原始的thinking字段（如果有）
                thinking = action_json.get("thinking", "")
                if thinking and thinking != "未提供思考过程":
                    action_data["thinking"] = thinking

                target_message_obj = None
                if action not in ["no_action", "no_reply", "do_nothing", "proactive_reply"]:
                    if target_message_id := action_data.get("target_message_id"):
                        target_message_dict = self._find_message_by_id(target_message_id, message_id_list)
                    else:
                        # 如果LLM没有指定target_message_id，进行特殊处理
                        if action == "poke_user":
                            # 对于poke_user，尝试找到触发它的那条戳一戳消息
                            target_message_dict = self._find_poke_notice(message_id_list)
                            if not target_message_dict:
                                # 如果找不到，再使用最新消息作为兜底
                                target_message_dict = self._get_latest_message(message_id_list)
                        else:
                            # 其他动作，默认选择最新的一条消息
                            target_message_dict = self._get_latest_message(message_id_list)

                    if target_message_dict:
                        # 直接使用字典作为action_message，避免DatabaseMessages对象创建失败
                        target_message_obj = target_message_dict
                        # 替换action_data中的临时ID为真实ID
                        if "target_message_id" in action_data:
                            real_message_id = target_message_dict.get("message_id") or target_message_dict.get("id")
                            if real_message_id:
                                action_data["target_message_id"] = real_message_id
                        
                        # 确保 action_message 中始终有 message_id 字段
                        if "message_id" not in target_message_obj and "id" in target_message_obj:
                            target_message_obj["message_id"] = target_message_obj["id"]
                    else:
                        # 如果找不到目标消息，对于reply动作来说这是必需的，应该记录警告
                        if action == "reply":
                            logger.warning(
                                f"reply动作找不到目标消息，target_message_id: {action_data.get('target_message_id')}"
                            )
                            # 将reply动作改为no_action，避免后续执行时出错
                            action = "no_action"
                            reasoning = f"找不到目标消息进行回复。原始理由: {reasoning}"

                if (
                    action not in ["no_action", "no_reply", "reply", "do_nothing", "proactive_reply"]
                    and action not in plan.available_actions
                ):
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

    def _filter_no_actions(self, action_list: List[ActionPlannerInfo]) -> List[ActionPlannerInfo]:
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
            # 构建参数的JSON示例
            params_json_list = []
            if action_info.action_parameters:
                for p_name, p_desc in action_info.action_parameters.items():
                    # 为参数描述添加一个通用示例值
                    if action_name == "set_emoji_like" and p_name == "emoji":
                        # 特殊处理set_emoji_like的emoji参数
                        from plugins.set_emoji_like.qq_emoji_list import qq_face
                        emoji_options = [re.search(r"\[表情：(.+?)\]", name).group(1) for name in qq_face.values() if re.search(r"\[表情：(.+?)\]", name)]
                        example_value = f"<从'{', '.join(emoji_options[:10])}...'中选择一个>"
                    else:
                        example_value = f"<{p_desc}>"
                    params_json_list.append(f'        "{p_name}": "{example_value}"')
            
            # 基础动作信息
            action_description = action_info.description
            action_require = "\n".join(f"- {req}" for req in action_info.action_require)

            # 构建完整的JSON使用范例
            json_example_lines = [
                "    {",
                f'        "action_type": "{action_name}"',
            ]
            # 将参数列表合并到JSON示例中
            if params_json_list:
                # 移除最后一行的逗号
                json_example_lines.extend([line.rstrip(',') for line in params_json_list])

            json_example_lines.append('        "reason": "<执行该动作的详细原因>"')
            json_example_lines.append("    }")
            
            # 使用逗号连接内部元素，除了最后一个
            json_parts = []
            for i, line in enumerate(json_example_lines):
                # "{" 和 "}" 不需要逗号
                if line.strip() in ["{", "}"]:
                    json_parts.append(line)
                    continue
                
                # 检查是否是最后一个需要逗号的元素
                is_last_item = True
                for next_line in json_example_lines[i+1:]:
                    if next_line.strip() not in ["}"]:
                        is_last_item = False
                        break
                
                if not is_last_item:
                    json_parts.append(f"{line},")
                else:
                    json_parts.append(line)

            json_example = "\n".join(json_parts)

            # 使用新的、更详细的action_prompt模板
            using_action_prompt = await global_prompt_manager.get_prompt_async("action_prompt_with_example")
            action_options_block += using_action_prompt.format(
                action_name=action_name,
                action_description=action_description,
                action_require=action_require,
                json_example=json_example,
            )
        return action_options_block

    def _find_message_by_id(self, message_id: str, message_id_list: list) -> Optional[Dict[str, Any]]:
        # 兼容多种 message_id 格式：数字、m123、buffered-xxxx
        # 如果是纯数字，补上 m 前缀以兼容旧格式
        candidate_ids = {message_id}
        if message_id.isdigit():
            candidate_ids.add(f"m{message_id}")

        # 如果是 m 开头且后面是数字，尝试去掉 m 前缀的数字形式
        if message_id.startswith("m") and message_id[1:].isdigit():
            candidate_ids.add(message_id[1:])

        # 逐项匹配 message_id_list（每项可能为 {'id':..., 'message':...}）
        for item in message_id_list:
            # 支持 message_id_list 中直接是字符串/ID 的情形
            if isinstance(item, str):
                if item in candidate_ids:
                    # 没有 message 对象，返回None
                    return None
                continue

            if not isinstance(item, dict):
                continue

            item_id = item.get("id")
            # 直接匹配分配的短 id
            if item_id and item_id in candidate_ids:
                return item.get("message")

            # 有时 message 存储里会有原始的 message_id 字段（如 buffered-xxxx）
            message_obj = item.get("message")
            if isinstance(message_obj, dict):
                orig_mid = message_obj.get("message_id") or message_obj.get("id")
                if orig_mid and orig_mid in candidate_ids:
                    return message_obj

        # 作为兜底，尝试在 message_id_list 中找到 message.message_id 匹配
        for item in message_id_list:
            if isinstance(item, dict) and isinstance(item.get("message"), dict):
                mid = item["message"].get("message_id") or item["message"].get("id")
                if mid == message_id:
                    return item["message"]

        return None

    @staticmethod
    def _get_latest_message(message_id_list: list) -> Optional[Dict[str, Any]]:
        if not message_id_list:
            return None
        return message_id_list[-1].get("message")

    def _find_poke_notice(self, message_id_list: list) -> Optional[Dict[str, Any]]:
        """在消息列表中寻找戳一戳的通知消息"""
        for item in reversed(message_id_list):
            message = item.get("message")
            if (
                isinstance(message, dict)
                and message.get("type") == "notice"
                and "戳" in message.get("processed_plain_text", "")
            ):
                return message
        return None
