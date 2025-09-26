import asyncio
import orjson
import io
from typing import Callable, Any, Coroutine, Optional
import aiohttp

from src.config.api_ada_configs import ModelInfo, APIProvider
from src.common.logger import get_logger
from .base_client import APIResponse, UsageRecord, BaseClient, client_registry
from ..exceptions import (
    RespParseException,
    NetworkConnectionError,
    RespNotOkException,
    ReqAbortException,
)
from ..payload_content.message import Message, RoleType
from ..payload_content.resp_format import RespFormat, RespFormatType
from ..payload_content.tool_option import ToolOption, ToolParam, ToolCall

logger = get_logger("AioHTTP-Gemini客户端")


# gemini_thinking参数（默认范围）
# 不同模型的思考预算范围配置
THINKING_BUDGET_LIMITS = {
    "gemini-2.5-flash": {"min": 1, "max": 24576, "can_disable": True},
    "gemini-2.5-flash-lite": {"min": 512, "max": 24576, "can_disable": True},
    "gemini-2.5-pro": {"min": 128, "max": 32768, "can_disable": False},
}
# 思维预算特殊值
THINKING_BUDGET_AUTO = -1  # 自动调整思考预算，由模型决定
THINKING_BUDGET_DISABLED = 0  # 禁用思考预算（如果模型允许禁用）

gemini_safe_settings = [
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_CIVIC_INTEGRITY", "threshold": "BLOCK_NONE"},
]


def _format_to_mime_type(image_format: str) -> str:
    """
    将图片格式转换为正确的MIME类型

    Args:
        image_format (str): 图片格式 (如 'jpg', 'png' 等)

    Returns:
        str: 对应的MIME类型
    """
    format_mapping = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
        "gif": "image/gif",
        "heic": "image/heic",
        "heif": "image/heif",
    }

    return format_mapping.get(image_format.lower(), f"image/{image_format.lower()}")


def _convert_messages(messages: list[Message]) -> tuple[list[dict], list[str] | None]:
    """
    转换消息格式 - 将消息转换为Gemini REST API所需的格式
    :param messages: 消息列表
    :return: (contents, system_instructions)
    """

    def _convert_message_item(message: Message) -> dict:
        """转换单个消息格式"""
        # 转换角色名称
        if message.role == RoleType.Assistant:
            role = "model"
        elif message.role == RoleType.User:
            role = "user"
        else:
            raise ValueError(f"不支持的消息角色: {message.role}")

        # 转换内容
        parts = []
        if isinstance(message.content, str):
            parts.append({"text": message.content})
        elif isinstance(message.content, list):
            for item in message.content:
                if isinstance(item, tuple):  # (format, base64_data)
                    parts.append({"inline_data": {"mime_type": _format_to_mime_type(item[0]), "data": item[1]}})
                elif isinstance(item, str):
                    parts.append({"text": item})
        else:
            raise RuntimeError("无法触及的代码：请使用MessageBuilder类构建消息对象")

        return {"role": role, "parts": parts}

    contents = []
    system_instructions = []

    for message in messages:
        if message.role == RoleType.System:
            if isinstance(message.content, str):
                system_instructions.append(message.content)
            else:
                raise ValueError("System消息不支持非文本内容")
        elif message.role == RoleType.Tool:
            # 工具调用结果处理
            if not message.tool_call_id:
                raise ValueError("工具调用消息缺少tool_call_id")
            contents.append({"role": "function", "parts": [{"text": str(message.content)}]})
        else:
            contents.append(_convert_message_item(message))

    return contents, system_instructions if system_instructions else None


def _convert_tool_options(tool_options: list[ToolOption]) -> list[dict]:
    """
    转换工具选项格式 - 将工具选项转换为Gemini REST API所需的格式
    """

    def _convert_tool_param(param: ToolParam) -> dict:
        """转换工具参数"""
        result: dict[str, Any] = {
            "type": param.param_type.value,
            "description": param.description,
        }
        if param.enum_values:
            result["enum"] = param.enum_values
        return result

    def _convert_tool_option_item(tool_option: ToolOption) -> dict:
        """转换单个工具选项"""
        function_declaration: dict[str, Any] = {
            "name": tool_option.name,
            "description": tool_option.description,
        }

        if tool_option.params:
            function_declaration["parameters"] = {
                "type": "object",
                "properties": {param.name: _convert_tool_param(param) for param in tool_option.params},
                "required": [param.name for param in tool_option.params if param.required],
            }

        return {"function_declarations": [function_declaration]}

    return [_convert_tool_option_item(tool_option) for tool_option in tool_options]


