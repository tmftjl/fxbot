# QQ 官方适配器

本次对齐以 `temp/adapter-qq-reference/nonebot/adapters/qq`（本地版本 `11c0991`）为源码基准，
范围为普通群、C2C、公共鉴权和网关；频道/论坛/音频频道不扩展、不计入对齐验收。
FxBot 不依赖 NoneBot，不复制宿主、驱动器、环境变量配置或旧接口兼容层。
改写参考 API 的 MIT 许可随包保存在 `platforms/qq/resources/licenses/adapter-qq.txt`。

## 覆盖范围

| 领域 | 已接入 |
| --- | --- |
| 连接 | Webhook 验签/握手、WebSocket Identify、心跳 ACK、Resume、退避、会话额度、自动及指定分片 |
| 消息 | 普通群、群 AT、C2C、文本、提及、表情、引用正文及引用索引 |
| 高级消息 | Markdown、模板键盘/自定义键盘、Ark、Embed、预上传媒体；C2C 流式消息、提示键盘、操作按钮 |
| 文件 | URL、内联字节、预上传、无鉴权预签名 PUT、分片完成通知、合并、群/C2C 文件发送 |
| 通知 | 好友增删、收发开关、机器人入退群、群消息开关、成员入退群、入群申请、交互、审核结果 |
| 群管理 | 成员列表/详情、群资料、Bot 状态、禁言设置、申请列表/审批、移除成员、黑名单查询/修改 |
| 撤回 | 按明确目标撤回群/C2C 消息；通用撤回能力可撤回本客户端发送记录中的消息 |

## 平台 API

QQ 独有接口通过显式能力取得，不给最小 BotClient 增加平台方法：

```python
from fxbot.platforms.qq import QQ_API

api = ctx.bot.capabilities.require(QQ_API)
info = await api.get_group_info(group_id=ctx.event.target.target_id)
await api.approval_join_request(
    group_id=ctx.event.target.target_id,
    member_openid="官方成员 OpenID",
    op="approve",
    join_request_id="申请 ID",
)
```

这些接口只做协议调用，不自动套用插件权限；插件须先完成自己的权限和业务校验。
返回值为 JSON 字典，不是 NoneBot 的 Pydantic 模型；字段与路径保持参考协议语义。

29 个公开异步入口：

- 公共：`me`、`url_get`、`shard_url_get`、`put_interaction`。
- C2C：`post_c2c_messages`、`post_c2c_files`、`post_c2c_upload`、
  `post_c2c_upload_prepare`、`post_c2c_upload_part_finish`、`delete_c2c_message`。
- 群消息：`post_group_messages`、`post_group_files`、`post_group_upload`、
  `post_group_upload_prepare`、`post_group_upload_part_finish`、`delete_group_message`。
- 上传：`put_upload_part`。
- 群资料及成员：`post_group_members`、`get_group_info`、`get_group_bot_state`、
  `get_group_members`、`get_group_member`。
- 管理：`get_group_mute_setting`、`set_group_members_mute`、`get_group_join_request_list`、
  `approval_join_request`、`batch_remove_group_members`、`get_group_member_blacklist`、`post_group_member_blacklist`。

## 消息声明

```python
from fxbot.core import Message
from fxbot.platforms.qq.message import markdown, keyboard

await ctx.send(Message([
    markdown({"custom_template_id": "模板 ID", "params": [{"key": "title", "values": ["标题"]}]}),
    keyboard({"id": "键盘模板 ID"}),
]))
```

模块也提供 `ark`、`embed`、`media`、`emoji`、`stream`、`prompt_keyboard`、`action_button`。
官方结构直接使用字典；核心 Message 只保存 `qq:*` 消息段，不认识平台业务。
重复高级段、多项媒体或不支持的消息组合会明确拒绝，不自动拆成多条消息。
被动消息自动使用当前消息的 msg_id/msg_seq；支持事件被动回复的通知只使用 event_id，不自动添加 msg_seq。
按官方列表接受交互、机器人入群、群/C2C 恢复消息接收、好友新增事件；成员入群事件按本地参考
`bot.py` 的 `GroupMemberAddEvent` 分支同样支持事件回复。其他通知按主动发送处理，
仍需账号具备主动消息权限，不会借用不支持的 event_id。
通知处理器可从 `ctx.event.metadata["qq_data"]` 获取原始业务字段。

## 配置与生命周期

`qq_bots` 中按 AppID 配置 secret，mode 为 webhook 或 websocket，默认 webhook。
intents 是明确的整数掩码；默认开启位 24、25、26、27，不会因权限不足调整订阅。
api_base/auth_url 必须为 HTTPS，gateway_url 必须为 WSS；都只从项目配置读取。
Webhook 强制 Ed25519 验签与 300 秒时间窗口，不受 OneBot 的可选 token 开关影响。
通知时间接受官方 Unix 秒整数，并转换为 UTC；声明为带时区字符串的事件字段仍按 ISO 时间解析。
布尔值、浮点数、越界秒数和无时区字符串均拒绝，不猜秒/毫秒单位。

自动分片共用一个 Bot、HTTP 鉴权、回复序号和事件队列。每片持有自己的会话与恢复游标；
单片断线不替换其他片仍在使用的账号客户端，全部断线才注销并回收该代客户端。
队列、并发和关闭超时使用运行配置；任务退出必须等待子任务清理。
普通网络错误在单片内恢复；任意分片的永久协议错误会快速终止整组，取消并等待其他片并关闭共享资源。
不把无效配置或畸形协议帧当作可重连故障无限重试。

通用 ban 先读取当前禁言成员列表，新增用 add、已禁言用 update、解除用 del；同一客户端串行执行读写，
读失败不执行写操作，也不在写失败后换 op 重试。外部管理员并发修改仍可能使官方拒绝本次写入。
官方接口路径统一逐段编码并拒绝 `.` / `..`；发送、撤回和上传与平台原始接口使用同一规则。

大于 10 MiB 的字节媒体自动使用分片上传；总文件上限 200 MiB，实际媒体配额仍由官方决定。
预签名上传不附带 Bot 鉴权头，不跟随重定向；分片完成接口仅在返回 40093001 时有限重报，
最多三次，并受官方 retry_timeout/retry_delay 约束。普通写动作失败绝不自动重发。

这是协议功能对齐，不代表每个 AppID 都拥有这些官方权限，也不是实网验收。
单元测试使用替身/本机回环，正式使用前仍需在官方测试环境确认权限、模板与媒体格式。

协议依据：[群消息字段](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_messages.post.html)、
[禁言设置](https://bot.q.qq.com/wiki/develop/api-v2/autogen/api/v2_groups_group_openid_restrict_chat_setting.post.html)。
文档未规定的宿主行为以本地参考源码为准；不将测试替身通过视为真机验收。
畸形 2xx 响应（非 JSON、非对象）转为 QQAPIError，保留状态码、不包含响应正文、不自动重试；
明确允许空响应的接口仍返回空对象。

最新回归：全量 567 项、标准库隔离 160 项通过；35 个相关文件类型检查与打包、安装模拟闭环通过。
成员通知时间字段依据[官方成员入群事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html)；
成员入群事件回复行为依据本地参考版本 `11c0991` 的 `Bot.send` 分支。
