"""
MCP SSE Client

基于 SSE 传输的 MCP 客户端实现
兼容 fastmcp 的 Client 接口，可用于 MCPClientManager
"""

import asyncio
from typing import Any

import mcp.types

from src.common.logger import get_logger
from .mcp_sse_transport import SSETransport

logger = get_logger("mcp_sse_client")


class SSEClient:
    """
    SSE MCP 客户端

    提供与 fastmcp Client 兼容的接口，使用 HTTP+SSE 传输
    """

    # MCP 协议版本
    PROTOCOL_VERSION = "2024-11-05"

    def __init__(
        self,
        url: str,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
        headers: dict[str, str] | None = None,
    ):
        """
        初始化 SSE 客户端

        Args:
            url: SSE 端点 URL
            timeout: 请求超时（秒）
            sse_read_timeout: SSE 读取超时（秒）
            headers: 额外的 HTTP 请求头
        """
        self.url = url
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout
        self.headers = headers or {}

        self._transport: SSETransport | None = None
        self._request_id = 0
        self._pending_requests: dict[int | str, asyncio.Future] = {}
        self._receive_task: asyncio.Task | None = None
        self._initialized = False
        self._server_info: dict[str, Any] | None = None
        self._server_capabilities: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

        logger.debug(f"SSE 客户端初始化: {url}")

    def _next_request_id(self) -> int:
        """生成下一个请求 ID"""
        self._request_id += 1
        return self._request_id

    async def connect(self) -> None:
        """建立连接并执行初始化握手"""
        async with self._lock:
            if self._initialized:
                return

            logger.info(f"SSE 客户端连接: {self.url}")

            # 创建传输
            self._transport = SSETransport(
                sse_url=self.url,
                headers=self.headers,
                timeout=self.timeout,
                sse_read_timeout=self.sse_read_timeout,
            )

            # 连接到 SSE 端点
            await self._transport.connect()

            # 启动消息接收任务
            self._receive_task = asyncio.create_task(self._receive_messages())

            # 执行 MCP 初始化握手
            await self._initialize()

            self._initialized = True
            logger.info("SSE 客户端初始化完成")

    async def _initialize(self) -> None:
        """执行 MCP 协议初始化握手"""
        # 发送 initialize 请求
        init_response = await self._send_request(
            "initialize",
            {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {
                    "roots": {"listChanged": True},
                    "sampling": {},
                },
                "clientInfo": {
                    "name": "MoFox-Bot",
                    "version": "1.0.0",
                },
            },
        )

        # 保存服务器信息
        self._server_info = init_response.get("serverInfo")
        self._server_capabilities = init_response.get("capabilities") or {}

        capabilities_keys = list(self._server_capabilities.keys()) if self._server_capabilities else []
        logger.info(
            f"MCP 服务器信息: {self._server_info} | "
            f"能力: {capabilities_keys}"
        )

        # 发送 initialized 通知
        await self._send_notification("notifications/initialized", {})

    async def _receive_messages(self) -> None:
        """接收并分发服务器消息"""
        if not self._transport:
            return

        try:
            async for message in self._transport.receive_iter():
                await self._handle_message(message)
        except asyncio.CancelledError:
            logger.debug("消息接收任务被取消")
        except Exception as e:
            logger.error(f"消息接收异常: {e}")

    async def _handle_message(self, message: dict[str, Any]) -> None:
        """
        处理接收到的 JSON-RPC 消息

        Args:
            message: JSON-RPC 消息
        """
        # 检查是否是响应
        if "id" in message and ("result" in message or "error" in message):
            request_id = message["id"]
            if request_id in self._pending_requests:
                future = self._pending_requests.pop(request_id)
                if "error" in message:
                    future.set_exception(
                        MCPError(
                            message["error"].get("code", -1),
                            message["error"].get("message", "Unknown error"),
                            message["error"].get("data"),
                        )
                    )
                else:
                    future.set_result(message.get("result"))
            else:
                logger.warning(f"收到未知请求 ID 的响应: {request_id}")

        # 检查是否是请求（服务器发起）
        elif "method" in message and "id" in message:
            logger.debug(f"收到服务器请求: {message['method']}")
            # 目前简单处理，返回空结果或错误
            await self._handle_server_request(message)

        # 检查是否是通知
        elif "method" in message:
            logger.debug(f"收到服务器通知: {message['method']}")
            await self._handle_notification(message)

    async def _handle_server_request(self, message: dict[str, Any]) -> None:
        """处理服务器发起的请求"""
        method = message.get("method")
        request_id = message.get("id")

        # 如果没有 request_id，跳过
        if request_id is None:
            logger.warning(f"服务器请求缺少 id: {method}")
            return

        # 根据不同的请求类型处理
        if method == "sampling/createMessage":
            # 采样请求 - 暂不支持
            await self._send_response(
                request_id,
                error={
                    "code": -32601,
                    "message": "Method not supported",
                },
            )
        elif method == "roots/list":
            # 返回空的 roots 列表
            await self._send_response(request_id, result={"roots": []})
        else:
            # 未知请求
            logger.warning(f"未处理的服务器请求: {method}")
            await self._send_response(
                request_id,
                error={
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            )

    async def _handle_notification(self, message: dict[str, Any]) -> None:
        """处理服务器通知"""
        method = message.get("method")
        params = message.get("params", {})

        logger.debug(f"服务器通知: {method} | 参数: {params}")

        # 可以在这里添加特定通知的处理逻辑
        # 例如 tools/list_changed, resources/list_changed 等

    async def _send_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """
        发送 JSON-RPC 请求并等待响应

        Args:
            method: 方法名
            params: 参数

        Returns:
            响应结果
        """
        if not self._transport:
            raise ConnectionError("客户端未连接")

        request_id = self._next_request_id()
        request: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            request["params"] = params

        # 创建 Future 等待响应
        future: asyncio.Future[Any] = asyncio.get_event_loop().create_future()
        self._pending_requests[request_id] = future

        try:
            await self._transport.send(request)
            result = await asyncio.wait_for(future, timeout=self.timeout)
            return result
        except asyncio.TimeoutError as e:
            self._pending_requests.pop(request_id, None)
            raise TimeoutError(f"请求 {method} 超时") from e
        except Exception:
            self._pending_requests.pop(request_id, None)
            raise

    async def _send_notification(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        """
        发送 JSON-RPC 通知（无需响应）

        Args:
            method: 方法名
            params: 参数
        """
        notification: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            notification["params"] = params

        if self._transport:
            await self._transport.send(notification)

    async def _send_response(
        self,
        request_id: int | str,
        result: Any = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        """
        发送 JSON-RPC 响应

        Args:
            request_id: 请求 ID
            result: 成功结果
            error: 错误信息
        """
        response: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
        }
        if error is not None:
            response["error"] = error
        else:
            response["result"] = result

        if self._transport:
            await self._transport.send(response)

    async def list_tools(self) -> list[mcp.types.Tool]:
        """
        获取服务器提供的工具列表

        Returns:
            工具列表
        """
        if not self._initialized:
            raise ConnectionError("客户端未初始化")

        result = await self._send_request("tools/list", {})
        tools_data = result.get("tools", [])

        tools = []
        for tool_data in tools_data:
            tool = mcp.types.Tool(
                name=tool_data.get("name", ""),
                description=tool_data.get("description"),
                inputSchema=tool_data.get("inputSchema", {}),
            )
            tools.append(tool)

        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> mcp.types.CallToolResult:
        """
        调用工具

        Args:
            name: 工具名称
            arguments: 工具参数

        Returns:
            工具调用结果
        """
        if not self._initialized:
            raise ConnectionError("客户端未初始化")

        result = await self._send_request(
            "tools/call",
            {
                "name": name,
                "arguments": arguments or {},
            },
        )

        # 转换结果为 CallToolResult
        content = []
        for item in result.get("content", []):
            content_type = item.get("type", "text")
            if content_type == "text":
                content.append(
                    mcp.types.TextContent(
                        type="text",
                        text=item.get("text", ""),
                    )
                )
            elif content_type == "image":
                content.append(
                    mcp.types.ImageContent(
                        type="image",
                        data=item.get("data", ""),
                        mimeType=item.get("mimeType", "image/png"),
                    )
                )
            else:
                # 其他类型作为文本处理
                content.append(
                    mcp.types.TextContent(
                        type="text",
                        text=str(item),
                    )
                )

        return mcp.types.CallToolResult(
            content=content,
            isError=result.get("isError", False),
        )

    async def list_resources(self) -> list[mcp.types.Resource]:
        """
        获取服务器提供的资源列表

        Returns:
            资源列表
        """
        if not self._initialized:
            raise ConnectionError("客户端未初始化")

        if not self._server_capabilities or "resources" not in self._server_capabilities:
            return []

        result = await self._send_request("resources/list", {})
        resources_data = result.get("resources", [])

        resources = []
        for res_data in resources_data:
            resource = mcp.types.Resource(
                uri=res_data.get("uri", ""),
                name=res_data.get("name"),
                description=res_data.get("description"),
                mimeType=res_data.get("mimeType"),
            )
            resources.append(resource)

        return resources

    async def read_resource(self, uri: str) -> mcp.types.ReadResourceResult:
        """
        读取资源内容

        Args:
            uri: 资源 URI

        Returns:
            资源内容
        """
        if not self._initialized:
            raise ConnectionError("客户端未初始化")

        result = await self._send_request(
            "resources/read",
            {"uri": uri},
        )

        contents = []
        for item in result.get("contents", []):
            if "text" in item:
                contents.append(
                    mcp.types.TextResourceContents(
                        uri=item.get("uri", uri),
                        mimeType=item.get("mimeType"),
                        text=item.get("text", ""),
                    )
                )
            elif "blob" in item:
                contents.append(
                    mcp.types.BlobResourceContents(
                        uri=item.get("uri", uri),
                        mimeType=item.get("mimeType"),
                        blob=item.get("blob", ""),
                    )
                )

        return mcp.types.ReadResourceResult(contents=contents)

    async def list_prompts(self) -> list[mcp.types.Prompt]:
        """
        获取服务器提供的提示模板列表

        Returns:
            提示模板列表
        """
        if not self._initialized:
            raise ConnectionError("客户端未初始化")

        if not self._server_capabilities or "prompts" not in self._server_capabilities:
            return []

        result = await self._send_request("prompts/list", {})
        prompts_data = result.get("prompts", [])

        prompts = []
        for prompt_data in prompts_data:
            prompt = mcp.types.Prompt(
                name=prompt_data.get("name", ""),
                description=prompt_data.get("description"),
                arguments=prompt_data.get("arguments"),
            )
            prompts.append(prompt)

        return prompts

    async def get_prompt(
        self,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> mcp.types.GetPromptResult:
        """
        获取提示模板内容

        Args:
            name: 提示模板名称
            arguments: 模板参数

        Returns:
            提示模板内容
        """
        if not self._initialized:
            raise ConnectionError("客户端未初始化")

        result = await self._send_request(
            "prompts/get",
            {
                "name": name,
                "arguments": arguments or {},
            },
        )

        messages = []
        for msg in result.get("messages", []):
            role = msg.get("role", "user")
            content = msg.get("content", {})

            if isinstance(content, str):
                content_obj = mcp.types.TextContent(type="text", text=content)
            elif content.get("type") == "text":
                content_obj = mcp.types.TextContent(
                    type="text",
                    text=content.get("text", ""),
                )
            elif content.get("type") == "image":
                content_obj = mcp.types.ImageContent(
                    type="image",
                    data=content.get("data", ""),
                    mimeType=content.get("mimeType", "image/png"),
                )
            else:
                content_obj = mcp.types.TextContent(
                    type="text",
                    text=str(content),
                )

            messages.append(
                mcp.types.PromptMessage(
                    role=role,
                    content=content_obj,
                )
            )

        return mcp.types.GetPromptResult(
            description=result.get("description"),
            messages=messages,
        )

    async def close(self) -> None:
        """关闭客户端连接"""
        async with self._lock:
            if not self._initialized and self._transport is None:
                return

            logger.info("关闭 SSE 客户端...")

            # 取消消息接收任务
            if self._receive_task and not self._receive_task.done():
                self._receive_task.cancel()
                try:
                    await self._receive_task
                except asyncio.CancelledError:
                    pass

            # 取消所有待处理的请求
            for future in self._pending_requests.values():
                if not future.done():
                    future.cancel()
            self._pending_requests.clear()

            # 关闭传输
            if self._transport:
                await self._transport.close()
                self._transport = None

            self._initialized = False
            logger.info("SSE 客户端已关闭")

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return bool(self._initialized and self._transport and self._transport.is_connected)

    async def __aenter__(self) -> "SSEClient":
        """异步上下文管理器入口"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.close()

    def __repr__(self):
        return f"<SSEClient url={self.url} connected={self.is_connected}>"


class MCPError(Exception):
    """MCP 协议错误"""

    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"MCP Error {code}: {message}")

    def __repr__(self):
        return f"<MCPError code={self.code} message={self.message}>"