def _build_generation_config(
    max_tokens: int,
    temperature: float,
    thinking_budget: int,
    response_format: RespFormat | None = None,
    extra_params: dict | None = None,
) -> dict:
    """构建生成配置"""
    config = {
        "maxOutputTokens": max_tokens,
        "temperature": temperature,
        "topK": 1,
        "topP": 1,
        "safetySettings": gemini_safe_settings,
        "thinkingConfig": {"includeThoughts": True, "thinkingBudget": thinking_budget},
    }

    # 处理响应格式
    if response_format:
        if response_format.format_type == RespFormatType.JSON_OBJ:
            config["responseMimeType"] = "application/json"
        elif response_format.format_type == RespFormatType.JSON_SCHEMA:
            config["responseMimeType"] = "application/json"
            config["responseSchema"] = response_format.to_dict()

    # 合并额外参数
    if extra_params:
        # 拷贝一份以防修改原始字典
        safe_extra_params = extra_params.copy()
        # 移除已单独处理的 thinking_budget
        safe_extra_params.pop("thinking_budget", None)
        config.update(safe_extra_params)

    return config


class AiohttpGeminiStreamParser:
    """流式响应解析器"""

    def __init__(self):
        self.content_buffer = io.StringIO()
        self.reasoning_buffer = io.StringIO()
        self.tool_calls_buffer = []
        self.usage_record = None

    def parse_chunk(self, chunk_text: str):
        """解析单个流式数据块"""
        try:
            if not chunk_text.strip():
                return

            # 移除data:前缀
            if chunk_text.startswith("data: "):
                chunk_text = chunk_text[6:].strip()

            if chunk_text == "[DONE]":
                return

            chunk_data = orjson.loads(chunk_text)

            # 解析候选项
            if "candidates" in chunk_data and chunk_data["candidates"]:
                candidate = chunk_data["candidates"][0]

                # 解析内容
                if "content" in candidate and "parts" in candidate["content"]:
                    for part in candidate["content"]["parts"]:
                        if "text" in part:
                            self.content_buffer.write(part["text"])

                # 解析工具调用
                if "functionCall" in candidate:
                    func_call = candidate["functionCall"]
                    call_id = f"gemini_call_{len(self.tool_calls_buffer)}"
                    self.tool_calls_buffer.append(
                        {"id": call_id, "name": func_call.get("name", ""), "args": func_call.get("args", {})}
                    )

            # 解析使用统计
            if "usageMetadata" in chunk_data:
                usage = chunk_data["usageMetadata"]
                self.usage_record = (
                    usage.get("promptTokenCount", 0),
                    usage.get("candidatesTokenCount", 0),
                    usage.get("totalTokenCount", 0),
                )

        except orjson.JSONDecodeError as e:
            logger.warning(f"解析流式数据块失败: {e}, 数据: {chunk_text}")
        except Exception as e:
            logger.error(f"处理流式数据块时出错: {e}")

    def get_response(self) -> APIResponse:
        """获取最终响应"""
        response = APIResponse()

        if self.content_buffer.tell() > 0:
            response.content = self.content_buffer.getvalue()

        if self.reasoning_buffer.tell() > 0:
            response.reasoning_content = self.reasoning_buffer.getvalue()

        if self.tool_calls_buffer:
            response.tool_calls = []
            for call_data in self.tool_calls_buffer:
                response.tool_calls.append(ToolCall(call_data["id"], call_data["name"], call_data["args"]))

        # 清理缓冲区
        self.content_buffer.close()
        self.reasoning_buffer.close()

        return response


async def _default_stream_response_handler(
    response: aiohttp.ClientResponse,
    interrupt_flag: asyncio.Event | None,
) -> tuple[APIResponse, Optional[tuple[int, int, int]]]:
    """默认流式响应处理器"""
    parser = AiohttpGeminiStreamParser()

    try:
        async for line in response.content:
            if interrupt_flag and interrupt_flag.is_set():
                raise ReqAbortException("请求被外部信号中断")

            line_text = line.decode("utf-8").strip()
            if line_text:
                parser.parse_chunk(line_text)

        api_response = parser.get_response()
        return api_response, parser.usage_record

    except Exception as e:
        if not isinstance(e, ReqAbortException):
            raise RespParseException(None, f"流式响应解析失败: {e}") from e
        raise


