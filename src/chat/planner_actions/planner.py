import json
import time
import traceback
from typing import Dict, Any, Optional, Tuple, List
from rich.traceback import install
from datetime import datetime
from json_repair import repair_json

from src.llm_models.utils_model import LLMRequest
from src.config.config import global_config, model_config
from src.common.logger import get_logger
from src.chat.utils.prompt_builder import Prompt, global_prompt_manager
from src.chat.utils.chat_message_builder import (
    build_readable_actions,
    get_actions_by_timestamp_with_chat,
    build_readable_messages_with_id,
    get_raw_msg_before_timestamp_with_chat,
)
from src.chat.utils.utils import get_chat_type_and_target_info
from src.chat.planner_actions.action_manager import ActionManager
from src.chat.message_receive.chat_stream import get_chat_manager
from src.plugin_system.base.component_types import ActionInfo, ChatMode, ComponentType
from src.plugin_system.core.component_registry import component_registry
from src.manager.schedule_manager import schedule_manager
from src.mood.mood_manager import mood_manager
from src.chat.memory_system.Hippocampus import hippocampus_manager
logger = get_logger("planner")

install(extra_lines=3)


def init_prompt():
    Prompt(
"""
{schedule_block}
{mood_block}
{time_block}
{identity_block}

{custom_prompt_block}
{chat_context_description}，以下是具体的聊天内容
{chat_content_block}

{moderation_prompt}

现在请你根据聊天内容和用户的最新消息选择合适的action和触发action的消息:
{actions_before_now_block}

{no_action_block}

动作：reply
动作描述：参与聊天回复，发送文本进行表达
- 你想要闲聊或者随便附和
- 有人提到了你，但是你还没有回应
- {mentioned_bonus}
- 如果你刚刚进行了回复，不要对同一个话题重复回应
{{
    "action": "reply",
    "target_message_id":"想要回复的消息id",
    "reason":"回复的原因"
}}

{action_options_text}

你必须从上面列出的可用action中选择一个，并说明触发action的消息id（不是消息原文）和选择该action的原因。消息id格式:m+数字

请根据动作示例，以严格的 JSON 格式输出，且仅包含 JSON 内容：
""",
        "planner_prompt",
    )

    Prompt(
"""
# 主动思考决策

## 你的内部状态
{time_block}
{identity_block}
{schedule_block}
{mood_block}

## 长期记忆摘要
{long_term_memory_block}

## 任务
基于以上所有信息，分析当前情况，决定是否需要主动做些什么。
如果你认为不需要，就选择 'do_nothing'。

## 可用动作
{action_options_text}

你必须从上面列出的可用action中选择一个。
请以严格的 JSON 格式输出，且仅包含 JSON 内容：
""",
        "proactive_planner_prompt",
    )

    Prompt(
        """
动作：{action_name}
动作描述：{action_description}
{action_require}
{{
    "action": "{action_name}",{action_parameters},
    "target_message_id":"触发action的消息id",
    "reason":"触发action的原因"
}}
""",
        "action_prompt",
    )


