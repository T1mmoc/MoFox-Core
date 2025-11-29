"""
MCP SSE Transport

实现 MCP 协议的 HTTP+SSE 传输（2024-11-05 版本）
支持通过 SSE 连接接收服务器消息，通过 HTTP POST 发送客户端消息

该传输方式的工作流程：
1. 客户端通过 GET 请求连接到 SSE 端点
2. 服务器发送 'endpoint' 事件，告知客户端 POST 消息的端点 URL
3. 客户端通过 HTTP POST 向该端点发送 JSON-RPC 消息
4. 服务器通过 SSE 流发送 JSON-RPC 响应和通知
"""

import asyncio
from typing import Any
from collections.abc import AsyncIterator

import httpx
import orjson

from src.common.logger import get_logger

logger = get_logger("mcp_sse_transport")


class SSEEvent:
    """SSE 事件解析结果"""

    def __init__(self, event: str = "message", data: str = "", event_id: str | None = None, retry: int | None = None):
        self.event = event
        self.data = data
        self.event_id = event_id
        self.retry = retry

    def __repr__(self):
        return f"<SSEEvent event={self.event} data_len={len(self.data)} id={self.event_id}>"


class SSEParser:
    """SSE 事件流解析器"""

    def __init__(self):
        self._event = "message"
        self._data_lines: list[str] = []
        self._id: str | None = None
        self._retry: int | None = None

    def parse_line(self, line: str) -> SSEEvent | None:
        """
        解析单行 SSE 数据

        Args:
            line: SSE 行数据

        Returns:
            SSEEvent: 如果完成一个事件则返回，否则返回 None
        """
        # 空行表示事件结束
        if not line:
            if self._data_lines:
                event = SSEEvent(
                    event=self._event,
                    data="\n".join(self._data_lines),
                    event_id=self._id,
                    retry=self._retry,
                )
                # 重置状态（保留 id）
                self._event = "message"
                self._data_lines = []
                self._retry = None
                return event
            return None

        # 忽略注释
        if line.startswith(":"):
            return None

        # 解析字段
        if ":" in line:
            field, _, value = line.partition(":")
            # 移除值前的单个空格（如果存在）
            if value.startswith(" "):
                value = value[1:]
        else:
            field = line
            value = ""

        if field == "event":
            self._event = value
        elif field == "data":
            self._data_lines.append(value)
        elif field == "id":
            if "\x00" not in value:  # 忽略包含 NULL 的 id
                self._id = value
        elif field == "retry":
            try:
                self._retry = int(value)
            except ValueError:
                pass

        return None


