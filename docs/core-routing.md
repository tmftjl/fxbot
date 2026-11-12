# 核心协议与消息路由

`core` 只依赖标准库，不在导入时注册入口、建立连接或启动后台任务。
平台协议放在 `core/platforms/`，业务通过核心事件和客户端接口处理消息。

## 消息入口

OneBot V11 使用 `parse_event(payload, bot_id=...)` 解析平台消息段数组。
`bot_id` 来自已确定身份的连接，必须与事件中的 `self_id` 一致。
解析器复制消息数据，保留原始消息、提及、引用 ID、时间和发送者角色；不查询引用内容。
群消息处理文本可以去掉首尾直接相邻的自身提及，原始消息不变；不推断昵称唤醒或被引用者身份。

当前只支持群和私聊消息段数组。通知、请求、CQ 字符串和匿名消息明确报不支持，不能当作正常用户消息放行。
未知入站段保留为 `onebot11:<type>`，但不能直接发送；出站消息仅编码明确支持的段。
文本始终使用消息数组发送，不能把用户文本中的 CQ 标记当成可执行段。

## 路由规则

- 预处理器按注册顺序执行，必须返回严格的 `True` 才放行。
- 命令大小写敏感，前缀由注册者显式写入命令词；命令与参数以空白分隔。
- 路由按优先级从小到大执行，同优先级按注册顺序串行执行。
- 匹配和规则通过后检查权限；没有权限函数表示公开，权限返回非 `True` 跳过该处理器。
  单个命令权限拒绝不等于全局门禁拒绝，后续公开路由仍可处理；全局门禁必须放在预处理器。
- `block=True` 在处理器正常结束后停止所有剩余路由，包括同优先级路由。
- `finish()` 可选发送消息并结束当前处理器，本身不改变阻断设置。
- 规则、权限、处理器或发送发生异常时记录服务器日志并停止本次分发，不向用户发送内部异常。
- 取消异常向上抛出；正常、拒绝、失败和取消路径都会恢复事件上下文。
- 注册表在分发开始时取快照，处理中新增的路由或准入检查只影响下一次分发。

## 使用方式

```python
from core import Router, HandlerContext
from core.platforms.onebot11 import OneBot11Client, parse_event

router = Router()


async def ping(context: HandlerContext) -> None:
    await context.finish("pong")


router.command("ping", "/ping", ping)
```

协议连接提供异步 `sender(action, params)` 后构造 `OneBot11Client(bot_id, sender)`，
将收到的协议数据交给 `parse_event`，再调用 `router.dispatch(client, event)`。
客户端只做动作和消息编码，连接鉴权、请求与响应关联、超时和断线清理由传输层负责。
当前仅有测试传输函数，没有可对外监听的网络服务，也没有持久化处理。

验证：`python -I -S -m unittest discover -s tests/migration -t .`。
