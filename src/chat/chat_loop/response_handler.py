import time
import random
from typing import Dict, Any, Tuple

from src.common.logger import get_logger
from src.plugin_system.apis import send_api, message_api, database_api
from src.person_info.person_info import get_person_info_manager
from .hfc_context import HfcContext

# 导入反注入系统

# 日志记录器
logger = get_logger("hfc")
anti_injector_logger = get_logger("anti_injector")


class ResponseHandler:
    """
    响应处理器类，负责生成和发送机器人的回复。
    """
    def __init__(self, context: HfcContext):
        """
        初始化响应处理器

        Args:
            context: HFC聊天上下文对象

        功能说明:
        - 负责生成和发送机器人的回复
        - 处理回复的格式化和发送逻辑
        - 管理回复状态和日志记录
        """
        self.context = context

    async def generate_and_send_reply(
        self,
        response_set,
        reply_to_str,
        loop_start_time,
        action_message,
        cycle_timers: Dict[str, float],
        thinking_id,
        plan_result,
    ) -> Tuple[Dict[str, Any], str, Dict[str, float]]:
        """
        生成并发送回复的主方法

        Args:
            response_set: 生成的回复内容集合
            reply_to_str: 回复目标字符串
            loop_start_time: 循环开始时间
            action_message: 动作消息数据
            cycle_timers: 循环计时器
            thinking_id: 思考ID
            plan_result: 规划结果

        Returns:
            tuple: (循环信息, 回复文本, 计时器信息)

        功能说明:
        - 发送生成的回复内容
        - 存储动作信息到数据库
        - 构建并返回完整的循环信息
        - 用于上级方法的状态跟踪
        """
        reply_text = await self.send_response(response_set, loop_start_time, action_message)

        person_info_manager = get_person_info_manager()

        # 获取平台信息
        platform = "default"
        if self.context.chat_stream:
            platform = (
                action_message.get("chat_info_platform")
                or action_message.get("user_platform")
                or self.context.chat_stream.platform
            )

        # 获取用户信息并生成回复提示
        user_id = action_message.get("user_id", "")
        person_id = person_info_manager.get_person_id(platform, user_id)
        person_name = await person_info_manager.get_value(person_id, "person_name")
        action_prompt_display = f"你对{person_name}进行了回复：{reply_text}"

        # 存储动作信息到数据库
        await database_api.store_action_info(
            chat_stream=self.context.chat_stream,
            action_build_into_prompt=False,
            action_prompt_display=action_prompt_display,
            action_done=True,
            thinking_id=thinking_id,
            action_data={"reply_text": reply_text, "reply_to": reply_to_str},
            action_name="reply",
        )

        # 构建循环信息
        loop_info: Dict[str, Any] = {
            "loop_plan_info": {
                "action_result": plan_result.get("action_result", {}),
            },
            "loop_action_info": {
                "action_taken": True,
                "reply_text": reply_text,
                "command": "",
                "taken_time": time.time(),
            },
        }

        return loop_info, reply_text, cycle_timers

    async def send_response(self, reply_set, thinking_start_time, message_data) -> str:
        """
        发送回复内容的具体实现

        Args:
            reply_set: 回复内容集合，包含多个回复段
            reply_to: 回复目标
            thinking_start_time: 思考开始时间
            message_data: 消息数据

        Returns:
            str: 完整的回复文本

        功能说明:
        - 检查是否有新消息需要回复
        - 处理主动思考的"沉默"决定
        - 根据消息数量决定是否添加回复引用
        - 逐段发送回复内容，支持打字效果
        - 正确处理元组格式的回复段
        """
        current_time = time.time()
        # 计算新消息数量
        new_message_count = await message_api.count_new_messages(
            chat_id=self.context.stream_id, start_time=thinking_start_time, end_time=current_time
        )

        # 根据新消息数量决定是否需要引用回复
        need_reply = new_message_count >= random.randint(2, 4)

        reply_text = ""
        is_proactive_thinking = (message_data.get("message_type") == "proactive_thinking") if message_data else True

        first_replied = False
        for reply_seg in reply_set:
            # 调试日志：验证reply_seg的格式
            logger.debug(f"Processing reply_seg type: {type(reply_seg)}, content: {reply_seg}")

            # 修正：正确处理元组格式 (格式为: (type, content))
            if isinstance(reply_seg, tuple) and len(reply_seg) >= 2:
                _, data = reply_seg
            else:
                # 向下兼容：如果已经是字符串，则直接使用
                data = str(reply_seg)

            if isinstance(data, list):
                data = "".join(map(str, data))
            reply_text += data

            # 如果是主动思考且内容为“沉默”，则不发送
            if is_proactive_thinking and data.strip() == "沉默":
                logger.info(f"{self.context.log_prefix} 主动思考决定保持沉默，不发送消息")
                continue

            # 发送第一段回复
            if not first_replied:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=self.context.stream_id,
                    reply_to_message=message_data,
                    set_reply=need_reply,
                    typing=False,
                )
                first_replied = True
            else:
                # 发送后续回复
                sent_message = await send_api.text_to_stream(
                    text=data,
                    stream_id=self.context.stream_id,
                    reply_to_message=None,
                    set_reply=False,
                    typing=True,
                )

        return reply_text
