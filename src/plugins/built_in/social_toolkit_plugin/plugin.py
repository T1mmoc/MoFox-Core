import re
from typing import List, Tuple, Type, Optional

from src.plugin_system import (
    BasePlugin,
    register_plugin,
    BaseAction,
    ComponentInfo,
    ActionActivationType,
    ConfigField,
)
from src.common.logger import get_logger
from .qq_emoji_list import qq_face
from src.plugin_system.base.component_types import ChatType
from src.person_info.person_info import get_person_info_manager
from dateutil.parser import parse as parse_datetime
from src.manager.async_task_manager import AsyncTask, async_task_manager
from src.plugin_system.apis import send_api, llm_api, generator_api
from src.plugin_system.base.component_types import ComponentType
from typing import Optional
from src.chat.message_receive.chat_stream import ChatStream
import asyncio
import datetime

logger = get_logger("set_emoji_like_plugin")

# ============================ AsyncTask ============================


class ReminderTask(AsyncTask):
    def __init__(
        self,
        delay: float,
        stream_id: str,
        group_id: Optional[str],
        is_group: bool,
        target_user_id: str,
        target_user_name: str,
        event_details: str,
        creator_name: str,
        chat_stream: ChatStream,
    ):
        super().__init__(task_name=f"ReminderTask_{target_user_id}_{datetime.datetime.now().timestamp()}")
        self.delay = delay
        self.stream_id = stream_id
        self.group_id = group_id
        self.is_group = is_group
        self.target_user_id = target_user_id
        self.target_user_name = target_user_name
        self.event_details = event_details
        self.creator_name = creator_name
        self.chat_stream = chat_stream

    async def run(self):
        try:
            if self.delay > 0:
                logger.info(f"等待 {self.delay:.2f} 秒后执行提醒...")
                await asyncio.sleep(self.delay)

            logger.info(f"执行提醒任务: 给 {self.target_user_name} 发送关于 '{self.event_details}' 的提醒")

            extra_info = f"现在是提醒时间，请你以一种符合你人设的、俏皮的方式提醒 {self.target_user_name}。\n提醒内容: {self.event_details}\n设置提醒的人: {self.creator_name}"
            last_message = self.chat_stream.context_manager.context.get_last_message()
            reply_message_dict = last_message.flatten() if last_message else None
            success, reply_set, _ = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                extra_info=extra_info,
                reply_message=reply_message_dict,
                request_type="plugin.reminder.remind_message",
            )

            if success and reply_set:
                for i, (_, text) in enumerate(reply_set):
                    if self.is_group:
                        message_payload = []
                        if i == 0:
                            message_payload.append({"type": "at", "data": {"qq": self.target_user_id}})
                        message_payload.append({"type": "text", "data": {"text": f" {text}"}})
                        await send_api.adapter_command_to_stream(
                            action="send_group_msg",
                            params={"group_id": self.group_id, "message": message_payload},
                            stream_id=self.stream_id,
                        )
                    else:
                        await send_api.text_to_stream(text=text, stream_id=self.stream_id)
            else:
                # Fallback message
                reminder_text = f"叮咚！这是 {self.creator_name} 让我准时提醒你的事情：\n\n{self.event_details}"
                if self.is_group:
                    message_payload = [
                        {"type": "at", "data": {"qq": self.target_user_id}},
                        {"type": "text", "data": {"text": f" {reminder_text}"}},
                    ]
                    await send_api.adapter_command_to_stream(
                        action="send_group_msg",
                        params={"group_id": self.group_id, "message": message_payload},
                        stream_id=self.stream_id,
                    )
                else:
                    await send_api.text_to_stream(text=reminder_text, stream_id=self.stream_id)

            logger.info(f"提醒任务 {self.task_name} 成功完成。")

        except Exception as e:
            logger.error(f"执行提醒任务 {self.task_name} 时出错: {e}", exc_info=True)


# =============================== Actions ===============================


def get_emoji_id(emoji_input: str) -> str | None:
    """根据输入获取表情ID"""
    # 如果输入本身就是数字ID，直接返回
    if emoji_input.isdigit() or (isinstance(emoji_input, str) and emoji_input.startswith("😊")):
        if emoji_input in qq_face:
            return emoji_input

    # 尝试从 "[表情：xxx]" 格式中提取
    match = re.search(r"\[表情：(.+?)\]", emoji_input)
    if match:
        emoji_name = match.group(1).strip()
    else:
        emoji_name = emoji_input.strip()

    # 遍历查找
    for key, value in qq_face.items():
        # value 的格式是 "[表情：xxx]"
        if f"[表情：{emoji_name}]" == value:
            return key

    return None