def _default_normal_response_parser(
    response_data: dict,
) -> tuple[APIResponse, Optional[tuple[int, int, int]]]:
    """默认普通响应解析器"""
    api_response = APIResponse()

    try:
        # 解析候选项
        if "candidates" in response_data and response_data["candidates"]:
            candidate = response_data["candidates"][0]

            # 解析文本内容
            if "content" in candidate and "parts" in candidate["content"]:
                content_parts = []
                for part in candidate["content"]["parts"]:
                    if "text" in part:
                        content_parts.append(part["text"])

                if content_parts:
                    api_response.content = "".join(content_parts)

            # 解析工具调用
            if "functionCall" in candidate:
                func_call = candidate["functionCall"]
                api_response.tool_calls = [
                    ToolCall("gemini_call_0", func_call.get("name", ""), func_call.get("args", {}))
                ]

        # 解析使用统计
        usage_record = None
        if "usageMetadata" in response_data:
            usage = response_data["usageMetadata"]
            usage_record = (
                usage.get("promptTokenCount", 0),
                usage.get("candidatesTokenCount", 0),
                usage.get("totalTokenCount", 0),
            )

        api_response.raw_data = response_data
        return api_response, usage_record

    except Exception as e:
        raise RespParseException(response_data, f"响应解析失败: {e}") from e


