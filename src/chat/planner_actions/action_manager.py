import asyncio
import traceback
import time
import random
from typing import Dict, Optional, Type, Any, Tuple


from src.chat.utils.timer_calculator import Timer
from src.person_info.person_info import get_person_info_manager
from src.chat.message_receive.chat_stream import ChatStream, get_chat_manager
from src.common.logger import get_logger
from src.config.config import global_config
from src.plugin_system.core.component_registry import component_registry
from src.plugin_system.base.component_types import ComponentType, ActionInfo
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.apis import generator_api, database_api, send_api, message_api

logger = get_logger("action_manager")


class ActionManager:
    """
    动作管理器，用于管理各种类型的动作

    现在统一使用新插件系统，简化了原有的新旧兼容逻辑。
    """

    def __init__(self):
        """初始化动作管理器"""

        # 当前正在使用的动作集合，默认加载默认动作
        self._using_actions: Dict[str, ActionInfo] = {}

        # 初始化时将默认动作加载到使用中的动作
        self._using_actions = component_registry.get_default_actions()

        self.log_prefix: str = "ActionManager"

    # === 执行Action方法 ===

    @staticmethod
    def create_action(
            action_name: str,
        action_data: dict,
        reasoning: str,
        cycle_timers: dict,
        thinking_id: str,
        chat_stream: ChatStream,
        log_prefix: str,
        shutting_down: bool = False,
        action_message: Optional[dict] = None,
    ) -> Optional[BaseAction]:
        """
        创建动作处理器实例

        Args:
            action_name: 动作名称
            action_data: 动作数据
            reasoning: 执行理由
            cycle_timers: 计时器字典
            thinking_id: 思考ID
            chat_stream: 聊天流
            log_prefix: 日志前缀
            shutting_down: 是否正在关闭

        Returns:
            Optional[BaseAction]: 创建的动作处理器实例，如果动作名称未注册则返回None
        """
        try:
            # 获取组件类 - 明确指定查询Action类型
            component_class: Type[BaseAction] = component_registry.get_component_class(
                action_name, ComponentType.ACTION
            )  # type: ignore
            if not component_class:
                logger.warning(f"{log_prefix} 未找到Action组件: {action_name}")
                return None

            # 获取组件信息
            component_info = component_registry.get_component_info(action_name, ComponentType.ACTION)
            if not component_info:
                logger.warning(f"{log_prefix} 未找到Action组件信息: {action_name}")
                return None

            # 获取插件配置
            plugin_config = component_registry.get_plugin_config(component_info.plugin_name)

            # 创建动作实例
            instance = component_class(
                action_data=action_data,
                reasoning=reasoning,
                cycle_timers=cycle_timers,
                thinking_id=thinking_id,
                chat_stream=chat_stream,
                log_prefix=log_prefix,
                shutting_down=shutting_down,
                plugin_config=plugin_config,
                action_message=action_message,
            )

            logger.debug(f"创建Action实例成功: {action_name}")
            return instance

        except Exception as e:
            logger.error(f"创建Action实例失败 {action_name}: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return None

    def get_using_actions(self) -> Dict[str, ActionInfo]:
        """获取当前正在使用的动作集合"""
        return self._using_actions.copy()

    # === Modify相关方法 ===
    def remove_action_from_using(self, action_name: str) -> bool:
        """
        从当前使用的动作集中移除指定动作

        Args:
            action_name: 动作名称

        Returns:
            bool: 移除是否成功
        """
        if action_name not in self._using_actions:
            logger.warning(f"移除失败: 动作 {action_name} 不在当前使用的动作集中")
            return False

        del self._using_actions[action_name]
        logger.debug(f"已从使用集中移除动作 {action_name}")
        return True

    def restore_actions(self) -> None:
        """恢复到默认动作集"""
        actions_to_restore = list(self._using_actions.keys())
        self._using_actions = component_registry.get_default_actions()
        logger.debug(f"恢复动作集: 从 {actions_to_restore} 恢复到默认动作集 {list(self._using_actions.keys())}")

    async def execute_action(
        self,
        action_name: str,
        chat_id: str,
        target_message: Optional[dict] = None,
        reasoning: str = "",
        action_data: Optional[dict] = None,
        thinking_id: Optional[str] = None,
        log_prefix: str = "",
    ) -> Any:
        """
        执行单个动作的通用函数

        Args:
            action_name: 动作名称
            chat_id: 聊天id
            target_message: 目标消息
            reasoning: 执行理由
            action_data: 动作数据
            thinking_id: 思考ID
            log_prefix: 日志前缀

        Returns:
            执行结果
        """
        try:
            # 通过chat_id获取chat_stream
            chat_manager = get_chat_manager()
            chat_stream = chat_manager.get_stream(chat_id)

            if not chat_stream:
                logger.error(f"{log_prefix} 无法找到chat_id对应的chat_stream: {chat_id}")
                return {
                    "action_type": action_name,
                    "success": False,
                    "reply_text": "",
                    "error": "chat_stream not found",
                }

            if action_name == "no_action":
                return {"action_type": "no_action", "success": True, "reply_text": "", "command": ""}

            if action_name == "no_reply":
                # 直接处理no_reply逻辑，不再通过动作系统
                reason = reasoning or "选择不回复"
                logger.info(f"{log_prefix} 选择不回复，原因: {reason}")

                # 存储no_reply信息到数据库
                await database_api.store_action_info(
                    chat_stream=chat_stream,
                    action_build_into_prompt=False,
                    action_prompt_display=reason,
                    action_done=True,
                    thinking_id=thinking_id,
                    action_data={"reason": reason},
                    action_name="no_reply",
                )
                return {"action_type": "no_reply", "success": True, "reply_text": "", "command": ""}

            elif action_name != "reply" and action_name != "no_action":
                # 执行普通动作
                success, reply_text, command = await self._handle_action(
                    chat_stream,
                    action_name,
                    reasoning,
                    action_data or {},
                    {},  # cycle_timers
                    thinking_id,
                    target_message,
                )
                return {
                    "action_type": action_name,
                    "success": success,
                    "reply_text": reply_text,
                    "command": command,
                }
            else:
                # 生成回复
                try:
                    success, response_set, _ = await generator_api.generate_reply(
                        chat_stream=chat_stream,
                        reply_message=target_message,
                        available_actions=self.get_using_actions(),
                        enable_tool=global_config.tool.enable_tool,
                        request_type="chat.replyer",
                        from_plugin=False,
                    )
                    if not success or not response_set:
                        logger.info(
                            f"对 {target_message.get('processed_plain_text') if target_message else '未知消息'} 的回复生成失败"
                        )
                        return {"action_type": "reply", "success": False, "reply_text": "", "loop_info": None}
                except asyncio.CancelledError:
                    logger.debug(f"{log_prefix} 并行执行：回复生成任务已被取消")
                    return {"action_type": "reply", "success": False, "reply_text": "", "loop_info": None}

                # 发送并存储回复
                loop_info, reply_text, cycle_timers_reply = await self._send_and_store_reply(
                    chat_stream,
                    response_set,
                    asyncio.get_event_loop().time(),
                    target_message,
                    {},  # cycle_timers
                    thinking_id,
                    [],  # actions
                )
                return {"action_type": "reply", "success": True, "reply_text": reply_text, "loop_info": loop_info}

        except Exception as e:
            logger.error(f"{log_prefix} 执行动作时出错: {e}")
            logger.error(f"{log_prefix} 错误信息: {traceback.format_exc()}")
            return {
                "action_type": action_name,
                "success": False,
                "reply_text": "",
                "loop_info": None,
                "error": str(e),
            }

    async def _handle_action(
        self, chat_stream, action, reasoning, action_data, cycle_timers, thinking_id, action_message
    ) -> tuple[bool, str, str]:
        """
        处理具体的动作执行

        Args:
            chat_stream: ChatStream实例
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
        if not chat_stream:
            return False, "", ""
        try:
            # 创建动作处理器
            action_handler = self.create_action(
                action_name=action,
                action_data=action_data,
                reasoning=reasoning,
                cycle_timers=cycle_timers,
                thinking_id=thinking_id,
                chat_stream=chat_stream,
                log_prefix=self.log_prefix,
                action_message=action_message,
            )
            if not action_handler:
                # 动作处理器创建失败，尝试回退机制
                logger.warning(f"{self.log_prefix} 创建动作处理器失败: {action}，尝试回退方案")

                # 获取当前可用的动作
                available_actions = self.get_using_actions()
                fallback_action = None

                # 回退优先级：reply > 第一个可用动作
                if "reply" in available_actions:
                    fallback_action = "reply"
                elif available_actions:
                    fallback_action = list(available_actions.keys())[0]

                if fallback_action and fallback_action != action:
                    logger.info(f"{self.log_prefix} 使用回退动作: {fallback_action}")
                    action_handler = self.create_action(
                        action_name=fallback_action,
                        action_data=action_data,
                        reasoning=f"原动作'{action}'不可用，自动回退。{reasoning}",
                        cycle_timers=cycle_timers,
                        thinking_id=thinking_id,
                        chat_stream=chat_stream,
                        log_prefix=self.log_prefix,
                        action_message=action_message,
                    )

                if not action_handler:
                    logger.error(f"{self.log_prefix} 回退方案也失败，无法创建任何动作处理器")
                    return False, "", ""

            # 执行动作
            success, reply_text = await action_handler.handle_action()
            return success, reply_text, ""
        except Exception as e:
            logger.error(f"{self.log_prefix} 处理{action}时出错: {e}")
            traceback.print_exc()
            return False, "", ""

    async def _send_and_store_reply(
        self,
        chat_stream: ChatStream,
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
            chat_stream: ChatStream实例
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
            reply_text = await self.send_response(chat_stream, response_set, loop_start_time, action_message)

        # 存储reply action信息
        person_info_manager = get_person_info_manager()

        # 获取 platform，如果不存在则从 chat_stream 获取，如果还是 None 则使用默认值
        platform = action_message.get("chat_info_platform")
        if platform is None:
            platform = getattr(chat_stream, "platform", "unknown")

        # 获取用户信息并生成回复提示
        person_id = person_info_manager.get_person_id(
            platform,
            action_message.get("user_id", ""),
        )
        person_name = await person_info_manager.get_value(person_id, "person_name")
        action_prompt_display = f"你对{person_name}进行了回复：{reply_text}"

        # 存储动作信息到数据库
        await database_api.store_action_info(
            chat_stream=chat_stream,
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

    async def send_response(self, chat_stream, reply_set, thinking_start_time, message_data) -> str:
        """
        发送回复内容的具体实现

        Args:
            chat_stream: ChatStream实例
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
        new_message_count = message_api.count_new_messages(
            chat_id=chat_stream.stream_id, start_time=thinking_start_time, end_time=current_time
        )

        # 根据新消息数量决定是否需要引用回复
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
                logger.info(f"{self.log_prefix} 主动思考决定保持沉默，不发送消息")
                continue

            # 发送第一段回复
            if not first_replied:
                await send_api.text_to_stream(
                    text=data,
                    stream_id=chat_stream.stream_id,
                    reply_to_message=message_data,
                    set_reply=bool(message_data),
                    typing=False,
                )
                first_replied = True
            else:
                # 发送后续回复
                sent_message = await send_api.text_to_stream(
                    text=data,
                    stream_id=chat_stream.stream_id,
                    reply_to_message=None,
                    set_reply=False,
                    typing=True,
                )

        return reply_text
