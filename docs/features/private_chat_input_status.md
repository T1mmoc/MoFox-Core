# 好友聊天输入状态功能说明

## 功能概述

在好友私聊场景中，当 MoFox-Bot 准备生成回复时，会自动发送"标记消息已读"命令来模拟输入状态，让对方感知到机器人正在回复。

## 实现原理

由于 OneBot 11 协议没有直接的"设置输入状态"API，本功能通过调用 `mark_private_msg_as_read` 命令来实现类似效果。当用户看到消息被标记为已读时，通常意味着对方正在准备回复。

## 修改内容

### 1. 添加新的命令类型

**文件**: `src/plugins/built_in/napcat_adapter/src/event_models.py`

在 `CommandType` 枚举中添加：
```python
MARK_MSG_AS_READ = "mark_private_msg_as_read"  # 标记私聊消息已读（模拟输入状态）
```

### 2. 实现命令处理器

**文件**: `src/plugins/built_in/napcat_adapter/src/handlers/to_napcat/send_handler.py`

添加 `handle_mark_msg_as_read_command` 方法来处理标记已读命令。

### 3. 新增 Send API

**文件**: `src/plugin_system/apis/send_api.py`

添加 `mark_msg_as_read_to_stream` 函数，提供便捷的 API 调用：

```python
async def mark_msg_as_read_to_stream(
    stream_id: str,
    storage_message: bool = False,
) -> bool:
    """向指定私聊流发送标记消息已读命令（模拟输入状态）"""
```

**特性**：
- 自动检测是否为私聊场景（群聊不支持）
- 不存储到数据库（默认 `storage_message=False`）
- 异步非阻塞执行

### 4. 集成到回复生成流程

**文件**: `src/chat/replyer/default_generator.py`

在 `generate_reply_with_context` 方法中，在调用 LLM 生成回复之前，对好友私聊自动发送标记已读：

```python
# 在好友聊天中，发送标记已读命令（模拟输入状态）
if not self.chat_stream.group_info:
    try:
        from src.plugin_system.apis import send_api
        
        asyncio.create_task(
            send_api.mark_msg_as_read_to_stream(
                stream_id=self.chat_stream.stream_id,
                storage_message=False,
            )
        )
        logger.debug(f"[{self.chat_stream.stream_id}] 已发送输入状态（标记已读）")
    except Exception as mark_error:
        logger.warning(f"发送输入状态失败: {mark_error}")
```

## 使用场景

### 自动触发

在好友私聊场景中，当 Bot 需要生成回复时，会自动触发输入状态：

1. 用户发送消息给 Bot
2. Bot 判断需要回复
3. **自动发送标记已读命令** ← 新增
4. Bot 调用 LLM 生成回复内容
5. Bot 发送回复给用户

### 手动调用

也可以在插件或其他代码中手动调用：

```python
from src.plugin_system.apis import send_api

# 在私聊中模拟输入状态
await send_api.mark_msg_as_read_to_stream(stream_id="qq:private:123456")
```

## 注意事项

1. **仅支持私聊**: 群聊不支持标记已读功能，调用时会自动跳过
2. **非阻塞**: 使用 `asyncio.create_task` 异步发送，不会阻塞 LLM 生成流程
3. **容错处理**: 发送失败不会影响正常的回复生成
4. **不存储消息**: 标记已读命令本身不会被存储到数据库

## 测试方法

1. 启动 MoFox-Bot 并连接 NapCat 适配器
2. 使用 QQ 好友身份给 Bot 发送消息
3. 观察 QQ 客户端上的消息状态：
   - 消息应该很快被标记为"已读"
   - 随后 Bot 会发送回复

## 技术细节

### 执行时机

标记已读命令在以下时机发送：
- ✅ 在 `POST_LLM` 事件触发之后
- ✅ 在实际调用 LLM 模型之前
- ✅ 在 `is_replying` 状态设置之前

### 性能影响

- 异步非阻塞，不增加回复延迟
- 命令执行失败不影响正常流程
- 单次网络请求，开销极小

## 相关配置

无需额外配置，功能开箱即用。如需禁用，可以在代码中注释掉相关调用。

## 未来改进

- [ ] 支持配置项来开关此功能
- [ ] 支持多次标记已读（模拟持续输入）
- [ ] 探索 NapCat 是否支持更原生的输入状态 API

## 参考资料

- [OneBot 11 标准](https://github.com/botuniverse/onebot-11)
- [NapCat 文档](https://napcat.napneko.icu/)
- MoFox-Bot 插件系统文档: `docs/plugins/`
