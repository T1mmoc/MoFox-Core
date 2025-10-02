import orjson
from typing import Optional, Dict, Any
from datetime import datetime

from src.common.logger import get_logger
from src.plugin_system.apis import chat_api, person_api, schedule_api, send_api, llm_api, message_api
from src.config.config import global_config, model_config
from src.person_info.person_info import get_person_info_manager

logger = get_logger(__name__)


class ProactiveThinkerExecutor:
    """
    主动思考执行器 V2
    - 统一执行入口
    - 引入决策模块，判断是否及如何发起对话
    - 结合人设、日程、关系信息生成更具情境的对话
    """

    def __init__(self):
        # 可以在此初始化所需模块，例如LLM请求器等
        pass

    async def execute(self, stream_id: str, start_mode: str = "wake_up"):
        """
        统一执行入口
        Args:
            stream_id: 聊天流ID
            start_mode: 启动模式, 'cold_start' 或 'wake_up'
        """
        logger.info(f"开始为聊天流 {stream_id} 执行主动思考，模式: {start_mode}")

        # 1. 信息收集
        context = await self._gather_context(stream_id)
        if not context:
            return

        # 2. 决策阶段
        decision_result = await self._make_decision(context, start_mode)
        if not decision_result or not decision_result.get("should_reply"):
            reason = decision_result.get("reason", "未提供") if decision_result else "决策过程返回None"
            logger.info(f"决策结果为：不回复。原因: {reason}")
            return

        # 3. 规划与执行阶段
        topic = decision_result.get("topic", "打个招呼")
        logger.info(f"决策结果为：回复。话题: {topic}")
        
        plan_prompt = self._build_plan_prompt(context, start_mode, topic)
        
        is_success, response, _, _ = await llm_api.generate_with_model(prompt=plan_prompt, model_config=model_config.model_task_config.utils)
        
        if is_success:
            stream = self._get_stream_from_id(stream_id)
            if stream:
                await send_api.text_to_stream(stream_id=stream.stream_id, text=response)
            else:
                logger.warning(f"无法发送消息，因为找不到 stream_id 为 {stream_id} 的聊天流")

    def _get_stream_from_id(self, stream_id: str):
        """根据stream_id解析并获取stream对象"""
        try:
            platform, chat_id, stream_type = stream_id.split(":")
            if stream_type == "private":
                return chat_api.ChatManager.get_private_stream_by_user_id(platform, chat_id)
            elif stream_type == "group":
                return chat_api.ChatManager.get_group_stream_by_group_id(platform, chat_id)
        except Exception as e:
            logger.error(f"解析 stream_id ({stream_id}) 或获取 stream 失败: {e}")
        return None

    async def _gather_context(self, stream_id: str) -> Optional[Dict[str, Any]]:
        """
        收集构建提示词所需的所有上下文信息
        """
        stream = self._get_stream_from_id(stream_id)
        if not stream:
            logger.warning(f"无法找到 stream_id 为 {stream_id} 的聊天流")
            return None

        user_info = stream.user_info
        if not user_info or not user_info.platform or not user_info.user_id:
            logger.warning(f"Stream {stream_id} 的 user_info 不完整")
            return None
        
        person_id = person_api.get_person_id(user_info.platform, int(user_info.user_id))
        person_info_manager = get_person_info_manager()

        # 获取日程
        schedules = await schedule_api.ScheduleAPI.get_today_schedule()
        schedule_context = "\n".join([f"- {s['title']} ({s['start_time']}-{s['end_time']})" for s in schedules]) if schedules else "今天没有日程安排。"

        # 获取关系信息
        short_impression = await person_info_manager.get_value(person_id, "short_impression") or "无"
        impression = await person_info_manager.get_value(person_id, "impression") or "无"
        attitude = await person_info_manager.get_value(person_id, "attitude") or 50
        
        # 获取最近聊天记录
        recent_messages = await message_api.get_recent_messages(stream_id, limit=10)
        recent_chat_history = await message_api.build_readable_messages_to_str(recent_messages) if recent_messages else "无"

        return {
            "person_id": person_id,
            "user_info": user_info,
            "schedule_context": schedule_context,
            "recent_chat_history": recent_chat_history,
            "relationship": {
                "short_impression": short_impression,
                "impression": impression,
                "attitude": attitude
            },
            "persona": {
                "core": global_config.personality.personality_core,
                "side": global_config.personality.personality_side,
                "identity": global_config.personality.identity,
            },
            "current_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

    async def _make_decision(self, context: Dict[str, Any], start_mode: str) -> Optional[Dict[str, Any]]:
        """
        决策模块：判断是否应该主动发起对话，以及聊什么话题
        """
        persona = context['persona']
        user_info = context['user_info']
        relationship = context['relationship']

        prompt = f"""
# 角色
你的名字是{global_config.bot.nickname}，你的人设如下：
- 核心人设: {persona['core']}
- 侧面人设: {persona['side']}
- 身份: {persona['identity']}

# 任务
现在是 {context['current_time']}，你需要根据当前的情境，决定是否要主动向用户 '{user_info.user_nickname}' 发起对话。

# 情境分析
1.  **启动模式**: {start_mode} ({'初次见面/很久未见' if start_mode == 'cold_start' else '日常唤醒'})
2.  **你的日程**:
{context['schedule_context']}
3.  **你和Ta的关系**:
    - 简短印象: {relationship['short_impression']}
    - 详细印象: {relationship['impression']}
    - 好感度: {relationship['attitude']}/100
4.  **最近的聊天摘要**:
{context['recent_chat_history']}

# 决策指令
请综合以上所有信息，做出决策。你的决策需要以JSON格式输出，包含以下字段：
- `should_reply`: bool, 是否应该发起对话。
- `topic`: str, 如果 `should_reply` 为 true，你打算聊什么话题？(例如：问候一下今天的日程、关心一下昨天的某件事、分享一个你自己的趣事等)
- `reason`: str, 做出此决策的简要理由。

---
示例1 (应该回复):
{{
  "should_reply": true,
  "topic": "提醒Ta今天下午有'项目会议'的日程",
  "reason": "现在是上午，Ta下午有个重要会议，我觉得应该主动提醒一下，这会显得我很贴心。"
}}

示例2 (不应回复):
{{
  "should_reply": false,
  "topic": null,
  "reason": "虽然我们的关系不错，但现在是深夜，而且Ta今天的日程都已经完成了，我没有合适的理由去打扰Ta。"
}}
---

请输出你的决策:
"""
        
        is_success, response, _, _ = await llm_api.generate_with_model(prompt=prompt, model_config=model_config.model_task_config.utils)
        
        if not is_success:
            return {"should_reply": False, "reason": "决策模型生成失败"}

        try:
            # 假设LLM返回JSON格式的决策结果
            decision = orjson.loads(response)
            return decision
        except orjson.JSONDecodeError:
            logger.error(f"决策LLM返回的JSON格式无效: {response}")
            return {"should_reply": False, "reason": "决策模型返回格式错误"}

    def _build_plan_prompt(self, context: Dict[str, Any], start_mode: str, topic: str) -> str:
        """
        根据启动模式和决策话题，构建最终的规划提示词
        """
        persona = context['persona']
        user_info = context['user_info']
        relationship = context['relationship']

        if start_mode == "cold_start":
            prompt = f"""
# 角色
你的名字是{global_config.bot.nickname}，你的人设如下：
- 核心人设: {persona['core']}
- 侧面人设: {persona['side']}
- 身份: {persona['identity']}

# 任务
你需要主动向一个新朋友 '{user_info.user_nickname}' 发起对话。这是你们的第一次交流，或者很久没聊了。

# 情境分析
- **你和Ta的关系**:
    - 简短印象: {relationship['short_impression']}
    - 详细印象: {relationship['impression']}
    - 好感度: {relationship['attitude']}/100

# 对话指引
- 你的目标是“破冰”，让对话自然地开始。
- 你应该围绕这个话题展开: {topic}
- 你的语气应该符合你的人设，友好且真诚。
- 直接输出你要说的第一句话，不要包含任何额外的前缀或解释。
"""
        else:  # wake_up
            prompt = f"""
# 角色
你的名字是{global_config.bot.nickname}，你的人设如下：
- 核心人设: {persona['core']}
- 侧面人设: {persona['side']}
- 身份: {persona['identity']}

# 任务
现在是 {context['current_time']}，你需要主动向你的朋友 '{user_info.user_nickname}' 发起对话。

# 情境分析
1.  **你的日程**:
{context['schedule_context']}
2.  **你和Ta的关系**:
    - 详细印象: {relationship['impression']}
    - 好感度: {relationship['attitude']}/100
3.  **最近的聊天摘要**:
{context['recent_chat_history']}

# 对话指引
- 你决定和Ta聊聊关于“{topic}”的话题。
- 请结合以上所有情境信息，自然地开启对话。
- 你的语气应该符合你的人设以及你对Ta的好感度。
- 直接输出你要说的第一句话，不要包含任何额外的前缀或解释。
"""
        return prompt
