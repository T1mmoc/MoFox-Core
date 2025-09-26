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
from typing import Optional, Dict, Any, Callable, Coroutine

from src.common.logger import get_logger
from src.config.api_ada_configs import TaskConfig
from .model_client.base_client import APIResponse
from .model_selector import ModelSelector
from .payload_content.message import MessageBuilder
from .prompt_processor import PromptProcessor
from .request_executor import RequestExecutor

logger = get_logger("model_utils")


class RequestStrategy:
    """
    高级请求策略模块。
    负责实现复杂的请求逻辑，如模型的故障转移（fallback）和并发请求。
    """

    def __init__(self, model_set: TaskConfig, model_selector: ModelSelector, task_name: str):
        """
        初始化请求策略。

        Args:
            model_set (TaskConfig): 特定任务的模型配置。
            model_selector (ModelSelector): 模型选择器实例。
            task_name (str): 当前任务的名称。
        """
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

        该方法会按顺序尝试任务配置中的所有可用模型，直到一个模型成功返回响应。
        如果所有模型都失败，将根据 `raise_when_empty` 参数决定是抛出异常还是返回一个失败结果。

        Args:
            base_payload (Dict[str, Any]): 基础请求载荷，包含prompt、工具选项等。
            raise_when_empty (bool, optional): 如果所有模型都失败或返回空内容，是否抛出异常。 Defaults to True.

        Returns:
            Dict[str, Any]: 一个包含响应结果的字典，包括内容、模型信息、用量和成功状态。
        """
        # 记录在本次请求中已经失败的模型，避免重复尝试
        failed_models_in_this_request = set()
        max_attempts = len(self.model_set.model_list)
        last_exception: Optional[Exception] = None

        for attempt in range(max_attempts):
            # 选择一个当前最佳且未失败的模型
            model_selection_result = self.model_selector.select_best_available_model(failed_models_in_this_request)

            if model_selection_result is None:
                logger.error(f"尝试 {attempt + 1}/{max_attempts}: 没有可用的模型了。")
                break  # 没有更多可用模型，跳出循环

            model_info, api_provider, client = model_selection_result
            model_name = model_info.name
            logger.debug(f"尝试 {attempt + 1}/{max_attempts}: 正在使用模型 '{model_name}'...")

            try:
                # 步骤 1: 预处理Prompt
                prompt_processor: PromptProcessor = base_payload["prompt_processor"]
                raw_prompt = base_payload["prompt"]
                processed_prompt = prompt_processor.process_prompt(
                    raw_prompt, model_info, api_provider, self.task_name
                )
                
                # 步骤 2: 构建消息体
                message_builder = MessageBuilder().add_text_content(processed_prompt)
                messages = [message_builder.build()]

                # 步骤 3: 为执行器创建载荷
                executor_payload = {
                    "request_type": "response",  # 策略模式目前只处理'response'类型请求
                    "message_list": messages,
                    "tool_options": base_payload["tool_options"],
                    "temperature": base_payload["temperature"],
                    "max_tokens": base_payload["max_tokens"],
                }
                
                # 创建请求执行器实例
                executor = RequestExecutor(
                    task_name=self.task_name,
                    model_set=self.model_set,
                    api_provider=api_provider,
                    client=client,
                    model_info=model_info,
                    model_selector=self.model_selector,
                )
                # 执行请求，并处理内部的空回复/截断重试
                response = await self._execute_and_handle_empty_retry(executor, executor_payload, prompt_processor)

                # 步骤 4: 后处理响应
                # 在获取到成功的、完整的响应后，提取思考过程内容
                final_content, reasoning_content = prompt_processor.extract_reasoning(response.content or "")
                response.content = final_content  # 使用清理后的内容更新响应对象
                
                tool_calls = response.tool_calls

                # 检查最终内容是否为空
                if not final_content and not tool_calls:
                    if raise_when_empty:
                        raise RuntimeError("所选模型生成了空回复。")
                    logger.warning(f"模型 '{model_name}' 生成了空回复，返回默认信息。")

                logger.debug(f"模型 '{model_name}' 成功生成了回复。")
                # 返回成功结果，包含用量和模型信息，供上层记录
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
                # 捕获请求过程中的任何异常
                logger.error(f"模型 '{model_info.name}' 失败，异常: {e}。将其添加到当前请求的失败模型列表中。")
                failed_models_in_this_request.add(model_info.name)
                last_exception = e

        # 如果循环结束仍未成功
        logger.error(f"当前请求已尝试 {max_attempts} 个模型，所有模型均已失败。")
        if raise_when_empty:
            if last_exception:
                raise RuntimeError("所有模型均未能生成响应。") from last_exception
            raise RuntimeError("所有模型均未能生成响应，且无具体异常信息。")
        
        # 返回失败结果
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
        以指定的并发数执行多个协程，并从所有成功的结果中随机选择一个返回。

        Args:
            coro_callable (Callable): 要并发执行的协程函数。
            concurrency_count (int): 并发数量。
            *args: 传递给协程函数的位置参数。
            **kwargs: 传递给协程函数的关键字参数。

        Returns:
            Any: 从成功的结果中随机选择的一个。
        
        Raises:
            RuntimeError: 如果所有并发任务都失败了。
        """
        logger.info(f"启用并发请求模式，并发数: {concurrency_count}")
        # 创建并发任务列表
        tasks = [coro_callable(*args, **kwargs) for _ in range(concurrency_count)]

        # 等待所有任务完成
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # 筛选出成功的结果
        successful_results = [
            res for res in results if isinstance(res, dict) and res.get("success")
        ]

        if successful_results:
            # 从成功结果中随机选择一个
            selected = random.choice(successful_results)
            logger.info(f"并发请求完成，从{len(successful_results)}个成功结果中选择了一个")
            return selected

        # 如果没有成功的结果，记录所有异常
        for i, res in enumerate(results):
            if isinstance(res, Exception):
                logger.error(f"并发任务 {i + 1}/{concurrency_count} 失败: {res}")

        # 抛出第一个遇到的异常
        first_exception = next((res for res in results if isinstance(res, Exception)), None)
        if first_exception:
            raise first_exception

        raise RuntimeError(f"所有 {concurrency_count} 个并发请求都失败了，但没有具体的异常信息")

    async def _execute_and_handle_empty_retry(
        self, executor: RequestExecutor, payload: Dict[str, Any], prompt_processor: PromptProcessor
    ) -> APIResponse:
        """
        在单个模型内部处理因回复为空或被截断而触发的重试逻辑。

        Args:
            executor (RequestExecutor): 请求执行器实例。
            payload (Dict[str, Any]): 传递给 `execute_request` 的载荷。
            prompt_processor (PromptProcessor): 提示词处理器，用于获取反截断标记。

        Returns:
            APIResponse: 一个有效的、非空且完整的API响应。
        
        Raises:
            RuntimeError: 如果在达到最大重试次数后仍然收到空回复或截断的回复。
        """
        empty_retry_count = 0
        max_empty_retry = executor.api_provider.max_retry
        empty_retry_interval = executor.api_provider.retry_interval
        # 检查模型是否启用了反截断功能
        use_anti_truncation = getattr(executor.model_info, "use_anti_truncation", False)
        end_marker = prompt_processor.end_marker

        while empty_retry_count <= max_empty_retry:
            response = await executor.execute_request(**payload)

            content = response.content or ""
            tool_calls = response.tool_calls
            
            # 判断是否为空回复
            is_empty_reply = not tool_calls and (not content or content.strip() == "")
            is_truncated = False
            
            # 如果启用了反截断，检查回复是否被截断
            if use_anti_truncation and end_marker:
                if content.endswith(end_marker):
                    # 如果包含结束标记，说明回复完整，移除标记
                    response.content = content[: -len(end_marker)].strip()
                else:
                    # 否则，认为回复被截断
                    is_truncated = True

            # 如果是空回复或截断，则进行重试
            if is_empty_reply or is_truncated:
                empty_retry_count += 1
                if empty_retry_count <= max_empty_retry:
                    reason = "空回复" if is_empty_reply else "截断"
                    logger.warning(
                        f"模型 '{executor.model_info.name}' 检测到{reason}，正在进行内部重试 ({empty_retry_count}/{max_empty_retry})..."
                    )
                    if empty_retry_interval > 0:
                        await asyncio.sleep(empty_retry_interval)
                    continue  # 继续下一次循环重试
                else:
                    # 达到最大重试次数，抛出异常
                    reason = "空回复" if is_empty_reply else "截断"
                    raise RuntimeError(f"模型 '{executor.model_info.name}' 经过 {max_empty_retry} 次内部重试后仍然生成{reason}的回复。")
            
            # 成功获取到有效响应，返回结果
            return response
        
        # 此处理论上不会到达，因为循环要么返回要么抛出异常
        raise RuntimeError("空回复/截断重试逻辑出现未知错误")
