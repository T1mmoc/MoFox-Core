import asyncio
import random
import time
from datetime import datetime
from typing import List, Union, Type, Optional

from maim_message import UserInfo

from src.chat.message_receive.chat_stream import get_chat_manager
from src.common.logger import get_logger
from src.config.config import global_config
from src.manager.async_task_manager import async_task_manager, AsyncTask
from src.plugin_system import EventType, BaseEventHandler
from src.plugin_system.apis import chat_api, person_api
from src.plugin_system.base.base_event import HandlerResult
from .proactive_thinker_executor import ProactiveThinkerExecutor

logger = get_logger(__name__)


class ColdStartTask(AsyncTask):
    """
    冷启动任务，专门用于处理那些在白名单里，但从未与机器人发生过交互的用户。
    它的核心职责是“破冰”，主动创建聊天流并发起第一次问候。
    """

    def __init__(self):
        super().__init__(task_name="ColdStartTask")
        self.chat_manager = get_chat_manager()
        self.executor = ProactiveThinkerExecutor()

    async def run(self):
        """任务主循环，周期性地检查是否有需要“破冰”的新用户。"""
        logger.info("冷启动任务已启动，将周期性检查白名单中的新朋友。")
        # 初始等待一段时间，确保其他服务（如数据库）完全启动
        await asyncio.sleep(20)

        while True:
            try:
                logger.info("【冷启动】开始扫描白名单，寻找从未聊过的用户...")

                # 从全局配置中获取私聊白名单
                enabled_private_chats = global_config.proactive_thinking.enabled_private_chats
                if not enabled_private_chats:
                    logger.debug("【冷启动】私聊白名单为空，任务暂停一小时。")
                    await asyncio.sleep(3600)  # 白名单为空时，没必要频繁检查
                    continue

                # 遍历白名单中的每一个用户
                for chat_id in enabled_private_chats:
                    try:
                        platform, user_id_str = chat_id.split(":")
                        user_id = int(user_id_str)

                        # 【核心逻辑】使用 chat_api 检查该用户是否已经存在聊天流（ChatStream）
                        # 如果返回了 ChatStream 对象，说明已经聊过天了，不是本次任务的目标
                        if chat_api.get_stream_by_user_id(user_id_str, platform):
                            continue  # 跳过已存在的用户

                        logger.info(f"【冷启动】发现白名单新用户 {chat_id}，准备发起第一次问候。")

                        # 【增强体验】尝试从关系数据库中获取该用户的昵称
                        # 这样打招呼时可以更亲切，而不是只知道一个冷冰冰的ID
                        person_id = person_api.get_person_id(platform, user_id)
                        nickname = await person_api.get_person_value(person_id, "nickname")

                        # 如果数据库里有昵称，就用数据库里的；如果没有，就用 "用户+ID" 作为备用
                        user_nickname = nickname or f"用户{user_id}"

                        # 创建 UserInfo 对象，这是创建聊天流的必要信息
                        user_info = UserInfo(platform=platform, user_id=str(user_id), user_nickname=user_nickname)
                        
                        # 【关键步骤】主动创建聊天流。
                        # 创建后，该用户就进入了机器人的“好友列表”，后续将由 ProactiveThinkingTask 接管
                        stream = await self.chat_manager.get_or_create_stream(platform, user_info)

                        await self.executor.execute(stream_id=stream.stream_id, start_mode="cold_start")
                        logger.info(f"【冷启动】已为新用户 {chat_id} (昵称: {user_nickname}) 创建聊天流并发送问候。")

                    except ValueError:
                        logger.warning(f"【冷启动】白名单条目格式错误或用户ID无效，已跳过: {chat_id}")
                    except Exception as e:
                        logger.error(f"【冷启动】处理用户 {chat_id} 时发生未知错误: {e}", exc_info=True)

                # 完成一轮检查后，进入长时休眠
                await asyncio.sleep(3600)

            except asyncio.CancelledError:
                logger.info("冷启动任务被正常取消。")
                break
            except Exception as e:
                logger.error(f"【冷启动】任务出现严重错误，将在5分钟后重试: {e}", exc_info=True)
                await asyncio.sleep(300)