# ===== Action组件 =====


class PokeAction(BaseAction):
    """发送戳一戳动作"""

    # === 基本信息（必须填写）===
    action_name = "poke_user"
    action_description = "向用户发送戳一戳"
    activation_type = ActionActivationType.LLM_JUDGE
    parallel_action = True

    # === 功能描述（必须填写）===
    action_parameters = {
        "user_name": "需要戳一戳的用户的名字 (可选)",
        "user_id": "需要戳一戳的用户的ID (可选，优先级更高)",
        "times": "需要戳一戳的次数 (默认为 1)",
    }
    action_require = ["当需要戳某个用户时使用", "当你想提醒特定用户时使用"]
    llm_judge_prompt = """
    判定是否需要使用戳一戳动作的条件：
    1. **关键**: 这是一个高消耗的动作，请仅在绝对必要时使用，例如用户明确要求或作为提醒的关键部分。请极其谨慎地使用。
    2. **用户请求**: 用户明确要求使用戳一戳。
    3. **互动提醒**: 你想以一种有趣的方式提醒或与某人互动，但请确保这是对话的自然延伸，而不是无故打扰。
    4. **上下文需求**: 上下文明确需要你戳一个或多个人。
    5. **频率限制**: 如果最近已经戳过，或者用户情绪不高，请绝对不要使用。
    6.  **核心原则**：
        *   这是一个**强打扰**且**高消耗**的动作。
        *   **禁止**在模糊情境下使用。
    请严格根据上述规则，回答“是”或“否”。
    """
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        """执行戳一戳的动作"""
        user_id = self.action_data.get("user_id")
        user_name = self.action_data.get("user_name")

        try:
            times = int(self.action_data.get("times", 1))
            if times > 3:
                times = 3
        except (ValueError, TypeError):
            times = 1

        # 优先使用 user_id
        if not user_id:
            if not user_name:
                logger.warning("戳一戳动作缺少 'user_id' 或 'user_name' 参数。")
                return False, "缺少用户标识参数"

            # 备用方案：通过 user_name 查找
            user_info = await get_person_info_manager().get_person_info_by_name(user_name)
            if not user_info or not user_info.get("user_id"):
                logger.info(f"找不到名为 '{user_name}' 的用户。")
                return False, f"找不到名为 '{user_name}' 的用户"
            user_id = user_info.get("user_id")

        display_name = user_name or user_id

        for i in range(times):
            logger.info(f"正在向 {display_name} ({user_id}) 发送第 {i + 1}/{times} 次戳一戳...")
            await self.send_command(
                "SEND_POKE", args={"qq_id": user_id}, display_message=f"戳了戳 {display_name} ({i + 1}/{times})"
            )
            # 添加一个小的延迟，以避免发送过快
            await asyncio.sleep(0.5)

        success_message = f"已向 {display_name} 发送 {times} 次戳一戳。"
        await self.store_action_info(
            action_build_into_prompt=True, action_prompt_display=success_message, action_done=True
        )
        return True, success_message