@client_registry.register_client_class("aiohttp_gemini")
class AiohttpGeminiClient(BaseClient):
    """使用aiohttp的Gemini客户端"""

    def __init__(self, api_provider: APIProvider):
        super().__init__(api_provider)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta"
        self.session: aiohttp.ClientSession | None = None
        self.api_key = api_provider.api_key

        # 如果提供了自定义base_url，使用它
        if api_provider.base_url:
            self.base_url = api_provider.base_url.rstrip("/")

    @staticmethod
    def clamp_thinking_budget(tb: int, model_id: str) -> int:
        """
        按模型限制思考预算范围，仅支持指定的模型（支持带数字后缀的新版本）
        """
        limits = None

        # 优先尝试精确匹配
        if model_id in THINKING_BUDGET_LIMITS:
            limits = THINKING_BUDGET_LIMITS[model_id]
        else:
            # 按 key 长度倒序，保证更长的（更具体的，如 -lite）优先
            sorted_keys = sorted(THINKING_BUDGET_LIMITS.keys(), key=len, reverse=True)
            for key in sorted_keys:
                # 必须满足：完全等于 或者 前缀匹配（带 "-" 边界）
                if model_id == key or model_id.startswith(f"{key}-"):
                    limits = THINKING_BUDGET_LIMITS[key]
                    break

        # 特殊值处理
        if tb == THINKING_BUDGET_AUTO:
            return THINKING_BUDGET_AUTO
        if tb == THINKING_BUDGET_DISABLED:
            if limits and limits.get("can_disable", False):
                return THINKING_BUDGET_DISABLED
            return limits["min"] if limits else THINKING_BUDGET_AUTO

        # 已知模型裁剪到范围
        if limits:
            return max(limits["min"], min(tb, limits["max"]))

        # 未知模型，返回动态模式
        logger.warning(f"模型 {model_id} 未在 THINKING_BUDGET_LIMITS 中定义，将使用动态模式 tb=-1 兼容。")
        return tb

    # 移除全局 session，全部请求都用 with aiohttp.ClientSession() as session:

    async def _make_request(
        self, method: str, endpoint: str, data: dict | None = None, stream: bool = False
    ) -> aiohttp.ClientResponse:
        """发起HTTP请求（每次都用 with aiohttp.ClientSession() as session）"""
        url = f"{self.base_url}/{endpoint}?key={self.api_key}"
        timeout = aiohttp.ClientTimeout(total=300)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                headers={"Content-Type": "application/json", "User-Agent": "MMC-AioHTTP-Gemini-Client/1.0"},
            ) as session:
                if method.upper() == "POST":
                    response = await session.post(
                        url, json=data, headers={"Accept": "text/event-stream" if stream else "application/json"}
                    )
                else:
                    response = await session.get(url)

                # 检查HTTP状态码
                if response.status >= 400:
                    error_text = await response.text()
                    raise RespNotOkException(response.status, error_text)

                return response
        except aiohttp.ClientError as e:
            raise NetworkConnectionError() from e

    async def get_response(
        self,
        model_info: ModelInfo,
        message_list: list[Message],
        tool_options: list[ToolOption] | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.7,
        response_format: RespFormat | None = None,
        stream_response_handler: Optional[
            Callable[
                [aiohttp.ClientResponse, asyncio.Event | None],
                Coroutine[Any, Any, tuple[APIResponse, Optional[tuple[int, int, int]]]],
            ]
        ] = None,
        async_response_parser: Optional[Callable[[dict], tuple[APIResponse, Optional[tuple[int, int, int]]]]] = None,
        interrupt_flag: asyncio.Event | None = None,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取对话响应
        """
        if stream_response_handler is None:
            stream_response_handler = _default_stream_response_handler

        if async_response_parser is None:
            async_response_parser = _default_normal_response_parser

        # 转换消息格式
        contents, system_instructions = _convert_messages(message_list)

        # 处理思考预算
        tb = THINKING_BUDGET_AUTO
        if extra_params and "thinking_budget" in extra_params:
            try:
                tb = int(extra_params["thinking_budget"])
            except (ValueError, TypeError):
                logger.warning(f"无效的 thinking_budget 值 {extra_params['thinking_budget']}，将使用默认动态模式 {tb}")
        tb = self.clamp_thinking_budget(tb, model_info.model_identifier)

        # 构建请求体
        request_data = {
            "contents": contents,
            "generationConfig": _build_generation_config(
                max_tokens, temperature, tb, response_format, extra_params
            ),
        }

        # 添加系统指令
        if system_instructions:
            request_data["systemInstruction"] = {"parts": [{"text": instr} for instr in system_instructions]}

        # 添加工具定义
        if tool_options:
            request_data["tools"] = _convert_tool_options(tool_options)

        try:
            if model_info.force_stream_mode:
                # 流式请求
                endpoint = f"models/{model_info.model_identifier}:streamGenerateContent"
                req_task = asyncio.create_task(self._make_request("POST", endpoint, request_data, stream=True))

                while not req_task.done():
                    if interrupt_flag and interrupt_flag.is_set():
                        req_task.cancel()
                        raise ReqAbortException("请求被外部信号中断")
                    await asyncio.sleep(0.1)

                response = req_task.result()
                api_response, usage_record = await stream_response_handler(response, interrupt_flag)

            else:
                # 普通请求
                endpoint = f"models/{model_info.model_identifier}:generateContent"
                req_task = asyncio.create_task(self._make_request("POST", endpoint, request_data))

                while not req_task.done():
                    if interrupt_flag and interrupt_flag.is_set():
                        req_task.cancel()
                        raise ReqAbortException("请求被外部信号中断")
                    await asyncio.sleep(0.1)

                response = req_task.result()
                response_data = await response.json()
                api_response, usage_record = async_response_parser(response_data)

        except (ReqAbortException, NetworkConnectionError, RespNotOkException, RespParseException):
            # 直接重抛项目定义的异常
            raise
        except Exception as e:
            logger.debug(f"请求处理中发生未知异常: {e}")
            # 其他异常转换为网络连接错误
            raise NetworkConnectionError() from e

        # 设置使用统计
        if usage_record:
            api_response.usage = UsageRecord(
                model_name=model_info.name,
                provider_name=model_info.api_provider,
                prompt_tokens=usage_record[0],
                completion_tokens=usage_record[1],
                total_tokens=usage_record[2],
            )

        return api_response

    async def get_embedding(
        self,
        model_info: ModelInfo,
        embedding_input: str,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取文本嵌入 - 此客户端不支持嵌入功能
        """
        raise NotImplementedError("AioHTTP Gemini客户端不支持文本嵌入功能")

    async def get_audio_transcriptions(
        self,
        model_info: ModelInfo,
        audio_base64: str,
        extra_params: dict[str, Any] | None = None,
    ) -> APIResponse:
        """
        获取音频转录
        """
        # 构建包含音频的内容
        contents = [
            {
                "role": "user",
                "parts": [
                    {
                        "text": "Generate a transcript of the speech. The language of the transcript should match the language of the speech."
                    },
                    {"inline_data": {"mime_type": "audio/wav", "data": audio_base64}},
                ],
            }
        ]

        request_data = {
            "contents": contents,
            "generationConfig": _build_generation_config(2048, 0.1, THINKING_BUDGET_AUTO, None, extra_params),
        }

        try:
            endpoint = f"models/{model_info.model_identifier}:generateContent"
            response = await self._make_request("POST", endpoint, request_data)
            response_data = await response.json()

            api_response, usage_record = _default_normal_response_parser(response_data)

            if usage_record:
                api_response.usage = UsageRecord(
                    model_name=model_info.name,
                    provider_name=model_info.api_provider,
                    prompt_tokens=usage_record[0],
                    completion_tokens=usage_record[1],
                    total_tokens=usage_record[2],
                )

            return api_response

        except (NetworkConnectionError, RespNotOkException, RespParseException):
            raise
        except Exception as e:
            raise NetworkConnectionError() from e

    def get_support_image_formats(self) -> list[str]:
        """
        获取支持的图片格式
        """
        return ["png", "jpg", "jpeg", "webp", "heic", "heif"]

    # 移除 __aenter__、__aexit__、__del__，不再持有全局 session