class ActionPlanner:
    def __init__(self, chat_id: str, action_manager: ActionManager):
        self.chat_id = chat_id
        self.log_prefix = f"[{get_chat_manager().get_stream_name(chat_id) or chat_id}]"
        self.action_manager = action_manager
        # LLM规划器配置
        self.planner_llm = LLMRequest(
            model_set=model_config.model_task_config.planner, request_type="planner"
        )  # 用于动作规划

        self.last_obs_time_mark = 0.0
        # 添加重试计数器
        self.plan_retry_count = 0
        self.max_plan_retries = 3

    async def _get_long_term_memory_context(self) -> str:
        """
        获取长期记忆上下文
        """
        try:
            # 1. 生成时间相关的关键词
            now = datetime.now()
            keywords = ["今天", "日程", "计划"]
            if 5 <= now.hour < 12:
                keywords.append("早上")
            elif 12 <= now.hour < 18:
                keywords.append("中午")
            else:
                keywords.append("晚上")

            # TODO: 添加与聊天对象相关的关键词

            # 2. 调用 hippocampus_manager 检索记忆
            retrieved_memories = await hippocampus_manager.get_memory_from_topic(
                valid_keywords=keywords,
                max_memory_num=5,
                max_memory_length=1
            )

            if not retrieved_memories:
                return "最近没有什么特别的记忆。"

            # 3. 格式化记忆
            memory_statements = []
            for topic, memory_item in retrieved_memories:
                memory_statements.append(f"关于'{topic}', 你记得'{memory_item}'。")
            
            return " ".join(memory_statements)
        except Exception as e:
            logger.error(f"获取长期记忆时出错: {e}")
            return "回忆时出现了一些问题。"

    async def _build_action_options(self, current_available_actions: Dict[str, ActionInfo], mode: ChatMode, target_prompt: str = "") -> str:
        """
        构建动作选项
        """
        action_options_block = ""

        if mode == ChatMode.PROACTIVE:
            action_options_block += """动作：do_nothing
动作描述：保持沉默，不主动发起任何动作或对话。
- 当你分析了所有信息后，觉得当前不是一个发起互动的好时机时
{{
    "action": "do_nothing",
    "reason":"决定保持沉默的具体原因"
}}

"""
        for action_name, action_info in current_available_actions.items():
            # TODO: 增加一个字段来判断action是否支持在PROACTIVE模式下使用
            
            param_text = ""
            if action_info.action_parameters:
                param_text = "\n" + "\n".join(f'    "{p_name}":"{p_desc}"' for p_name, p_desc in action_info.action_parameters.items())
            
            require_text = "\n".join(f"- {req}" for req in action_info.action_require)

            using_action_prompt = await global_prompt_manager.get_prompt_async("action_prompt")
            action_options_block += using_action_prompt.format(
                action_name=action_name,
                action_description=action_info.description,
                action_parameters=param_text,
                action_require=require_text,
                target_prompt=target_prompt,
            )
        return action_options_block

    def find_message_by_id(self, message_id: str, message_id_list: list) -> Optional[Dict[str, Any]]:
        """
        根据message_id从message_id_list中查找对应的原始消息

        Args:
            message_id: 要查找的消息ID
            message_id_list: 消息ID列表，格式为[{'id': str, 'message': dict}, ...]

        Returns:
            找到的原始消息字典，如果未找到则返回None
        """
        for item in message_id_list:
            if item.get("id") == message_id:
                return item.get("message")
        return None

    def get_latest_message(self, message_id_list: list) -> Optional[Dict[str, Any]]:
        """
        获取消息列表中的最新消息
        
        Args:
            message_id_list: 消息ID列表，格式为[{'id': str, 'message': dict}, ...]
            
        Returns:
            最新的消息字典，如果列表为空则返回None
        """
        if not message_id_list:
            return None
        # 假设消息列表是按时间顺序排列的，最后一个是最新的
        return message_id_list[-1].get("message")

    async def plan(
        self, mode: ChatMode = ChatMode.FOCUS
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """
        规划器 (Planner): 使用LLM根据上下文决定做出什么动作。
        """

        action = "no_reply"  # 默认动作
        reasoning = "规划器初始化默认"
        action_data = {}
        current_available_actions: Dict[str, ActionInfo] = {}
        target_message: Optional[Dict[str, Any]] = None  # 初始化target_message变量
        prompt: str = ""
        message_id_list: list = []

        try:
            is_group_chat, chat_target_info, current_available_actions = self.get_necessary_info()

            # --- 构建提示词 (调用修改后的 PromptBuilder 方法) ---
            prompt, message_id_list = await self.build_planner_prompt(
                is_group_chat=is_group_chat,  # <-- Pass HFC state
                chat_target_info=chat_target_info,  # <-- 传递获取到的聊天目标信息
                current_available_actions=current_available_actions,  # <-- Pass determined actions
                mode=mode,
                refresh_time=True,
            )

            # --- 调用 LLM (普通文本生成) ---
            llm_content = None
            try:
                llm_content, (reasoning_content, _, _) = await self.planner_llm.generate_response_async(prompt=prompt)

                if global_config.debug.show_prompt:
                    logger.info(f"{self.log_prefix}规划器原始提示词: {prompt}")
                    logger.info(f"{self.log_prefix}规划器原始响应: {llm_content}")
                    if reasoning_content:
                        logger.info(f"{self.log_prefix}规划器推理: {reasoning_content}")
                else:
                    logger.debug(f"{self.log_prefix}规划器原始提示词: {prompt}")
                    logger.debug(f"{self.log_prefix}规划器原始响应: {llm_content}")
                    if reasoning_content:
                        logger.debug(f"{self.log_prefix}规划器推理: {reasoning_content}")

            except Exception as req_e:
                logger.error(f"{self.log_prefix}LLM 请求执行失败: {req_e}")
                reasoning = f"LLM 请求失败，模型出现问题: {req_e}"
                action = "no_reply"

            if llm_content:
                try:
                    parsed_json = json.loads(repair_json(llm_content))

                    if isinstance(parsed_json, list):
                        if parsed_json:
                            parsed_json = parsed_json[-1]
                            logger.warning(f"{self.log_prefix}LLM返回了多个JSON对象，使用最后一个: {parsed_json}")
                        else:
                            parsed_json = {}

                    if not isinstance(parsed_json, dict):
                        logger.error(f"{self.log_prefix}解析后的JSON不是字典类型: {type(parsed_json)}")
                        parsed_json = {}

                    action = parsed_json.get("action", "no_reply")
                    reasoning = parsed_json.get("reason", "未提供原因")

                    # 将所有其他属性添加到action_data
                    for key, value in parsed_json.items():
                        if key not in ["action", "reasoning"]:
                            action_data[key] = value

                    # 非no_reply动作需要target_message_id
                    if action != "no_reply":
                        if target_message_id := parsed_json.get("target_message_id"):
                            if isinstance(target_message_id, int):
                                target_message_id = str(target_message_id)
                            
                            if isinstance(target_message_id, str) and not target_message_id.startswith('m'):
                                target_message_id = f"m{target_message_id}"
                            # 根据target_message_id查找原始消息
                            target_message = self.find_message_by_id(target_message_id, message_id_list)
                            # 如果获取的target_message为None，输出warning并重新plan
                            if target_message is None:
                                self.plan_retry_count += 1
                                logger.warning(f"{self.log_prefix}无法找到target_message_id '{target_message_id}' 对应的消息，重试次数: {self.plan_retry_count}/{self.max_plan_retries}")
                                
                                # 如果连续三次plan均为None，输出error并选取最新消息
                                if self.plan_retry_count >= self.max_plan_retries:
                                    logger.error(f"{self.log_prefix}连续{self.max_plan_retries}次plan获取target_message失败，选择最新消息作为target_message")
                                    target_message = self.get_latest_message(message_id_list)
                                    self.plan_retry_count = 0  # 重置计数器
                                else:
                                    # 递归重新plan
                                    return await self.plan(mode, loop_start_time, available_actions)
                            else:
                                # 成功获取到target_message，重置计数器
                                self.plan_retry_count = 0
                        else:
                            logger.warning(f"{self.log_prefix}动作'{action}'缺少target_message_id")
                    
                    

                    if action == "no_action":
                        reasoning = "normal决定不使用额外动作"
                    elif mode == ChatMode.PROACTIVE and action == "do_nothing":
                        pass  # 在PROACTIVE模式下，do_nothing是有效动作
                    elif action != "no_reply" and action != "reply" and action not in current_available_actions:
                        logger.warning(
                            f"{self.log_prefix}LLM 返回了当前不可用或无效的动作: '{action}' (可用: {list(current_available_actions.keys())})，将强制使用 'no_reply'"
                        )
                        reasoning = f"LLM 返回了当前不可用的动作 '{action}' (可用: {list(current_available_actions.keys())})。原始理由: {reasoning}"
                        action = "no_reply"
                        
                        # 检查no_reply是否可用，如果不可用则使用reply作为终极回退
                        if "no_reply" not in current_available_actions:
                            if "reply" in current_available_actions:
                                action = "reply"
                                reasoning += " (no_reply不可用，使用reply作为回退)"
                                logger.warning(f"{self.log_prefix}no_reply不可用，使用reply作为回退")
                            else:
                                # 如果连reply都不可用，使用第一个可用的动作
                                if current_available_actions:
                                    action = list(current_available_actions.keys())[0]
                                    reasoning += f" (no_reply和reply都不可用，使用{action}作为回退)"
                                    logger.warning(f"{self.log_prefix}no_reply和reply都不可用，使用{action}作为回退")
                                else:
                                    # 如果没有任何可用动作，这是一个严重错误
                                    logger.error(f"{self.log_prefix}没有任何可用动作，系统状态异常")
                                    action = "no_reply"  # 仍然尝试no_reply，让上层处理
                    
                    # 对no_reply动作本身也进行可用性检查
                    elif action == "no_reply" and "no_reply" not in current_available_actions:
                        if "reply" in current_available_actions:
                            action = "reply"
                            reasoning = f"no_reply不可用，自动回退到reply。原因: {reasoning}"
                            logger.warning(f"{self.log_prefix}no_reply不可用，自动回退到reply")
                        elif current_available_actions:
                            action = list(current_available_actions.keys())[0]
                            reasoning = f"no_reply不可用，自动回退到{action}。原因: {reasoning}"
                            logger.warning(f"{self.log_prefix}no_reply不可用，自动回退到{action}")
                        else:
                            logger.error(f"{self.log_prefix}没有任何可用动作，保持no_reply让上层处理")

                except Exception as json_e:
                    logger.warning(f"{self.log_prefix}解析LLM响应JSON失败 {json_e}. LLM原始输出: '{llm_content}'")
                    traceback.print_exc()
                    reasoning = f"解析LLM响应JSON失败: {json_e}. 将使用默认动作 'no_reply'."
                    action = "no_reply"
                    
                    # 检查no_reply是否可用
                    if "no_reply" not in current_available_actions:
                        if "reply" in current_available_actions:
                            action = "reply"
                            reasoning += " (no_reply不可用，使用reply)"
                        elif current_available_actions:
                            action = list(current_available_actions.keys())[0]
                            reasoning += f" (no_reply不可用，使用{action})"

        except Exception as outer_e:
            logger.error(f"{self.log_prefix}Planner 处理过程中发生意外错误，规划失败，将执行 no_reply: {outer_e}")
            traceback.print_exc()
            action = "no_reply"
            reasoning = f"Planner 内部处理错误: {outer_e}"
            
            # 检查no_reply是否可用
            current_available_actions = self.action_manager.get_using_actions()
            if "no_reply" not in current_available_actions:
                if "reply" in current_available_actions:
                    action = "reply"
                    reasoning += " (no_reply不可用，使用reply)"
                elif current_available_actions:
                    action = list(current_available_actions.keys())[0]
                    reasoning += f" (no_reply不可用，使用{action})"
                else:
                    logger.error(f"{self.log_prefix}严重错误：没有任何可用动作")

        is_parallel = False
        if mode == ChatMode.NORMAL and action in current_available_actions:
            is_parallel = current_available_actions[action].parallel_action
            
            
        action_data["loop_start_time"] = loop_start_time
        
        actions = []
            
        # 1. 添加Planner取得的动作
        actions.append({
            "action_type": action,
            "reasoning": reasoning,
            "timestamp": time.time(),
            "is_parallel": is_parallel,
        }


        return (
            {
                "action_result": action_result,
                "action_prompt": prompt,
            },
            target_message,
        )

    async def build_planner_prompt(
        self,
        is_group_chat: bool,  # Now passed as argument
        chat_target_info: Optional[dict],  # Now passed as argument
        current_available_actions: Dict[str, ActionInfo],
        refresh_time :bool = False,
        mode: ChatMode = ChatMode.FOCUS,
    ) -> tuple[str, list]:  # sourcery skip: use-join
        """构建 Planner LLM 的提示词 (获取模板并填充数据)"""
        try:
            # --- 通用信息获取 ---
            time_block = f"当前时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            bot_name = global_config.bot.nickname
            bot_nickname = f",也有人叫你{','.join(global_config.bot.alias_names)}" if global_config.bot.alias_names else ""
            bot_core_personality = global_config.personality.personality_core
            identity_block = f"你的名字是{bot_name}{bot_nickname}，你{bot_core_personality}："
            
            schedule_block = ""
            if global_config.schedule.enable:
                if current_activity := schedule_manager.get_current_activity():
                    schedule_block = f"你当前正在：{current_activity}。"

            mood_block = ""
            if global_config.mood.enable_mood:
                chat_mood = mood_manager.get_mood_by_chat_id(self.chat_id)
                mood_block = f"你现在的心情是：{chat_mood.mood_state}"

            # --- 根据模式构建不同的Prompt ---
            if mode == ChatMode.PROACTIVE:
                long_term_memory_block = await self._get_long_term_memory_context()
                action_options_text = await self._build_action_options(current_available_actions, mode)
                
                prompt_template = await global_prompt_manager.get_prompt_async("proactive_planner_prompt")
                prompt = prompt_template.format(
                    time_block=time_block,
                    identity_block=identity_block,
                    schedule_block=schedule_block,
                    mood_block=mood_block,
                    long_term_memory_block=long_term_memory_block,
                    action_options_text=action_options_text,
                )
                return prompt, []

            # --- FOCUS 和 NORMAL 模式的逻辑 ---
            message_list_before_now = get_raw_msg_before_timestamp_with_chat(
                chat_id=self.chat_id,
                timestamp=time.time(),
                limit=int(global_config.chat.max_context_size * 0.6),
            )

            chat_content_block, message_id_list = build_readable_messages_with_id(
                messages=message_list_before_now,
                timestamp_mode="normal",
                read_mark=self.last_obs_time_mark,
                truncate=True,
                show_actions=True,
            )

            actions_before_now = get_actions_by_timestamp_with_chat(
                chat_id=self.chat_id,
                timestamp_start=time.time() - 3600,
                timestamp_end=time.time(),
                limit=5,
            )

            actions_before_now_block = build_readable_actions(actions=actions_before_now)
            actions_before_now_block = f"你刚刚选择并执行过的action是：\n{actions_before_now_block}"

            self.last_obs_time_mark = time.time()

            if mode == ChatMode.FOCUS:
                no_action_block = """
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
                by_what = "聊天内容和用户的最新消息"
                target_prompt = ""
                no_action_block = """重要说明：
- 'reply' 表示只进行普通聊天回复，不执行任何额外动作
- 其他action表示在普通回复的基础上，执行相应的额外动作
"""

            chat_context_description = "你现在正在一个群聊中"
            if not is_group_chat and chat_target_info:
                chat_target_name = chat_target_info.get("person_name") or chat_target_info.get("user_nickname") or "对方"
                chat_context_description = f"你正在和 {chat_target_name} 私聊"

            action_options_block = await self._build_action_options(current_available_actions, mode, target_prompt)

            moderation_prompt_block = "请不要输出违法违规内容，不要输出色情，暴力，政治相关内容，如有敏感内容，请规避。"
            
            custom_prompt_block = ""
            if global_config.custom_prompt.planner_custom_prompt_enable and global_config.custom_prompt.planner_custom_prompt_content:
                custom_prompt_block = global_config.custom_prompt.planner_custom_prompt_content

            planner_prompt_template = await global_prompt_manager.get_prompt_async("planner_prompt")
            prompt = planner_prompt_template.format(
                schedule_block=schedule_block,
                mood_block=mood_block,
                time_block=time_block,
                chat_context_description=chat_context_description,
                chat_content_block=chat_content_block,
                actions_before_now_block=actions_before_now_block,
                no_action_block=no_action_block,
                mentioned_bonus=mentioned_bonus,
                action_options_text=action_options_block,
                moderation_prompt=moderation_prompt_block,
                identity_block=identity_block,
                custom_prompt_block=custom_prompt_block,
                bot_name=bot_name,
            )
            return prompt, message_id_list
        except Exception as e:
            logger.error(f"构建 Planner 提示词时出错: {e}")
            logger.error(traceback.format_exc())
            return "构建 Planner Prompt 时出错", []

    def get_necessary_info(self) -> Tuple[bool, Optional[dict], Dict[str, ActionInfo]]:
        """
        获取 Planner 需要的必要信息
        """
        is_group_chat = True
        is_group_chat, chat_target_info = get_chat_type_and_target_info(self.chat_id)
        logger.debug(f"{self.log_prefix}获取到聊天信息 - 群聊: {is_group_chat}, 目标信息: {chat_target_info}")

        current_available_actions_dict = self.action_manager.get_using_actions()

        # 获取完整的动作信息
        all_registered_actions: Dict[str, ActionInfo] = component_registry.get_components_by_type(  # type: ignore
            ComponentType.ACTION
        )
        current_available_actions = {}
        for action_name in current_available_actions_dict:
            if action_name in all_registered_actions:
                current_available_actions[action_name] = all_registered_actions[action_name]
            else:
                logger.warning(f"{self.log_prefix}使用中的动作 {action_name} 未在已注册动作中找到")

        return is_group_chat, chat_target_info, current_available_actions


init_prompt()
