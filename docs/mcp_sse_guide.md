# MCP SSE 传输支持

本文档介绍 MoFox-Bot 中 MCP (Model Context Protocol) SSE 传输的使用方法。

## 概述

MCP 支持多种传输方式，MoFox-Bot 现在支持以下三种：

| 传输类型 | 协议版本 | 说明 |
|---------|---------|------|
| `streamable-http` | 2025-06-18 | 新版 Streamable HTTP 传输，单端点支持 POST/GET |
| `stdio` | - | 本地进程标准输入输出传输 |
| `sse` | 2024-11-05 | 旧版 HTTP+SSE 传输，独立的 SSE 和 POST 端点 |

## SSE 传输配置

### 配置文件

在 `config/mcp.json` 中添加 SSE 传输的服务器配置：

```json
{
  "mcpServers": {
    "my_sse_server": {
      "description": "我的 SSE MCP 服务器",
      "enabled": true,
      "transport": {
        "type": "sse",
        "url": "http://localhost:8000/sse",
        "sse_read_timeout": 300
      },
      "auth": null,
      "timeout": 30,
      "retry": {
        "max_retries": 3,
        "retry_delay": 1
      }
    }
  }
}
```

### 配置参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|-----|------|-----|-------|------|
| `type` | string | 是 | - | 必须为 `"sse"` |
| `url` | string | 是 | - | SSE 端点 URL |
| `sse_read_timeout` | number | 否 | 300 | SSE 流读取超时（秒） |

### 认证配置

支持 Bearer Token 认证：

```json
{
  "transport": {
    "type": "sse",
    "url": "https://api.example.com/sse"
  },
  "auth": {
    "type": "bearer",
    "token": "your-secret-token"
  }
}
```

## SSE 传输工作原理

HTTP+SSE 传输（2024-11-05 协议版本）的工作流程：

```
┌─────────┐                    ┌─────────┐
│  Client │                    │  Server │
└────┬────┘                    └────┬────┘
     │                              │
     │  GET /sse (建立 SSE 连接)     │
     │─────────────────────────────>│
     │                              │
     │  SSE event: endpoint         │
     │  data: /messages             │
     │<─────────────────────────────│
     │                              │
     │  POST /messages              │
     │  (JSON-RPC initialize)       │
     │─────────────────────────────>│
     │                              │
     │  SSE event: message          │
     │  data: {InitializeResult}    │
     │<─────────────────────────────│
     │                              │
     │  POST /messages              │
     │  (JSON-RPC tools/list)       │
     │─────────────────────────────>│
     │                              │
     │  SSE event: message          │
     │  data: {tools: [...]}        │
     │<─────────────────────────────│
     │                              │
```

1. 客户端通过 HTTP GET 连接到 SSE 端点
2. 服务器发送 `endpoint` 事件，告知客户端 POST 消息的端点 URL
3. 客户端通过 HTTP POST 发送 JSON-RPC 消息到该端点
4. 服务器通过 SSE 流发送 JSON-RPC 响应和通知

## 与 Streamable HTTP 的区别

| 特性 | SSE (旧版) | Streamable HTTP (新版) |
|-----|-----------|----------------------|
| 端点数量 | 2 个（SSE + POST） | 1 个 |
| 协议版本 | 2024-11-05 | 2025-06-18 |
| 会话管理 | 无 | 支持 `Mcp-Session-Id` |
| 响应方式 | 仅 SSE | 可选 SSE 或 JSON |
| 断线恢复 | 基础 `Last-Event-ID` | 完整 resumability |

## 示例：搭建 SSE MCP 服务器

使用 Python 和 FastAPI 搭建简单的 SSE MCP 服务器：

```python
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import asyncio
import json

app = FastAPI()

# 存储客户端的消息队列
message_queues = {}

@app.get("/sse")
async def sse_endpoint(request: Request):
    """SSE 端点 - 客户端连接这里接收消息"""
    client_id = id(request)
    message_queues[client_id] = asyncio.Queue()
    
    async def event_generator():
        # 发送 endpoint 事件
        yield f"event: endpoint\\ndata: /messages\\n\\n"
        
        try:
            while True:
                message = await message_queues[client_id].get()
                yield f"event: message\\ndata: {json.dumps(message)}\\n\\n"
        except asyncio.CancelledError:
            pass
        finally:
            del message_queues[client_id]
    
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"}
    )

@app.post("/messages")
async def messages_endpoint(request: Request):
    """消息端点 - 客户端发送 JSON-RPC 消息"""
    body = await request.json()
    
    # 处理 JSON-RPC 消息...
    response = handle_jsonrpc(body)
    
    # 将响应发送到 SSE 流
    for queue in message_queues.values():
        await queue.put(response)
    
    return {"status": "accepted"}
```

## 故障排除

### 连接超时

如果遇到 SSE 连接超时，可以增加 `sse_read_timeout` 配置：

```json
{
  "transport": {
    "type": "sse",
    "url": "http://localhost:8000/sse",
    "sse_read_timeout": 600
  }
}
```

### endpoint 事件未收到

确保服务器在建立 SSE 连接后立即发送 `endpoint` 事件：

```
event: endpoint
data: /your/message/endpoint

```

注意：事件数据后需要有两个换行符。

### 日志调试

启用 DEBUG 级别日志查看详细的 SSE 通信过程：

```toml
# config/bot_config.toml
[logging]
level = "DEBUG"
```

## 相关文件

- `src/plugin_system/core/mcp_sse_transport.py` - SSE 传输实现
- `src/plugin_system/core/mcp_sse_client.py` - SSE 客户端实现
- `src/plugin_system/core/mcp_client_manager.py` - MCP 客户端管理器
- `depends-data/mcp.schema.json` - 配置文件 JSON Schema
- `template/mcp.json` - 配置文件模板
