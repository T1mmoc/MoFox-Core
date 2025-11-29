# 沙盒插件系统

## 概述

MoFox-Bot 的沙盒插件系统为不受信任的第三方插件提供安全隔离的执行环境。通过资源限制、API白名单和受限的Python环境，确保插件无法危害系统安全。

## 核心特性

### 🔒 安全隔离

- **受限的模块导入**: 只能导入白名单中的标准库模块
- **禁止危险操作**: 禁用 `eval`、`exec`、`compile`、`__import__` 等危险函数
- **文件系统隔离**: 默认无法访问文件系统（除非明确授权）
- **网络隔离**: 默认无法进行网络请求（除非明确授权）

### ⚡ 资源限制

- **执行时间限制**: 防止无限循环或长时间运行
- **内存限制**: 限制最大内存使用（Unix/Linux）
- **CPU时间限制**: 限制CPU占用时间（Unix/Linux）

### 🛡️ API访问控制

- 通过代理提供受限的API接口
- 只能访问白名单中的功能
- 所有操作都有日志记录

## 架构设计

### 核心组件

```
沙盒插件系统
├── sandbox_environment.py      # 沙盒执行环境
├── sandbox_plugin.py           # 沙盒插件基类
└── sandbox_components.py       # 沙盒Action/Command组件
```

### 执行流程

```
插件代码 → 代码审查 → 沙盒环境 → 资源限制 → API代理 → 执行结果
```

## 使用指南

### 1. 创建沙盒插件

```python
from src.plugin_system import (
    register_plugin,
    SandboxPlugin,
    SandboxAction,
    ActionInfo,
)

# 定义沙盒Action
class MySandboxAction(SandboxAction):
    action_name = "my_sandbox_action"
    activation_keywords = ["触发词"]
    priority = 50
    sandbox_timeout = 5.0  # 5秒超时
    
    def get_action_code(self) -> str:
        """返回要在沙盒中执行的代码"""
        return """
# 这里的代码在沙盒中执行
# 可以访问: message_text, user_id, group_id, platform
# 可以使用: api['log']() 等安全API

import math

# 处理消息
result = f"收到消息: {message_text}"
api['log'](result)

# 设置返回值
__result__ = result
"""
    
    async def _handle_sandbox_result(self, result, chat_stream):
        """处理执行结果"""
        if result.get("success"):
            from src.plugin_system.apis import send_api
            await send_api.text_to_stream(
                text=result.get("result", ""),
                stream_id=chat_stream.stream_id,
            )
            return True
        return False
    
    @classmethod
    def get_action_info(cls) -> ActionInfo:
        return ActionInfo(
            action_name=cls.action_name,
            activation_keywords=cls.activation_keywords,
            priority=cls.priority,
            description="沙盒Action示例",
        )


# 定义沙盒插件
@register_plugin
class MySandboxPlugin(SandboxPlugin):
    plugin_name = "my_sandbox_plugin"
    config_file_name = "my_sandbox_config.toml"
    enable_plugin = True
    
    def get_plugin_components(self):
        return [
            (MySandboxAction.get_action_info(), MySandboxAction),
        ]
```

### 2. 配置沙盒环境

在 `_manifest.json` 中配置：

```json
{
  "name": "my_sandbox_plugin",
  "display_name": "我的沙盒插件",
  "version": "1.0.0",
  "description": "一个安全的沙盒插件",
  "author": "Your Name",
  "trust_level": "sandboxed",
  "sandbox_config": {
    "max_execution_time": 10.0,
    "max_memory_mb": 256,
    "max_cpu_time": 5.0,
    "allow_network": false,
    "allow_file_read": false,
    "allow_file_write": false,
    "allowed_modules": [
      "json",
      "re",
      "datetime",
      "math",
      "random"
    ]
  }
}
```

### 3. 自定义沙盒配置

在插件类中覆盖配置：

```python
from src.plugin_system import SandboxPlugin, SandboxConfig

class MySandboxPlugin(SandboxPlugin):
    # 自定义沙盒配置
    sandbox_config = SandboxConfig(
        max_execution_time=15.0,  # 15秒超时
        max_memory_mb=512,         # 512MB内存
        allow_network=True,        # 允许网络访问
        allowed_modules=[
            "json",
            "re",
            "datetime",
            "requests",  # 额外允许requests模块
        ],
    )
```

## 沙盒中的可用资源

### 默认允许的模块

```python
allowed_modules = [
    "json",         # JSON处理
    "re",           # 正则表达式
    "datetime",     # 日期时间
    "time",         # 时间相关
    "math",         # 数学函数
    "random",       # 随机数
    "collections",  # 集合类型
    "itertools",    # 迭代工具
    "functools",    # 函数工具
    "typing",       # 类型提示
]
```

### 可用的内置函数

```python
safe_builtins = {
    "abs", "all", "any", "bool", "dict", "enumerate",
    "filter", "float", "int", "len", "list", "map",
    "max", "min", "print", "range", "reversed", "round",
    "set", "sorted", "str", "sum", "tuple", "zip"
}
```

### 沙盒上下文变量

在 `get_action_code()` 返回的代码中可以访问：