class SetEmojiLikeAction(BaseAction):
    """设置消息表情回应"""

    # === 基本信息（必须填写）===
    action_name = "set_emoji_like"
    action_description = "为某条已经存在的消息添加‘贴表情’回应（类似点赞），而不是发送新消息。可以在觉得某条消息非常有趣、值得赞同或者需要特殊情感回应时主动使用。"
    activation_type = ActionActivationType.ALWAYS  # 消息接收时激活(?)
    chat_type_allow = ChatType.GROUP
    parallel_action = True

    # === 功能描述（必须填写）===
    # 从 qq_face 字典中提取所有表情名称用于提示
    emoji_options = []
    for name in qq_face.values():
        match = re.search(r"\[表情：(.+?)\]", name)
        if match:
            emoji_options.append(match.group(1))

    action_parameters = {
        "set": "是否设置回应 (True/False)",
    }
    action_require = [
        "当需要对一个已存在消息进行‘贴表情’回应时使用",
        "这是一个对旧消息的操作，而不是发送新消息",
        "如果你想发送一个新的表情包消息，请使用 'emoji' 动作",
    ]
    llm_judge_prompt = """
    判定是否需要使用贴表情动作的条件：
    1. 用户明确要求使用贴表情包
    2. 这是一个适合表达强烈情绪的场合
    3. 不要发送太多表情包，如果你已经发送过多个表情包则回答"否"
    
    请回答"是"或"否"。
    """
    associated_types = ["text"]

    async def execute(self) -> Tuple[bool, str]:
        """执行设置表情回应的动作"""
        message_id = None
        set_like = self.action_data.get("set", True)
        if self.has_action_message:
            logger.debug(str(self.action_message))
            if isinstance(self.action_message, dict):
                message_id = self.action_message.get("message_id")
            logger.info(f"获取到的消息ID: {message_id}")
        else:
            logger.error("未提供消息ID")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"执行了set_emoji_like动作：{self.action_name},失败: 未提供消息ID",
                action_done=False,
            )
            return False, "未提供消息ID"
        available_models = llm_api.get_available_models()
        if "utils_small" not in available_models:
                logger.error("未找到 'utils_small' 模型配置，无法选择表情")
                return False, "表情选择功能配置错误"

        model_to_use = available_models["utils_small"]
            
        # 获取最近的对话历史作为上下文
        context_text = ""
        if self.action_message:
            context_text = self.action_message.get("processed_plain_text", "")
        else:
            logger.error("无法找到动作选择的原始消息")
            return False, "无法找到动作选择的原始消息"
        
        prompt = (
                f"根据以下这条消息，从列表中选择一个最合适的表情名称来回应这条消息。\n"
                f"消息内容: '{context_text}'\n"
                f"可用表情列表: {', '.join(self.emoji_options)}\n"
                f"你的任务是：只输出你选择的表情的名称，不要包含任何其他文字或标点。\n"
                f"例如，如果觉得应该用'赞'，就只输出'赞'。"
            )

        success, response, _, _ = await llm_api.generate_with_model(
                prompt, model_config=model_to_use, request_type="plugin.set_emoji_like.select_emoji"
            )

        if not success or not response:
                logger.error("二级LLM未能选择有效的表情。")
                return False, "无法找到合适的表情。"

        chosen_emoji_name = response.strip()
        logger.info(f"二级LLM选择的表情是: '{chosen_emoji_name}'")
        emoji_id = get_emoji_id(chosen_emoji_name)

        if not emoji_id:
                logger.error(f"二级LLM选择的表情 '{chosen_emoji_name}' 仍然无法匹配到有效的表情ID。")
                await self.store_action_info(
                    action_build_into_prompt=True,
                    action_prompt_display=f"执行了set_emoji_like动作：{self.action_name},失败: 找不到表情: '{chosen_emoji_name}'",
                    action_done=False,
                )
                return False, f"找不到表情: '{chosen_emoji_name}'。"

        # 4. 使用适配器API发送命令
        if not message_id:
            logger.error("未提供消息ID")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"执行了set_emoji_like动作：{self.action_name},失败: 未提供消息ID",
                action_done=False,
            )
            return False, "未提供消息ID"

        try:
            # 使用适配器API发送贴表情命令
            success = await self.send_command(
                command_name="set_emoji_like",
                args={"message_id": message_id, "emoji_id": emoji_id, "set": set_like},
                storage_message=False,
            )
            if success:
                logger.info("设置表情回应成功")
                await self.store_action_info(
                    action_build_into_prompt=True,
                    action_prompt_display=f"执行了set_emoji_like动作,{chosen_emoji_name},设置表情回应: {emoji_id}, 是否设置: {set_like}",
                    action_done=True,
                )
                return True, "成功设置表情回应"
            else:
                logger.error("设置表情回应失败")
                await self.store_action_info(
                    action_build_into_prompt=True,
                    action_prompt_display=f"执行了set_emoji_like动作：{self.action_name},失败",
                    action_done=False,
                )
                return False, "设置表情回应失败"

        except Exception as e:
            logger.error(f"设置表情回应失败: {e}")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"执行了set_emoji_like动作：{self.action_name},失败: {e}",
                action_done=False,
            )
            return False, f"设置表情回应失败: {e}"


