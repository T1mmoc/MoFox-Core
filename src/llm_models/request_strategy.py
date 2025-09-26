# -*- coding: utf-8 -*-
"""
@File    :   request_strategy.py
@Time    :   2024/05/24 16:30:00
@Author  :   墨墨
@Version :   1.0
@Desc    :   高级请求策略（并发、故障转移）
"""
import asyncio
import random
from typing import List, Tuple, Optional, Dict, Any, Callable, Coroutine

from src.common.logger import get_logger
from src.config.api_ada_configs import TaskConfig
from .model_client.base_client import APIResponse
from .model_selector import ModelSelector
from .payload_content.message import MessageBuilder
from .payload_content.tool_option import ToolCall
from .prompt_processor import PromptProcessor
from .request_executor import RequestExecutor

logger = get_logger("request_strategy")


class RequestStrategy:
    """高级请求策略"""

    def __init__(self, model_set: TaskConfig, model_selector: ModelSelector, task_name: str):
        self.model_set = model_set
        self.model_selector = model_selector
        self.task_name = task_name

    async def execute_with_fallback(
        self,
        base_payload: Dict[str, Any],
        raise_when_empty: bool = True,
    ) -> Dict[str, Any]:
        """
        执行单次请求，动态选择最佳可用模型，并在模型失败时进行故障转移。
        """
        failed_models_in_this_request = set()
        max_attempts = len(self.model_set.model_list)
        last_exception: Optional[Exception] = None

        for attempt in range(max_attempts):
            model_selection_result = self.model_selector.select_best_available_model(failed_models_in_this_request)

            if model_selection_result is None:
                logger.error(f"尝试 {attempt + 1}/{max_attempts}: 没有可用的模型了。")
                break

            model_info, api_provider, client = model_selection_result
            model_name = model_info.name
            logger.debug(f"尝试 {attempt + 1}/{max_attempts}: 正在使用模型 '{model_name}'...")

            try:
                # 1. Process Prompt
                prompt_processor: PromptProcessor = base_payload["prompt_processor"]
                raw_prompt = base_payload["prompt"]
                processed_prompt = prompt_processor.process_prompt(
                    raw_prompt, model_info, api_provider, self.task_name
                )
                
                # 2. Build Message
                message_builder = MessageBuilder().add_text_content(processed_prompt)
                messages = [message_builder.build()]

                # 3. Create payload for executor
                executor_payload = {
                    "request_type": "response", # Strategy only handles response type
                    "message_list": messages,
                    "tool_options": base_payload["tool_options"],
                    "temperature": base_payload["temperature"],
                    "max_tokens": base_payload["max_tokens"],
                }
                
                executor = RequestExecutor(
                    task_name=self.task_name,
                    model_set=self.model_set,
                    api_provider=api_provider,
                    client=client,
                    model_info=model_info,
                    model_selector=self.model_selector,
                )
                response = await self._execute_and_handle_empty_retry(executor, executor_payload, prompt_processor)

                # 4. Post-process response
                # The reasoning content is now extracted here, after a successful, de-truncated response is received.
                final_content, reasoning_content = prompt_processor.extract_reasoning(response.content or "")
                response.content = final_content # Update response with cleaned content
                
                tool_calls = response.tool_calls

                if not final_content and not tool_calls:
                    if raise_when_empty:
                        raise RuntimeError("所选模型生成了空回复。")
                    content = "生成的响应为空"  # Fallback message

                logger.debug(f"模型 '{model_name}' 成功生成了回复。")
                return {
                    "content": response.content,
                    "reasoning_content": reasoning_content,
                    "model_name": model_name,
                    "tool_calls": tool_calls,
                    "model_info": model_info,
                    "usage": response.usage,
                    "success": True,
                }

            except Exception as e:
                logger.error(f"模型 '{model_info.name}' 失败，异常: {e}。将其添加到当前请求的失败模型列表中。")
                failed_models_in_this_request.add(model_info.name)
                last_exception = e

        logger.error(f"当前请求已尝试 {max_attempts} 个模型，所有模型均已失败。")
        if raise_when_empty:
            if last_exception:
                raise RuntimeError("所有模型均未能生成响应。") from last_exception
            raise RuntimeError("所有模型均未能生成响应，且无具体异常信息。")
        return {
            "content": "所有模型都请求失败",
            "reasoning_content": "",
            "model_name": "unknown",
            "tool_calls": None,
            "model_info": None,
            "usage": None,
            "success": False,
        }

    async def execute_concurrently(
        self,
        coro_callable: Callable[..., Coroutine[Any, Any, Any]],
        concurrency_count: int,
        *args,
        **kwargs,
    ) -> Any:
        """
        执行并发请求并从成功的结果中随机选择一个。
        """
        logger.info(f"启用并发请求模式，并发数: {concurrency_count}")
        tasks = [coro_callable(*args, **kwargs) for _ in range(concurrency_count)]

        results = await asyncio.gather(*tasks, return_exceptions=True)
        successful_results = [res for res in results if not isinstance(res, Exception)]

        if successful_results:
            selected = random.choice(successful_results)
            logger.info(f"并发请求完成，从{len(successful_results)}个成功结果中选择了一个")
            return selected

        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"并发任务 {i + 1}/{concurrency_count} 失败: {res}")

        first_exception = next((res for res in results if isinstance(res, Exception)), None)
        if first_exception:
            raise first_exception

        raise RuntimeError(f"所有 {concurrency_count} 个并发请求都失败了，但没有具体的异常信息")

    async def _execute_and_handle_empty_retry(
        self, executor: RequestExecutor, payload: Dict[str, Any], prompt_processor: PromptProcessor
    ) -> APIResponse:
        """
        在单个模型内部处理空回复/截断的重试逻辑
        """
        empty_retry_count = 0
        max_empty_retry = executor.api_provider.max_retry
        empty_retry_interval = executor.api_provider.retry_interval
        use_anti_truncation = getattr(executor.model_info, "use_anti_truncation", False)
        end_marker = prompt_processor.end_marker

        while empty_retry_count <= max_empty_retry:
            response = await executor.execute_request(**payload)

            content = response.content or ""
            tool_calls = response.tool_calls
            
            is_empty_reply = not tool_calls and (not content or content.strip() == "")
            is_truncated = False
            if use_anti_truncation and end_marker:
                if content.endswith(end_marker):
                    # 移除结束标记
                    response.content = content[: -len(end_marker)].strip()
                else:
                    is_truncated = True

            if is_empty_reply or is_truncated:
                empty_retry_count += 1
                if empty_retry_count <= max_empty_retry:
                    reason = "空回复" if is_empty_reply else "截断"
                    logger.warning(
                        f"模型 '{executor.model_info.name}' 检测到{reason}，正在进行内部重试 ({empty_retry_count}/{max_empty_retry})..."
                    )
                    if empty_retry_interval > 0:
                        await asyncio.sleep(empty_retry_interval)
                    continue
                else:
                    reason = "空回复" if is_empty_reply else "截断"
                    raise RuntimeError(f"模型 '{executor.model_info.name}' 经过 {max_empty_retry} 次内部重试后仍然生成{reason}的回复。")
            
            # 成功获取响应
            return response
        
        # 此处理论上不会到达，因为循环要么返回要么抛异常
        raise RuntimeError("空回复/截断重Test逻辑出现未知错误")
