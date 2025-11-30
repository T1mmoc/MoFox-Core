"""
Kokoro Flow Chatter V2 - Replyer

简化的回复生成模块，使用插件系统的 llm_api
"""

from typing import TYPE_CHECKING, Optional

from src.common.logger import get_logger
from src.plugin_system.apis import llm_api
from src.utils.json_parser import extract_and_parse_json

from .models import LLMResponse
from .prompt.builder import get_prompt_builder
from .session import KokoroSession

if TYPE_CHECKING:
    from src.chat.message_receive.chat_stream import ChatStream

logger = get_logger("kfc_v2_replyer")


async def generate_response(
    session: KokoroSession,
    user_name: str,
    situation_type: str = "new_message",
    chat_stream: Optional["ChatStream"] = None,
    available_actions: Optional[dict] = None,
    extra_context: Optional[dict] = None,
) -> LLMResponse:
    """
    生成回复
    
    Args:
        session: 会话对象
        user_name: 用户名称
        situation_type: 情况类型
        chat_stream: 聊天流对象
        available_actions: 可用动作字典
        extra_context: 额外上下文
        
    Returns:
        LLMResponse 对象
    """
    try:
        # 1. 构建提示词
        prompt_builder = get_prompt_builder()
        prompt = await prompt_builder.build_prompt(
            session=session,
            user_name=user_name,
            situation_type=situation_type,
            chat_stream=chat_stream,
            available_actions=available_actions,
            extra_context=extra_context,
        )
        
        logger.debug(f"[KFC Replyer] 构建的提示词:\n{prompt}")
        
        # 2. 获取模型配置并调用 LLM
        models = llm_api.get_available_models()
        replyer_config = models.get("replyer")
        
        if not replyer_config:
            logger.error("[KFC Replyer] 未找到 replyer 模型配置")
            return LLMResponse.create_error_response("未找到 replyer 模型配置")
        
        success, raw_response, reasoning, model_name = await llm_api.generate_with_model(
            prompt=prompt,
            model_config=replyer_config,
            request_type="kokoro_flow_chatter_v2",
        )
        
        if not success:
            logger.error(f"[KFC Replyer] LLM 调用失败: {raw_response}")
            return LLMResponse.create_error_response(raw_response)
        
        logger.debug(f"[KFC Replyer] LLM 响应 (model={model_name}):\n{raw_response}")
        
        # 3. 解析响应
        return _parse_response(raw_response)
        
    except Exception as e:
        logger.error(f"[KFC Replyer] 生成失败: {e}")
        import traceback
        traceback.print_exc()
        return LLMResponse.create_error_response(str(e))


def _parse_response(raw_response: str) -> LLMResponse:
    """解析 LLM 响应"""
    data = extract_and_parse_json(raw_response, strict=False)
    
    if not data or not isinstance(data, dict):
        logger.warning(f"[KFC Replyer] 无法解析 JSON: {raw_response[:200]}...")
        return LLMResponse.create_error_response("无法解析响应格式")
    
    response = LLMResponse.from_dict(data)
    
    if response.thought:
        logger.info(
            f"[KFC Replyer] 解析成功: thought={response.thought[:50]}..., "
            f"actions={[a.type for a in response.actions]}"
        )
    else:
        logger.warning("[KFC Replyer] 响应缺少 thought")
    
    return response