class RemindAction(BaseAction):
    """一个能从对话中智能识别并设置定时提醒的动作。"""

    # === 基本信息 ===
    action_name = "set_reminder"
    action_description = "根据用户的对话内容，智能地设置一个未来的提醒事项。"
    activation_type = ActionActivationType.KEYWORD
    activation_keywords = ["提醒", "叫我", "记得", "别忘了"]
    chat_type_allow = ChatType.ALL
    parallel_action = True

    # === LLM 判断与参数提取 ===
    llm_judge_prompt = ""
    action_parameters = {}
    action_require = [
        "当用户请求在未来的某个时间点提醒他/她或别人某件事时使用",
        "适用于包含明确时间信息和事件描述的对话",
        "例如：'10分钟后提醒我收快递'、'明天早上九点喊一下李四参加晨会'",
    ]

    async def execute(self) -> Tuple[bool, str]:
        """执行设置提醒的动作"""
        user_name = self.action_data.get("user_name")
        remind_time_str = self.action_data.get("remind_time")
        event_details = self.action_data.get("event_details")

        if not all([user_name, remind_time_str, event_details]):
            missing_params = [
                p
                for p, v in {
                    "user_name": user_name,
                    "remind_time": remind_time_str,
                    "event_details": event_details,
                }.items()
                if not v
            ]
            error_msg = f"缺少必要的提醒参数: {', '.join(missing_params)}"
            logger.warning(f"[ReminderPlugin] LLM未能提取完整参数: {error_msg}")
            return False, error_msg

        # 1. 解析时间
        try:
            assert isinstance(remind_time_str, str)
            # 优先尝试直接解析
            try:
                target_time = parse_datetime(remind_time_str, fuzzy=True)
            except Exception:
                # 如果直接解析失败，调用 LLM 进行转换
                logger.info(f"[ReminderPlugin] 直接解析时间 '{remind_time_str}' 失败，尝试使用 LLM 进行转换...")

                # 获取所有可用的模型配置
                available_models = llm_api.get_available_models()
                if "utils_small" not in available_models:
                    raise ValueError("未找到 'utils_small' 模型配置，无法解析时间")

                # 明确使用 'planner' 模型
                model_to_use = available_models["utils_small"]

                # 在执行时动态获取当前时间
                current_time_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                prompt = (
                    f"请将以下自然语言时间短语转换为一个未来的、标准的 'YYYY-MM-DD HH:MM:SS' 格式。"
                    f"请只输出转换后的时间字符串，不要包含任何其他说明或文字。\n"
                    f"作为参考，当前时间是: {current_time_str}\n"
                    f"需要转换的时间短语是: '{remind_time_str}'\n"
                    f"规则:\n"
                    f"- 如果用户没有明确指出是上午还是下午，请根据当前时间判断。例如，如果当前是上午，用户说‘8点’，则应理解为今天的8点；如果当前是下午，用户说‘8点’，则应理解为今天的20点。\n"
                    f"- 如果转换后的时间早于当前时间，则应理解为第二天的时间。\n"
                    f"示例:\n"
                    f"- 当前时间: 2025-09-16 10:00:00, 用户说: '8点' -> '2025-09-17 08:00:00'\n"
                    f"- 当前时间: 2025-09-16 14:00:00, 用户说: '8点' -> '2025-09-16 20:00:00'\n"
                    f"- 当前时间: 2025-09-16 23:00:00, 用户说: '晚上10点' -> '2025-09-17 22:00:00'"
                )

                success, response, _, _ = await llm_api.generate_with_model(
                    prompt, model_config=model_to_use, request_type="plugin.reminder.time_parser"
                )

                if not success or not response:
                    raise ValueError(f"LLM未能返回有效的时间字符串: {response}")

                converted_time_str = response.strip()
                logger.info(f"[ReminderPlugin] LLM 转换结果: '{converted_time_str}'")
                target_time = parse_datetime(converted_time_str, fuzzy=False)

        except Exception as e:
            logger.error(f"[ReminderPlugin] 无法解析或转换时间字符串 '{remind_time_str}': {e}", exc_info=True)
            await self.send_text(f"抱歉，我无法理解您说的时间 '{remind_time_str}'，提醒设置失败。")
            return False, f"无法解析时间 '{remind_time_str}'"

        now = datetime.datetime.now()
        if target_time <= now:
            await self.send_text("提醒时间必须是一个未来的时间点哦，提醒设置失败。")
            return False, "提醒时间必须在未来"

        delay_seconds = (target_time - now).total_seconds()

        # 2. 解析用户
        person_manager = get_person_info_manager()
        user_id_to_remind = None
        user_name_to_remind = ""

        assert isinstance(user_name, str)

        if user_name.strip() in ["自己", "我", "me"]:
            user_id_to_remind = self.user_id
            user_name_to_remind = self.user_nickname
        else:
            # 1. 精确匹配
            user_info = await person_manager.get_person_info_by_name(user_name)

            # 2. 包含匹配
            if not user_info:
                for person_id, name in person_manager.person_name_list.items():
                    if user_name in name:
                        user_info = await person_manager.get_values(person_id, ["user_id", "user_nickname"])
                        break

            # 3. 模糊匹配 (此处简化为字符串相似度)
            if not user_info:
                best_match = None
                highest_similarity = 0
                for person_id, name in person_manager.person_name_list.items():
                    import difflib

                    similarity = difflib.SequenceMatcher(None, user_name, name).ratio()
                    if similarity > highest_similarity:
                        highest_similarity = similarity
                        best_match = person_id

                if best_match and highest_similarity > 0.6:  # 相似度阈值
                    user_info = await person_manager.get_values(best_match, ["user_id", "user_nickname"])

            if not user_info or not user_info.get("user_id"):
                logger.warning(f"[ReminderPlugin] 找不到名为 '{user_name}' 的用户")
                await self.send_text(f"抱歉，我的联系人里找不到叫做 '{user_name}' 的人，提醒设置失败。")
                return False, f"用户 '{user_name}' 不存在"
            user_id_to_remind = user_info.get("user_id")
            user_name_to_remind = user_info.get("user_nickname") or user_name

        # 3. 创建并调度异步任务
        try:
            assert user_id_to_remind is not None
            assert event_details is not None

            reminder_task = ReminderTask(
                delay=delay_seconds,
                stream_id=self.chat_stream.stream_id,
                group_id=self.chat_stream.group_info.group_id
                if self.is_group and self.chat_stream.group_info
                else None,
                is_group=self.is_group,
                target_user_id=str(user_id_to_remind),
                target_user_name=str(user_name_to_remind),
                event_details=str(event_details),
                creator_name=str(self.user_nickname),
                chat_stream=self.chat_stream,
            )
            await async_task_manager.add_task(reminder_task)

            # 4. 生成并发送确认消息
            extra_info = f"你已经成功设置了一个提醒，请以一种符合你人设的、俏皮的方式回复用户。\n提醒时间: {target_time.strftime('%Y-%m-%d %H:%M:%S')}\n提醒对象: {user_name_to_remind}\n提醒内容: {event_details}"
            last_message = self.chat_stream.context_manager.context.get_last_message()
            reply_message_dict = last_message.flatten() if last_message else None
            success, reply_set, _ = await generator_api.generate_reply(
                chat_stream=self.chat_stream,
                extra_info=extra_info,
                reply_message=reply_message_dict,
                request_type="plugin.reminder.confirm_message",
            )
            if success and reply_set:
                for _, text in reply_set:
                    await self.send_text(text)
            else:
                # Fallback message
                fallback_message = f"好的，我记下了。\n将在 {target_time.strftime('%Y-%m-%d %H:%M:%S')} 提醒 {user_name_to_remind}：\n{event_details}"
                await self.send_text(fallback_message)

            return True, "提醒设置成功"
        except Exception as e:
            logger.error(f"[ReminderPlugin] 创建提醒任务时出错: {e}", exc_info=True)
            await self.send_text("抱歉，设置提醒时发生了一点内部错误。")
            return False, "设置提醒时发生内部错误"