```python
# Action中的可用变量
message_text  # 消息文本
user_id       # 用户ID
group_id      # 群组ID（如果在群聊中）
platform      # 平台标识

# 安全API
api['log'](msg)  # 日志记录

# 返回结果
__result__ = "要返回的值"  # 设置返回值
```

## 安全限制

### 禁止的操作

❌ **动态代码执行**
```python
eval("...")      # ❌ 禁止
exec("...")      # ❌ 禁止
compile("...")   # ❌ 禁止
```

❌ **文件操作**
```python
open("file.txt")          # ❌ 禁止
os.remove("file.txt")     # ❌ 禁止（os模块不在白名单）
```

❌ **系统调用**
```python
os.system("cmd")          # ❌ 禁止
subprocess.run(["ls"])    # ❌ 禁止（subprocess不在白名单）
```

❌ **网络请求**（默认禁止）
```python
import requests           # ❌ 默认禁止
import urllib             # ❌ 默认禁止
```

❌ **危险内置函数**
```python
__import__("os")          # ❌ 禁止
globals()                 # ❌ 禁止
locals()                  # ❌ 禁止
getattr(obj, "attr")      # ❌ 禁止
```

### 资源限制

| 资源类型 | 默认限制 | 说明 |
|---------|---------|------|
| 执行时间 | 30秒 | 超时后抛出 `SandboxTimeoutError` |
| 内存使用 | 256MB | 仅Unix/Linux系统有效 |
| CPU时间 | 10秒 | 仅Unix/Linux系统有效 |

## 异常处理

### 沙盒异常类型

```python
from src.plugin_system import (
    SandboxTimeoutError,    # 执行超时
    SandboxMemoryError,     # 内存超限
    SandboxSecurityError,   # 安全违规
)
```

### 异常处理示例

```python
try:
    result = await sandbox.execute_async(code, context)
    
    if not result.get("success"):
        error_type = result.get("error_type")
        error_msg = result.get("error")
        
        if error_type == "SandboxTimeoutError":
            print("执行超时")
        elif error_type == "SandboxMemoryError":
            print("内存超限")
        elif error_type == "SandboxSecurityError":
            print("安全违规")
            
except Exception as e:
    print(f"执行异常: {e}")
```

## 最佳实践

### ✅ 推荐做法

1. **明确资源限制**: 根据插件功能设置合理的超时和内存限制
2. **最小权限原则**: 只授予插件必需的权限
3. **输入验证**: 在沙盒外验证用户输入
4. **错误处理**: 妥善处理沙盒执行失败的情况
5. **日志记录**: 记录所有沙盒执行的关键操作

### ❌ 避免做法

1. **过度信任**: 不要假设沙盒代码一定安全
2. **敏感数据**: 不要在沙盒中处理敏感信息
3. **长时间运行**: 避免设置过长的超时时间
4. **复杂计算**: 避免在沙盒中进行大量计算

## 示例插件

完整的示例插件位于 `plugins/example_sandbox_plugin/`

### 功能演示

1. **数字平方计算**: 当消息包含"计算平方"时，提取数字并计算平方
2. **资源限制**: 5秒超时，128MB内存限制
3. **模块白名单**: 只允许 json、re、datetime、math、random

## 性能考虑

### 执行开销

- 沙盒初始化: ~1ms
- 代码编译: ~5ms
- 执行开销: 取决于代码复杂度
- 资源监控: ~1ms

### 优化建议

1. 复用沙盒环境实例
2. 缓存编译后的代码对象
3. 限制沙盒中的循环次数
4. 使用异步执行避免阻塞

## 故障排除

### 常见问题

**Q: Windows系统上资源限制不生效？**
A: Windows不支持 `resource` 模块，内存和CPU限制仅在Unix/Linux上有效。但执行时间限制在所有平台都有效。

**Q: 如何允许网络访问？**
A: 在沙盒配置中设置 `allow_network=True`，并将 `requests` 等模块添加到 `allowed_modules`。

**Q: 执行超时但代码没有问题？**
A: 检查是否有无限循环或大量计算。增加 `max_execution_time` 或优化代码。

**Q: 沙盒代码无法访问插件配置？**
A: 使用 `get_sandbox_safe_api()` 方法提供安全的配置访问接口。

## 安全审计

### 审计日志

所有沙盒执行都会记录：
- 执行时间
- 执行结果
- 错误信息
- 资源使用情况

### 安全检查清单

- [ ] 沙盒配置合理（超时、内存限制）
- [ ] 模块白名单最小化
- [ ] 禁用文件和网络访问（除非必需）
- [ ] 输入验证和清理
- [ ] 错误处理完善
- [ ] 日志记录完整

## 未来改进

- [ ] 支持更细粒度的API权限控制
- [ ] 添加代码静态分析
- [ ] 支持沙盒间通信
- [ ] 提供沙盒性能分析工具
- [ ] 支持Docker容器隔离（高级模式）

## 参考资料

- [Python安全编程最佳实践](https://docs.python.org/3/library/security_warnings.html)
- [RestrictedPython项目](https://github.com/zopefoundation/RestrictedPython)
- [沙盒逃逸防护指南](https://owasp.org/www-community/attacks/Code_Injection)