class SSETransport:
    """
    MCP SSE 传输实现

    实现 HTTP+SSE 传输协议，用于与支持该传输方式的 MCP 服务器通信
    """

    def __init__(
        self,
        sse_url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
        sse_read_timeout: float = 300.0,
    ):
        """
        初始化 SSE 传输

        Args:
            sse_url: SSE 端点 URL
            headers: 额外的 HTTP 请求头
            timeout: HTTP 请求超时（秒）
            sse_read_timeout: SSE 读取超时（秒）
        """
        self.sse_url = sse_url
        self.headers = headers or {}
        self.timeout = timeout
        self.sse_read_timeout = sse_read_timeout

        # 运行时状态
        self._post_endpoint: str | None = None
        self._client: httpx.AsyncClient | None = None
        self._sse_response: httpx.Response | None = None
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._sse_task: asyncio.Task | None = None
        self._connected = False
        self._last_event_id: str | None = None
        self._lock = asyncio.Lock()

        logger.debug(f"SSE 传输初始化: {sse_url}")

    async def connect(self) -> None:
        """
        建立 SSE 连接

        连接到 SSE 端点并等待接收 'endpoint' 事件
        """
        async with self._lock:
            if self._connected:
                logger.debug("SSE 传输已连接，跳过")
                return

            logger.info(f"正在连接 SSE 端点: {self.sse_url}")

            # 创建 HTTP 客户端
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, read=self.sse_read_timeout),
                headers={
                    "Accept": "text/event-stream",
                    "Cache-Control": "no-cache",
                    **self.headers,
                },
            )

            # 添加 Last-Event-ID 头（如果有）
            request_headers = {}
            if self._last_event_id:
                request_headers["Last-Event-ID"] = self._last_event_id

            # 创建 SSE 请求
            request = self._client.build_request(
                "GET",
                self.sse_url,
                headers=request_headers,
            )

            # 发送请求并获取响应（不使用 async with，手动管理生命周期）
            self._sse_response = await self._client.send(request, stream=True)

            # 检查响应状态
            if self._sse_response.status_code != 200:
                await self._sse_response.aclose()
                raise ConnectionError(
                    f"SSE 连接失败: HTTP {self._sse_response.status_code}"
                )

            # 检查 Content-Type
            content_type = self._sse_response.headers.get("content-type", "")
            if "text/event-stream" not in content_type:
                await self._sse_response.aclose()
                raise ConnectionError(
                    f"无效的 Content-Type: {content_type}，期望 text/event-stream"
                )

            # 启动 SSE 读取任务
            self._sse_task = asyncio.create_task(self._read_sse_events())

            # 等待接收 endpoint 事件
            endpoint_received = False
            wait_timeout = self.timeout

            try:
                while not endpoint_received:
                    try:
                        msg = await asyncio.wait_for(
                            self._message_queue.get(),
                            timeout=wait_timeout,
                        )

                        # 检查是否是 endpoint 事件的数据
                        if isinstance(msg, dict) and "_sse_endpoint" in msg:
                            self._post_endpoint = msg["_sse_endpoint"]
                            endpoint_received = True
                            logger.info(f"收到 POST 端点: {self._post_endpoint}")
                        else:
                            # 其他消息放回队列
                            await self._message_queue.put(msg)
                            break

                    except asyncio.TimeoutError:
                        raise TimeoutError(
                            f"等待 endpoint 事件超时 ({wait_timeout}s)"
                        ) from None

            except Exception:
                await self.close()
                raise

            self._connected = True
            logger.info("SSE 传输连接成功")

    async def _read_sse_events(self) -> None:
        """
        读取并解析 SSE 事件流

        将解析后的消息放入队列供 receive() 方法获取
        """
        parser = SSEParser()
        buffer = ""

        try:
            async for chunk in self._sse_response.aiter_text():
                buffer += chunk

                # 按行分割处理
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")

                    event = parser.parse_line(line)
                    if event:
                        await self._handle_sse_event(event)

            # 处理缓冲区剩余内容
            if buffer:
                event = parser.parse_line(buffer)
                if event:
                    await self._handle_sse_event(event)
                # 触发最后一个事件
                event = parser.parse_line("")
                if event:
                    await self._handle_sse_event(event)

        except httpx.ReadError as e:
            logger.warning(f"SSE 读取错误: {e}")
        except asyncio.CancelledError:
            logger.debug("SSE 读取任务被取消")
        except Exception as e:
            logger.error(f"SSE 事件处理异常: {e}")
        finally:
            self._connected = False
            logger.info("SSE 连接已关闭")

    async def _handle_sse_event(self, event: SSEEvent) -> None:
        """
        处理 SSE 事件

        Args:
            event: 解析后的 SSE 事件
        """
        # 更新 last event id
        if event.event_id is not None:
            self._last_event_id = event.event_id

        logger.debug(f"收到 SSE 事件: {event}")

        if event.event == "endpoint":
            # endpoint 事件：包含 POST 消息的端点 URL
            endpoint_url = event.data.strip()

            # 处理相对 URL
            if endpoint_url.startswith("/"):
                # 从 SSE URL 提取基础 URL
                from urllib.parse import urlparse, urlunparse

                parsed = urlparse(self.sse_url)
                endpoint_url = urlunparse(
                    (parsed.scheme, parsed.netloc, endpoint_url, "", "", "")
                )

            await self._message_queue.put({"_sse_endpoint": endpoint_url})

        elif event.event == "message":
            # message 事件：包含 JSON-RPC 消息
            if event.data:
                try:
                    message = orjson.loads(event.data)
                    await self._message_queue.put(message)
                except orjson.JSONDecodeError as e:
                    logger.error(f"JSON 解析失败: {e} | 数据: {event.data[:200]}")

        else:
            logger.debug(f"忽略未知事件类型: {event.event}")

    async def send(self, message: dict[str, Any]) -> None:
        """
        发送 JSON-RPC 消息到服务器

        Args:
            message: JSON-RPC 消息字典
        """
        if not self._connected or not self._post_endpoint:
            raise ConnectionError("SSE 传输未连接或未获取 POST 端点")

        logger.debug(f"发送消息到 {self._post_endpoint}: {message}")

        try:
            response = await self._client.post(
                self._post_endpoint,
                content=orjson.dumps(message),
                headers={
                    "Content-Type": "application/json",
                    **self.headers,
                },
            )

            if response.status_code not in (200, 202, 204):
                logger.error(
                    f"POST 请求失败: HTTP {response.status_code} | {response.text}"
                )
                raise ConnectionError(
                    f"发送消息失败: HTTP {response.status_code}"
                )

        except httpx.RequestError as e:
            logger.error(f"POST 请求异常: {e}")
            raise ConnectionError(f"发送消息失败: {e}") from e

    async def receive(self, timeout: float | None = None) -> dict[str, Any]:
        """
        接收服务器消息

        Args:
            timeout: 超时时间（秒），None 表示无限等待

        Returns:
            JSON-RPC 消息字典
        """
        if not self._connected:
            raise ConnectionError("SSE 传输未连接")

        try:
            if timeout is not None:
                message = await asyncio.wait_for(
                    self._message_queue.get(),
                    timeout=timeout,
                )
            else:
                message = await self._message_queue.get()

            # 过滤内部消息
            if isinstance(message, dict) and "_sse_endpoint" in message:
                # 这是 endpoint 事件，不应该返回给调用者
                return await self.receive(timeout)

            return message

        except asyncio.TimeoutError as e:
            raise TimeoutError("接收消息超时") from e

    async def receive_iter(self) -> AsyncIterator[dict[str, Any]]:
        """
        迭代接收服务器消息

        Yields:
            JSON-RPC 消息字典
        """
        while self._connected:
            try:
                message = await self.receive(timeout=1.0)
                yield message
            except TimeoutError:
                continue
            except ConnectionError:
                break

    async def close(self) -> None:
        """关闭 SSE 连接"""
        async with self._lock:
            if not self._connected and self._client is None:
                return

            logger.info("正在关闭 SSE 传输...")

            # 取消 SSE 读取任务
            if self._sse_task and not self._sse_task.done():
                self._sse_task.cancel()
                try:
                    await self._sse_task
                except asyncio.CancelledError:
                    pass

            # 关闭 SSE 响应
            if self._sse_response:
                await self._sse_response.aclose()
                self._sse_response = None

            # 关闭 HTTP 客户端
            if self._client:
                await self._client.aclose()
                self._client = None

            self._connected = False
            self._post_endpoint = None
            logger.info("SSE 传输已关闭")

    @property
    def is_connected(self) -> bool:
        """是否已连接"""
        return self._connected

    async def __aenter__(self) -> "SSETransport":
        """异步上下文管理器入口"""
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.close()

    def __repr__(self):
        return f"<SSETransport url={self.sse_url} connected={self._connected}>"