class ProactiveThinkingTask(AsyncTask):
    """
    主动思考的后台任务（日常唤醒），负责在聊天“冷却”后重新活跃气氛。
    它只处理已经存在的聊天流。
    """

    def __init__(self):
        super().__init__(task_name="ProactiveThinkingTask")
        self.chat_manager = get_chat_manager()
        self.executor = ProactiveThinkerExecutor()

    def _get_next_interval(self) -> float:
        """
        动态计算下一次执行的时间间隔，模拟人类行为的随机性。
        结合了基础间隔、随机偏移和每日不同时段的活跃度调整。
        """
        # 从配置中读取基础间隔和随机范围
        base_interval = global_config.proactive_thinking.interval
        sigma = global_config.proactive_thinking.interval_sigma

        # 1. 在 [base - sigma, base + sigma] 范围内随机取一个值
        interval = random.uniform(base_interval - sigma, base_interval + sigma)

        # 2. 根据当前时间，应用活跃度调整因子
        now = datetime.now()
        current_time_str = now.strftime("%H:%M")

        adjust_rules = global_config.proactive_thinking.talk_frequency_adjust
        if adjust_rules and adjust_rules[0]:
            # 按时间对规则排序，确保能找到正确的时间段
            rules = sorted([rule.split(",") for rule in adjust_rules[0][1:]], key=lambda x: x[0])

            factor = 1.0
            # 找到最后一个小于等于当前时间的规则
            for time_str, factor_str in rules:
                if current_time_str >= time_str:
                    factor = float(factor_str)
                else:
                    break  # 后面的时间都比当前晚，无需再找
            # factor > 1 表示更活跃，所以用除法来缩短间隔
            interval /= factor

        # 保证最小间隔，防止过于频繁的骚扰
        return max(60.0, interval)

    async def run(self):
        """任务主循环，周期性地检查所有已存在的聊天是否需要“唤醒”。"""
        logger.info("日常唤醒任务已启动，将根据动态间隔检查聊天活跃度。")
        await asyncio.sleep(15)  # 初始等待

        while True:
            # 计算下一次检查前的休眠时间
            next_interval = self._get_next_interval()
            try:
                logger.debug(f"【日常唤醒】下一次检查将在 {next_interval:.2f} 秒后进行。")
                await asyncio.sleep(next_interval)

                logger.info("【日常唤醒】开始检查不活跃的聊天...")

                # 加载白名单配置
                enabled_private = set(global_config.proactive_thinking.enabled_private_chats)
                enabled_groups = set(global_config.proactive_thinking.enabled_group_chats)

                # 获取当前所有聊天流的快照
                all_streams = list(self.chat_manager.streams.values())

                for stream in all_streams:
                    # 1. 检查该聊天是否在白名单内（或白名单为空时默认允许）
                    is_whitelisted = False
                    if stream.group_info:  # 群聊
                        if not enabled_groups or f"qq:{stream.group_info.group_id}" in enabled_groups:
                            is_whitelisted = True
                    else:  # 私聊
                        if not enabled_private or f"qq:{stream.user_info.user_id}" in enabled_private:
                            is_whitelisted = True

                    if not is_whitelisted:
                        continue  # 不在白名单内，跳过

                    # 2. 【核心逻辑】检查聊天冷却时间是否足够长
                    time_since_last_active = time.time() - stream.last_active_time
                    if time_since_last_active > next_interval:
                        logger.info(f"【日常唤醒】聊天流 {stream.stream_id} 已冷却 {time_since_last_active:.2f} 秒，触发主动对话。")
                        
                        await self.executor.execute(stream_id=stream.stream_id, start_mode="wake_up")
                        
                        # 【关键步骤】在触发后，立刻更新活跃时间并保存。
                        # 这可以防止在同一个检查周期内，对同一个目标因为意外的延迟而发送多条消息。
                        stream.update_active_time()
                        await self.chat_manager._save_stream(stream)

            except asyncio.CancelledError:
                logger.info("日常唤醒任务被正常取消。")
                break
            except Exception as e:
                logger.error(f"【日常唤醒】任务出现错误，将在60秒后重试: {e}", exc_info=True)
                await asyncio.sleep(60)


class ProactiveThinkerEventHandler(BaseEventHandler):
    """主动思考插件的启动事件处理器，负责根据配置启动一个或两个后台任务。"""

    handler_name: str = "proactive_thinker_on_start"
    handler_description: str = "主动思考插件的启动事件处理器"
    init_subscribe: List[Union[EventType, str]] = [EventType.ON_START]

    async def execute(self, kwargs: dict | None) -> "HandlerResult":
        """在机器人启动时执行，根据配置决定是否启动后台任务。"""
        logger.info("检测到插件启动事件，正在初始化【主动思考】插件...")
        # 检查总开关
        if global_config.proactive_thinking.enable:
            # 启动负责“日常唤醒”的核心任务
            logger.info("【主动思考】功能已启用，正在启动“日常唤醒”任务...")
            proactive_task = ProactiveThinkingTask()
            await async_task_manager.add_task(proactive_task)

            # 检查“冷启动”功能的独立开关
            if global_config.proactive_thinking.enable_cold_start:
                logger.info("“冷启动”功能已启用，正在启动“破冰”任务...")
                cold_start_task = ColdStartTask()
                await async_task_manager.add_task(cold_start_task)

        else:
            logger.info("【主动思考】功能未启用，所有任务均跳过启动。")
        return HandlerResult(success=True, continue_process=True, message=None)
