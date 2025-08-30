# TODO List:

- [x] logger使用主程序的
- [ ] 使用插件系统的config系统
- [x] 接收从napcat传递的所有信息
- [ ] <del>优化架构，各模块解耦，暴露关键方法用于提供接口</del>
- [ ] <del>单独一个模块负责与主程序通信</del>
- [ ] 使用event系统完善接口api


---
Event分为两种，一种是对外输出的event，由napcat插件自主触发并传递参数，另一种是接收外界输入的event，由外部插件触发并向napcat传递参数


## 例如，

### 对外输出的event：

napcat_on_received_text -> (message_seg: Seg)  接受到qq的文字消息,会向handler传递一个Seg
napcat_on_received_face -> (message_seg: Seg)  接受到qq的表情消息,会向handler传递一个Seg
napcat_on_received_reply -> (message_seg: Seg)  接受到qq的回复消息,会向handler传递一个Seg
napcat_on_received_image -> (message_seg: Seg)  接受到qq的图片消息,会向handler传递一个Seg
napcat_on_received_image -> (message_seg: Seg)  接受到qq的图片消息,会向handler传递一个Seg
napcat_on_received_record -> (message_seg: Seg)  接受到qq的语音消息,会向handler传递一个Seg
napcat_on_received_rps -> (message_seg: Seg)  接受到qq的猜拳魔法表情,会向handler传递一个Seg
napcat_on_received_friend_invitation -> (user_id: str)  接受到qq的好友请求,会向handler传递一个user_id
...

此类event不接受外部插件的触发，只能由napcat插件统一触发。

外部插件需要编写handler并订阅此类事件。
```python
from src.plugin_system.core.event_manager import event_manager
from src.plugin_system.base.base_event import HandlerResult

class MyEventHandler(BaseEventHandler):
    handler_name = "my_handler"
    handler_description = "我的自定义事件处理器"
    weight = 10  # 权重，越大越先执行
    intercept_message = False  # 是否拦截消息
    init_subscribe = ["napcat_on_received_text"]  # 初始订阅的事件

    async def execute(self, params: dict) -> HandlerResult:
        """处理事件"""
        try:
            message = params.get("message_seg")
            print(f"收到消息: {message.data}")
            
            # 业务逻辑处理
            # ...
            
            return HandlerResult(
                success=True,
                continue_process=True,  # 是否继续让其他处理器处理
                message="处理成功",
                handler_name=self.handler_name
            )
        except Exception as e:
            return HandlerResult(
                success=False,
                continue_process=True,
                message=f"处理失败: {str(e)}",
                handler_name=self.handler_name
            )

```

### 接收外界输入的event：

napcat_kick_group <- (user_id, group_id) 踢出某个群组中的某个用户
napcat_mute_user <- (user_id, group_id, time) 禁言某个群组中的某个用户
napcat_unmute_user <- (user_id, group_id) 取消禁言某个群组中的某个用户
napcat_mute_group <- (user_id, group_id) 禁言某个群组
napcat_unmute_group <- (user_id, group_id) 取消禁言某个群组
napcat_add_friend <- (user_id) 向某个用户发出好友请求
napcat_accept_friend <- (user_id) 接收某个用户的好友请求
napcat_reject_friend <- (user_id) 拒绝某个用户的好友请求
...
此类事件只由外部插件触发并传递参数，由napcat完成请求任务。

外部插件需要触发此类的event并传递正确的参数。

```python
from src.plugin_system.core.event_manager import event_manager

# 触发事件
await event_manager.trigger_event("napcat_accept_friend", user_id = 1234123)
```