# ===== 插件注册 =====
@register_plugin
class SetEmojiLikePlugin(BasePlugin):
    """一个集合多种实用功能的插件，旨在提升聊天体验和效率。"""

    # 插件基本信息
    plugin_name: str = "social_toolkit_plugin"  # 内部标识符
    enable_plugin: bool = True
    dependencies: List[str] = []  # 插件依赖列表
    python_dependencies: List[str] = []  # Python包依赖列表，现在使用内置API
    config_file_name: str = "config.toml"  # 配置文件名

    # 配置节描述
    config_section_descriptions = {"plugin": "插件基本信息", "components": "插件组件"}

    # 配置Schema定义
    config_schema: dict = {
        "plugin": {
            "name": ConfigField(type=str, default="set_emoji_like", description="插件名称"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "config_version": ConfigField(type=str, default="1.1", description="配置版本"),
        },
        "components": {
            "action_set_emoji_like": ConfigField(type=bool, default=True, description="是否启用设置表情回应功能"),
            "action_poke_enable": ConfigField(type=bool, default=True, description="是否启用戳一戳功能"),
            "action_set_reminder_enable": ConfigField(type=bool, default=True, description="是否启用定时提醒功能"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        enable_components = []
        if self.get_config("components.action_set_emoji_like"):
            enable_components.append((SetEmojiLikeAction.get_action_info(), SetEmojiLikeAction))
        if self.get_config("components.action_poke_enable"):
            enable_components.append((PokeAction.get_action_info(), PokeAction))
        if self.get_config("components.action_set_reminder_enable"):
            enable_components.append((RemindAction.get_action_info(), RemindAction))
        return enable_components
