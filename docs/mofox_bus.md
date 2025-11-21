# MoFox Bus 消息库说明

MoFox Bus 是 MoFox Bot 自研的统一消息中台，替换第三方 `maim_message`，将核心与各平台适配器之间的通信抽象成可拓展、可热插拔的组件。该库完全异步、面向高吞吐，覆盖消息建模、序列化、传输层、运行时路由、适配器工具等多个层面。

---

## 1. 设计目标

- **通用消息模型**：统一 envelope / content，使核心逻辑不关心平台差异。
- **零拷贝字典结构**：TypedDict + dataclass，方便直接序列化 JSON。
- **高性能传输**：批量收发 + orjson 序列化 + WS/HTTP 封装。
- **适配器友好**：提供 BaseAdapter、Sink、Router 与批处理工具。
- **渐进可扩展**：未来扩充 gRPC/MQ 仅需在 `transport/` 下新增实现。

---

## 2. 包结构概览（`src/mofox_bus/`）

| 模块 | 主要职责 |
| --- | --- |
| `types.py` | TypedDict 消息模型（MessageEnvelope、Content、Sender/ChannelInfo 等）。 |
| `message_models.py` | dataclass 版 `Seg` / `MessageBase`，兼容老的消息段语义。 |
| `codec.py` | 高性能 JSON 编解码，含批量接口与 schema version 升级钩子。 |
| `runtime.py` | 消息路由/Hook/批处理调度器，支撑核心处理链。 |
| `adapter_utils.py` | BaseAdapter、CoreMessageSink、BatchDispatcher 等工具。 |
| `api.py` | WebSocket `MessageServer`/`MessageClient`，提供 token、复用 FastAPI。 |
| `router.py` | 多平台客户端统一管理，自动重连与动态路由。 |
| `transport/` | HTTP/WS server&client 轻量封装，可独立复用。 |
| `__init__.py` | 导出常用符号供外部按需引用。 |

---

## 3. 消息模型

### 3.1 Envelope TypedDict（`types.py`）

- `MessageEnvelope`：核心字段包括 `id`、`direction`、`platform`、`timestamp_ms`、`channel`、`sender`、`content` 等，一律使用毫秒时间戳，保留 `raw_platform_message` 与 `metadata` 便于调试 / 扩展。
- `Content` 联合类型支持文本、图片、音频、文件、视频、事件、命令、系统消息，后续可扩展更多 literal。
- `SenderInfo` / `ChannelInfo` / `MessageDirection` / `Role` 等均以 `Literal` 控制取值，方便 IDE 静态检查。

### 3.2 dataclass 消息段（`message_models.py`）

- `Seg`：表示一段内容，支持嵌套 `seglist`。
- `UserInfo` / `GroupInfo` / `FormatInfo` / `TemplateInfo`：保留旧结构字段，但新增 `user_avatar` 等业务常用字段。
- `BaseMessageInfo` + `MessageBase`：方便适配器沿用原始 `MessageBase` API，也使核心可以在内存中直接传递 dataclass。

> **何时使用 TypedDict vs dataclass？**  
TypedDict 更适合网络传输和依赖注入；dataclass 版 MessageBase 则保留分段消息特性，适合适配器内部加工。

---

## 4. 序列化与版本（`codec.py`）

- `dumps_message` / `loads_message`：处理单条 `MessageEnvelope`，自动补充 `schema_version`（默认 1）。
- `dumps_messages` / `loads_messages`：批量传输 `{schema_version, items: [...]}`，减少 HTTP/WS 次数。
- 预留 `_upgrade_schema_if_needed` 钩子，可在引入 v2/v3 时集中兼容逻辑。

默认使用 `orjson`，若运行环境缺失会自动 fallback 到标准库 `json`，保证兼容性。

---

## 5. 运行时调度（`runtime.py`）

- `MessageRuntime`：
  - `add_route(predicate, handler)` 或 `@runtime.route(...)` 装饰器注册消息处理器。
  - `register_before_hook` / `register_after_hook` / `register_error_hook` 注入监控、埋点、Trace。
  - `set_batch_handler` 支持一次处理整批消息（例如批量落库）。
- `MessageProcessingError` 在 handler 抛出异常时封装上下文，便于日志追踪。

运行时内部使用 `RLock` 保护路由表，适合多协程并发读写，`_maybe_await` 自动兼容同步/异步 handler。

---

## 6. 传输层封装（`transport/`）

### 6.1 HTTP
- `HttpMessageServer`：使用 `aiohttp.web` 监听 `POST /messages`，调用业务 handler 后可返回响应批。
- `HttpMessageClient`：管理 `aiohttp.ClientSession`，`send_messages(messages, expect_reply=True)` 支持同步等待回复。

### 6.2 WebSocket
- `WsMessageServer`：基于 `aiohttp`，维护连接集合，支持 `broadcast`。
- `WsMessageClient`：自动重连、后台读取，`send_messages`/`send_message` 直接发送批量。

