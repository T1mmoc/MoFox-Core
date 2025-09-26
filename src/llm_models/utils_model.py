# -*- coding: utf-8 -*-
"""
@File    :   utils_model.py
@Time    :   2024/05/24 17:15:00
@Author  :   墨墨
@Version :   2.0 (Refactored)
@Desc    :   LLM请求协调器
"""
import time
from typing import Tuple, List, Dict, Optional, Any

from src.common.logger import get_logger
from src.config.api_ada_configs import TaskConfig, ModelInfo
from .llm_utils import build_tool_options, normalize_image_format
from .model_selector import ModelSelector
from .payload_content.message import MessageBuilder
from .payload_content.tool_option import ToolCall
from .prompt_processor import PromptProcessor
from .request_strategy import RequestStrategy
from .utils import llm_usage_recorder

logger = get_logger("model_utils")

class LLMRequest:
    """LLM请求协调器"""

    def __init__(self, model_set: TaskConfig, request_type: str = "") -> None:
        self.task_name = request_type
        self.model_for_task = model_set
        self.request_type = request_type
        self.model_selector = ModelSelector(model_set, request_type)
        self.prompt_processor = PromptProcessor()
        self.request_strategy = RequestStrategy(model_set, self.model_selector, request_type)

    async def generate_response_for_image(
        self,
        prompt: str,
        image_base64: str,
        image_format: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Tuple[str, Tuple[str, str, Optional[List[ToolCall]]]]:
        """为图像生成响应"""
        start_time = time.time()
        
        # 1. 选择模型
        model_info, api_provider, client = self.model_selector.select_model()
        
        # 2. 准备消息体
        processed_prompt = self.prompt_processor.process_prompt(prompt, model_info, api_provider, self.task_name)
        normalized_format = normalize_image_format(image_format)
        
        message_builder = MessageBuilder()
        message_builder.add_text_content(processed_prompt)
        message_builder.add_image_content(
            image_base64=image_base64,
            image_format=normalized_format,
            support_formats=client.get_support_image_formats(),
        )
        messages = [message_builder.build()]

        # 3. 执行请求 (图像请求通常不走复杂的故障转移策略，直接执行)
        from .request_executor import RequestExecutor
        executor = RequestExecutor(
            task_name=self.task_name,
            model_set=self.model_for_task,
            api_provider=api_provider,
            client=client,
            model_info=model_info,
            model_selector=self.model_selector,
        )
        response = await executor.execute_request(
            request_type="response",
            message_list=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        # 4. 处理响应
        content, reasoning_content = self.prompt_processor.extract_reasoning(response.content or "")
        tool_calls = response.tool_calls
        
        if usage := response.usage:
            await self._record_usage(model_info, usage, time.time() - start_time)
            
        return content, (reasoning_content, model_info.name, tool_calls)

    async def generate_response_for_voice(self, voice_base64: str) -> Optional[str]:
        """为语音生成响应"""
        model_info, api_provider, client = self.model_selector.select_model()
        
        from .request_executor import RequestExecutor
        executor = RequestExecutor(
            task_name=self.task_name,
            model_set=self.model_for_task,
            api_provider=api_provider,
            client=client,
            model_info=model_info,
            model_selector=self.model_selector,
        )
        response = await executor.execute_request(
            request_type="audio",
            audio_base64=voice_base64,
        )
        return response.content or None

    async def generate_response_async(
        self,
        prompt: str,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        raise_when_empty: bool = True,
    ) -> Tuple[str, Tuple[str, str, Optional[List[ToolCall]]]]:
        """异步生成响应，支持并发和故障转移"""
        
        # 1. 准备基础请求载荷
        tool_built = build_tool_options(tools)
        base_payload = {
            "prompt": prompt,
            "tool_options": tool_built,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_processor": self.prompt_processor,
        }
        
        # 2. 根据配置选择执行策略
        concurrency_count = getattr(self.model_for_task, "concurrency_count", 1)
        
        if concurrency_count <= 1:
            # 单次请求，但使用带故障转移的策略
            result = await self.request_strategy.execute_with_fallback(
                base_payload, raise_when_empty
            )
        else:
            # 并发请求策略
            result = await self.request_strategy.execute_concurrently(
                self.request_strategy.execute_with_fallback,
                concurrency_count,
                base_payload,
                raise_when_empty=False,
            )
        
        # 3. 处理最终结果
        content, (reasoning_content, model_name, tool_calls) = result
        
        # 4. 记录用量 (需要从策略中获取最终使用的模型信息和用量)
        # TODO: 改造策略以返回最终模型信息和用量, 此处暂时省略
        
        return content, (reasoning_content, model_name, tool_calls)

    async def get_embedding(self, embedding_input: str) -> Tuple[List[float], str]:
        """获取嵌入向量"""
        start_time = time.time()
        model_info, api_provider, client = self.model_selector.select_model()
        
        from .request_executor import RequestExecutor
        executor = RequestExecutor(
            task_name=self.task_name,
            model_set=self.model_for_task,
            api_provider=api_provider,
            client=client,
            model_info=model_info,
            model_selector=self.model_selector,
        )
        response = await executor.execute_request(
            request_type="embedding",
            embedding_input=embedding_input,
        )
        
        embedding = response.embedding
        if not embedding:
            raise RuntimeError("获取embedding失败")
            
        if usage := response.usage:
            await self._record_usage(model_info, usage, time.time() - start_time, "/embeddings")
            
        return embedding, model_info.name

    async def _record_usage(self, model_info: ModelInfo, usage, time_cost, endpoint="/chat/completions"):
        """记录模型用量"""
        await llm_usage_recorder.record_usage_to_database(
            model_info=model_info,
            model_usage=usage,
            user_id="system",
            time_cost=time_cost,
            request_type=self.request_type,
            endpoint=endpoint,
        )