以上都复用了 `codec` 批量协议，统一上下游格式。

---

## 7. Server / Client / Router（`api.py`、`router.py`）

### 7.1 MessageServer
- 可复用已有 FastAPI 实例（`app=get_global_server().get_app()`），在同一进程内共享路由。
- 支持 header token 校验 (`enable_token` + `add_valid_token`)。
- `broadcast_message`、`broadcast_to_platform`、`send_message(message: MessageBase)` 满足不同场景。

### 7.2 MessageClient
- 仅 WebSocket 模式，管理 `aiohttp` 连接、自动收消息，供适配器推送到核心。

### 7.3 Router
- `RouteConfig` + `TargetConfig` 描述平台到 URL 的映射。
- `Router.run()` 会为每个平台创建 `MessageClient` 并保持心跳，`_monitor_connections` 自动重连。
- `register_class_handler` 可绑定 Napcat 适配器那样的 class handler。

---

## 8. 适配器工具（`adapter_utils.py`）

- `BaseAdapter`：约定入站 `from_platform_message` / 出站 `_send_platform_message`，默认提供批量入口。
- `CoreMessageSink` 协议 + `InProcessCoreSink`：方便在同进程中把适配器消息直接推给核心协程。
- `BatchDispatcher`：封装缓冲 + 定时 flush 的发送管道，可与 HTTP/WS 客户端组合提升吞吐。

---

## 9. 集成与配置

1. **配置文件**：在 `config.*.toml` 中新增 `[message_bus]` 段（参考 `template/bot_config_template.toml`），控制 host/port/token/wss 等。
2. **服务启动**：`src/common/message/api.py` 中的 `get_global_api()` 已默认实例化 `MessageServer`，并将 token 写入服务器。
3. **适配器更新**：所有使用原 `maim_message` 的模块已改为 `from mofox_bus import ...`，无需额外适配即可继续利用 `MessageBase` / `Router` API。

---

## 10. 快速上手示例

```python
from mofox_bus import MessageRuntime, types
from mofox_bus.transport import HttpMessageServer

runtime = MessageRuntime()

@runtime.route(lambda env: env["content"]["type"] == "text")
async def handle_text(env: types.MessageEnvelope):
    print("收到文本：", env["content"]["text"])

async def http_handler(messages: list[types.MessageEnvelope]):
    await runtime.handle_batch(messages)

server = HttpMessageServer(http_handler)
app = server.make_app()  # 交给 aiohttp/uvicorn 运行
```

**适配器 Skeleton：**
```python
from mofox_bus import (
    BaseAdapter,
    MessageEnvelope,
    WebSocketAdapterOptions,
)

class MyAdapter(BaseAdapter):
    platform = "custom"

    def __init__(self, core_sink):
        super().__init__(
            core_sink,
            transport=WebSocketAdapterOptions(
                url="ws://127.0.0.1:19898",
                incoming_parser=lambda raw: orjson.loads(raw)["payload"],
            ),
        )

    def from_platform_message(self, raw: dict) -> MessageEnvelope:
        return {
            "id": raw["id"],
            "direction": "incoming",
            "platform": self.platform,
            "timestamp_ms": raw["ts"],
            "channel": {"channel_id": raw["room_id"], "channel_type": "dm"},
            "sender": {"user_id": raw["user_id"], "role": "user"},
            "content": {"type": "text", "text": raw["content"]},
            "conversation_id": raw["room_id"],
        }
```

- 如果传入 `WebSocketAdapterOptions`，BaseAdapter 会自动建立连接、监听、默认封装 `{"type":"message","payload":...}` 的标准 JSON，并允许通过 `outgoing_encoder` 自定义下行格式。
- 如果传入 `HttpAdapterOptions`，BaseAdapter 会自动启动一个 aiohttp Webhook（`POST /adapter/messages`）并将收到的 JSON 批量投递给核心。

> 完整的 WebSocket 适配器示例见 `examples/mofox_bus_demo_adapter.py`：演示了平台提供 WS 接口、适配器通过 `WebSocketAdapterOptions` 自动启动监听、接收/处理/回发的全过程，可直接运行观察日志。

---

## 11. 调试与最佳实践

- 利用 `MessageRuntime.register_error_hook` 打印 `correlation_id` / `id`，快速定位异常消息。
- 如果适配器与核心同进程，优先使用 `InProcessCoreSink` 以避免 JSON 编解码。
- 批量吞吐场景（如 HTTP 推送）优先通过 `BatchDispatcher` 聚合再发送，可显著降低连接开销。
- 自定义传输实现可参考 `transport/http_server.py` / `ws_client.py`，保持 `loads_messages` / `dumps_messages` 协议即可与现有核心互通。

---

通过以上结构，MoFox Bus 提供了一套端到端的统一消息能力，满足 AI Bot 在多平台、多形态场景下的高性能传输与扩展需求。若需要扩展新的传输协议或内容类型，只需在对应模块增加 Literal/TypedDict/Transport 实现即可。祝使用愉快！
