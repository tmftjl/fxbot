# 核心功能

`fxbot.core` 位于 `src/fxbot/core/`，只依赖标准库，导入时不注册入口、建立连接或启动后台任务。
平台协议放在 `src/fxbot/platforms/`，核心只保留平台无关契约，业务通过核心事件和客户端接口处理消息。

## 目录结构

```text
src/fxbot/
  __main__.py       模块启动入口
  bootstrap.py      应用装配
  core/             标准库核心：事件、路由、插件与生命周期
  platforms/        独立平台协议编解码与客户端
  runtime/          网络服务、协议连接与命令行
  chat/             AI 与记忆
  config/           业务配置
  console/          控制台后端及 web 前端
  db/               数据模型
  membership/       会员
  permission/       权限
  plugins/          自动发现的业务插件与声明
  resources/        公共资源
  utils/            通用工具
tests/              测试
scripts/            迁移与开发工具
docs/               核心、插件与启动文档
data/               运行数据（不入库）
```

## 消息入口

OneBot V11 使用 `parse_event(payload, bot_id=...)` 解析平台消息段数组或显式 CQ 字符串。
`bot_id` 来自已确定身份的连接，必须与事件中的 `self_id` 一致。
解析器复制消息数据，保留原始消息、提及、引用 ID、时间和发送者角色；不查询引用内容。
群消息处理文本可以去掉首尾直接相邻的自身提及，原始消息不变；不推断昵称唤醒或被引用者身份。

V11 已识别入退群、撤回、群角色变更等通知，并规范化为独立的核心事件类型，不作为普通消息处理。
CQ 输入只解码一层转义，不把解码后的普通文本重新解释为可执行消息段；请求和匿名身份仍明确拒绝。
未知入站段保留为 `onebot11:<type>`，但不能直接发送；出站消息仅编码明确支持的段。
文本始终使用消息数组发送，不能把用户文本中的 CQ 标记当成可执行段。

## 路由规则

QQ 官方消息使用独立的 platforms/qq 实现，通过 POST /qq/webhook 或主动 WebSocket 收取事件，
不经过 OneBot 的 echo 动作代理。AppID 及 secret 独立来自项目运行配置 `qq_bots`。
Webhook 按原始请求字节执行 Ed25519 验签，时间窗口为 300 秒，单次请求上限 1 MiB；
握手只签随机令牌，拒绝含 JSON 结构的内容，不能用于签署伪造事件。
每个账号拥有独立有界事件队列与 HTTP 客户端。只在消息验证并入队后登记去重，
队列满返回 503 且不登记该事件；去重窗口 600 秒、最多 4096 项，按账号和真实目标隔离。
返回回调 ACK 表示已入内存队列，不表示业务执行成功，更不等于持久化交付。

QQ 群使用 group_openid/member_openid，C2C 使用 user_openid；频道用 channel 会话，
频道私信使用 private:dms/频道私信guild_id/用户ID 的独立目标，不能与 C2C 混用。
原始消息中的真实提及 ID 保留，处理消息中的自身提及规范化为当前 Bot 身份。
频道首次处理时查询真实 Bot 用户 ID，查询失败不猜测身份，不分发当前事件。
Webhook 模式下控制台的 qq 账号表示已有验签消息；WebSocket 模式在 READY、
或 Resume 的首个回放 Dispatch/RESUMED 后登记，断线立即注销，不代表探测过全部接口权限。

QQ API 使用独立缓存的 access token 和刷新锁；请求头为 QQBot token 与 X-Union-Appid。
被动回复只使用当前事件的 msg_id，按目标和 msg_id 单调递增 msg_seq；失败也不复用序号。
支持事件被动回复的通知只传 event_id，不自动添加 msg_seq；未支持事件回复的通知按主动消息发送，
需要官方主动发送权限，不能借用任意通知 ID 绕过权限。
成员入群通知依据参考实现支持 event_id 回复，接收开关恢复事件依据官方文档保留支持。
通知 Unix 秒整数显式转为 UTC，带时区 ISO 字段按声明解析，不将布尔值或浮点数猜成时间戳。
HTTP 写操作不自动重试；401 只使后续调用重新鉴权，不重试本次写入。
媒体段先整体校验再上传；最多一项媒体，不把部分上传或缺少返回 ID 当作成功。
平台不支持的能力抛 UnsupportedCapability，不用 OpenID 构造 QQ 号头像或套用 OneBot 动作。
关闭先取消事件处理并等待清理，再关闭独占 HTTP 客户端并注销 Bot。
Webhook 与 WebSocket 使用同一 QQEventStream、核心事件和 QQClient，传输边界不互相继承或猜测。

WebSocket 配置 mode=websocket 后，客户端从 /gateway/bot 获取 WSS 地址与 SessionStartLimit。
Hello 提供毫秒心跳周期，Identify 使用 QQBot token、明确 intents 和 shard；
Ready 保存 session_id、用户 ID 与序号。未建立会话前不分发消息。
断线后优先 Resume。官方可能先回放遗漏消息，最后才发送 RESUMED，状态机支持此顺序。
心跳发送最新接收序号，Resume 使用已处理前缀的序号；RESUMED 不得覆盖未完成消息的游标。
收到重连指令或心跳 ACK 超时后断线重连；9/4006/4007 或官方内部错误要求重新 Identify。
无效 intents、无权限、无效分片及下架/封禁等永久错误直接停止，不循环试参数。
自动分片采用整组快速失败：任一片的永久 GatewayProtocolError 会取消并等待其他片，再关闭账号资源。
这也包含违反确定协议契约的帧；普通网络断线、官方重连请求和可恢复会话错误仍在单片内恢复。
Identify 本地额度不会因为重新发现网关而补回，每次至少间隔 5 秒，额度耗尽等待官方 reset_after。
网络故障使用最高 30 秒退避；HTTP 业务写入不因重连自动重试，401/403 鉴权错误不盲目重连。
每代连接使用新客户端，旧引用离线后不能借用新连接发送；被动回复 msg_seq 缓存跨重连保留，
避免在结果不确定时复用序号。仅排队尚未开始的事件可重新回放；已经开始的事件不会自动重做，
以免部分成功的业务写入重复执行。此语义不是事务或崩溃持久化交付。
明确配置 shard 时只连接该分片；不配置时按官方网关返回数量自动连接全部分片。
多分片共用一个账号客户端、HTTP 鉴权锁和有界事件队列，每片独立维护 session、接收和已提交游标。
某片断线且仍有其他在线片时不注销账号；全部片断线则回收共享事件流并令旧客户端失效。
Identify 消耗共用的账号额度并串行限速，防止各片分别把总额度当作自己的额度。
关闭取消并等待网关与事件消费者，重连/额度等待可被取消，再关闭 HTTP 资源，不留下心跳任务。

普通群/C2C 的平台接口由 QQActions 与 QQ_API 能力提供，返回明确的原始 JSON，不引入第三方适配器模型。
核心 MEMBER_MODERATION 仅包含成员禁言与踢人；完整 MODERATION 另含管理员、全群禁言和头衔，
QQ 不声明不具备的完整能力。现有群管理/入群验证已改用精确的成员管理能力。
好友、群成员、机器人变化、申请、交互和审核事件保留 qq_event_type 与独立副本 qq_data；
通知按事件 ID（或网关序列号）去重，不能用恒为空的 message_id 合并不同通知。
分片上传使用独立预签名 PUT，不携带 QQBot 令牌，所有子任务在取消/失败时统一等待清理。
只有明确的 40093001 分片转存错误允许有限重报完成，业务消息、审批和群管理写入不自动重试。

- 预处理器按注册顺序执行，必须返回严格的 `True` 才放行。
- 命令大小写敏感，前缀由注册者显式写入命令词；命令与参数以空白分隔。
- 路由按优先级从小到大分组，同优先级并发执行；整组完成后才进入下一优先级。
  执行完成顺序不确定，诊断结果仍按注册顺序收集。
- 匹配后依次检查权限、响应规则，再执行处理器；没有权限函数表示公开，权限返回非 `True` 跳过该响应器。
  单个命令权限拒绝不等于全局门禁拒绝，后续公开路由仍可处理；全局门禁必须放在预处理器。
- `block=True` 阻止更低优先级路由，不取消或阻止同优先级响应器。
- `finish()` 可选发送消息并结束当前响应器的整个处理链，本身不改变阻断设置。
- 规则、权限、处理器或发送发生异常时记录服务器日志，不向用户发送内部异常；
  等待同组其他响应器结束后停止向更低优先级传播，不回滚已经发生的同级操作。
- 取消异常向上抛出；正常、拒绝、失败和取消路径都会恢复事件上下文。
- 注册表在分发开始时取快照，处理中新增的路由或准入检查只影响下一次分发。

## 使用方式

```python
from fxbot.core import Router, HandlerContext
from fxbot.platforms.onebot11 import OneBot11Client, parse_event

router = Router()


async def ping(context: HandlerContext) -> None:
    await context.finish("pong")


router.command("ping", "/ping", ping)
```

协议连接提供异步 `sender(action, params)` 后构造 `OneBot11Client(bot_id, sender)`，
将收到的协议数据交给 `parse_event`，再调用 `router.dispatch(client, event)`。
客户端只做动作和消息编码，连接鉴权、请求与响应关联、超时和断线清理由传输层负责。
网络传输位于 `src/fxbot/runtime/`，独立应用装配入口为 `src/fxbot/bootstrap.py`。
V11 反向 WebSocket 地址为 `/onebot/v11/ws`，只接收 Universal 连接，不使用 Bot 白名单。
OneBot V11/V12 的 `token` 缺省或为空串时不鉴权，填写后严格校验 Bearer 令牌；
不鉴权也必须校验连接身份、事件归属和重复连接。QQ 官方验签与凭证不受此开关影响。
V12 使用 `/onebot/v12/{platform}/{bot_id}/ws`，要求 `12.*` 子协议，事件和动作都明确携带实际平台身份。
动作通过 echo 关联响应，事件使用有界队列和固定数量消费者并发分发；超时不自动重试。
运行配置 event_concurrency 默认 8，限制每个连接同时处理的事件数；queue_size 默认 128，
单独限制等待队列，不包含在途事件。值必须为正整数，设为 1 只串行处理事件，不改变同级响应器并发。
OneBot 连接、QQ Webhook 和 QQ 网关使用同一配置，不限制独立的动作响应和心跳通路。
QQ Resume 只提交已完成的连续前缀，后续事件先结束不能越过前面的在途事件。
QQ 待提交序列上限为 queue_size + event_concurrency，防止慢前缀导致已完成记录无限累积。
动作请求由 `ActionBroker` 独占管理，连接只组合请求代理、事件队列和传输关闭接口。
应用状态使用 `ApplicationState` 枚举，连接登记必须提供关闭函数；资源栈确保部分启动失败及
正常关闭时均释放所有已登记资源。清理失败在其余资源释放后向上抛出，不逐层吞掉异常。
并发停止等待同一次关闭操作，取消某个等待者不取消实际清理；关闭失败不会伪装成功。
受管任务通过 `Application.cancel(task)` 请求取消；取消请求幂等，调用方仍需等待任务退出。
连接消费者和定时任务使用同一取消入口，避免再次取消正在执行的异步清理。
消费者监督任务拥有固定子任务；响应器分发拥有本组子任务。父调用结束或取消前会等待子任务清理，
不将每条消息变成脱离所有者的后台任务。连接关闭后丢弃未开始队列，已开始的写操作不自动重放。
OneBot 关闭开始即通过 Application.retire 释放在线身份，允许同 ID 重连；
旧连接清理仍由应用持有直至 unregister 完成，旧连接的最终注销不会移除新连接。

调度器按名称保留执行槽位直到旧任务真正完成。任务重排、移除后重新添加或连续改配置时，
同名新任务必须等待旧执行及其清理结束；只执行最后保存的定义，不补跑错过的周期。
取消中的旧定义不会出现在公开任务列表中，但执行句柄仍由应用持有并等待回收。
独立入口已接入配置、数据库、原生调度、会员门禁与自助命令，并挂载基础控制台。
核心进程命令 `#重启`、`#关机`、`#更新` 由运行时直接注册，只接受 `system.superusers` 中的超级用户（完整私聊身份或裸平台用户 ID）；
重启和更新通过 SIGINT 优雅关闭，退出前用当前解释器重新执行 `-m fxbot`，关机只关闭不重拉。
`#更新` 只做 `git pull --ff-only`。拉取确认成功后立即记录重启意图，再读取提交日志和尝试回复；
后续日志查询取消、回复失败或超时都不撤销重启。拉取失败不重启；git 缺失会反馈更新失败。
HEAD/log 查询限时 15 秒，拉取阶段整体限时 180 秒；超时或取消会终止并等待拥有的进程退出。
重启完成后由启动任务在目标 Bot 重新上线时补发一次通知。
原生路由权限通过 `Authorizer` 直接消费核心事件，旧运行链路不参与分发。
控制台的账户、Bot、会员、配置、权限、日志和缓存端点独立工作；未知 HTTP 错误统一返回通用响应。
日志缓冲和写线程显式启停，同时采集核心标准日志和业务 Loguru 日志。
AI 原生路由已接入应用；其余平台和旧插件尚未全部迁移，当前不是完整独立应用验收状态。

## 聊天消息契约

`Event.occurred_at` 是协议边界校验后的带时区时间；聊天存储转换为 naive UTC。
语音段使用 `voice`，媒体引用通过 `bot.capabilities.require(Capability.MEDIA).resolve_media()` 解析；业务不读取 OneBot 文件字段。
引用正文使用 `QuotedMessage`，平台可选能力由 `Capability` 明确声明，不通过失败重试探测。
V11 文件上传必须显式注入 `file_uploader` 策略；标准客户端不假定远端与 FxBot 共享文件系统。

`ReplySender.send(Message | str)` 只负责交付，不提供 Matcher 生命周期或空的 finish()。
工具运行时持有明确的事件、客户端、发送器、权限服务和应用；Focus 与后台续话捕获真实目标，
不能根据派生会话字符串重新猜收件人。Focus 工具和回复保留原始权限服务，离线旧连接不能继续使用。

`chat.router.install()`、`chat.commands.install()` 和 `chat.recall.install()` 只显式注册原生路由。
bootstrap 通过 `ChatLifecycle` 安装这些入口，在数据库初始化后准备聊天存储、记忆内核与回写任务，
并注册原生维护任务。保留进程内共享服务，不为去除全局变量而额外构建实例容器。
退出时先停输入和生成任务，再关闭记忆、模型和 HTTP；资源栈同样用于部分启动失败的回收。
模型管理器只在明确调用 `start()` 后开放下一次应用生命周期；关闭失败的实例不能复用。

控制台已挂载聊天、长期记忆和独立网页聊天接口。健康状态使用当前应用的 Bot 表与原生调度器；
会话展示名通过 `bot.capabilities.require(Capability.TARGET_LOOKUP).target_name()` 查询，必须同时匹配平台与 Bot，缓存按应用隔离。
V12 用户及成员名称使用[标准字段](https://12.onebot.dev/interface/user/actions/)，不复用 V11 的 nickname 别名。

V11 转发字段按[官方消息段规范](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md)读取，
不接受多种响应结构猜测，也不把消息查询失败转换成另一种 ID 查询。

## OneBot 适配器

OneBot V11/V12 位于 `src/fxbot/platforms/onebot11` 与 `onebot12`，不依赖外部框架或适配器包。
连接地址、令牌和监听配置见[启动配置说明](startup.md)，本节描述协议行为。

### 连接与身份

V11 反向 WebSocket 为 `/onebot/v11/ws`，只接受 Universal 连接；Bot 身份来自 `X-Self-ID`，
必须为有效 ID，事件中的 `self_id` 必须与连接一致。V12 使用
`/onebot/v12/{platform}/{bot_id}/ws` 和 `12.*` 子协议，平台与 Bot 身份同时进入事件和动作帧。
`token` 非空时要求 `Authorization: Bearer <token>`；空串表示不鉴权，但仍校验身份格式、事件归属
和重复连接。同一个 Bot 不允许两个在线连接，关闭开始时先释放在线身份再完成清理。

### 事件与消息

V11 接受消息段数组或显式 CQ 字符串，CQ 只解码一层转义，不把普通文本重新解释为可执行段。
解析后保留原始消息、提及、引用 ID、时间和发送者角色，不主动查询引用内容。入退群、撤回、
群角色变更和群文件上传等通知规范化为核心事件，不进入普通消息路由。未识别的入站段保留为
`onebot11:<type>`，不能直接发送；出站只编码明确支持的段，文本始终以消息数组发送。
V12 使用标准消息段和标准用户/成员名称字段，不复用 V11 的昵称别名。

### 动作与能力

客户端只做动作编码与消息转换，连接鉴权、echo 关联、超时和断线清理由传输层负责。
基础能力包括发送、引用查询、成员/群目录、成员与 Bot 角色、头像、禁言/踢人/管理员/全群禁言/
头衔、撤回、点赞、合并转发和文件发送；文件上传必须显式注入上传策略，不假定远端与 FxBot
共享文件系统。动作超时和失败直接返回，不自动重试写操作。

V11 还提供历史、精华和禁言成员扩展。首次调用扩展动作时通过 `get_version_info` 的 `app_name`
识别实现：NapCat 使用 `reverseOrder=True` 与 `message_seq`，SnowLuma 使用
`reverse_order=True` 与 `message_id`。识别结果只在当前客户端缓存 600 秒，使用单调时钟与实例锁；
未知实现明确拒绝扩展调用，不发送实际动作，也不在失败后猜测另一套参数。
历史响应在解析边界按时间稳定排序，用最早消息作为锚点；分页不前进、身份不符或结构异常
统一作为 `ActionFailed` 交给业务反馈，调用参数错误仍为 `ValueError`。
V12 不提供历史、精华等扩展动作，未定义的平台操作直接拒绝。

### 传输与并发

动作通过 echo 关联响应，每个连接有有界事件队列和固定数量消费者；`event_concurrency`
限制同时处理的事件，`queue_size` 限制等待队列，`max_pending` 限制未完成动作请求。
同一优先级响应器仍并发执行，设为 1 只串行处理入站事件。超时和断线会结束等待中的动作，
不会重放写操作；关闭先取消并等待消费者与动作清理，再关闭传输并注销连接。

## QQ 官方适配器

QQ 官方平台使用独立实现，不复制宿主、驱动器或环境变量配置。
连接参数见[启动配置说明](startup.md)，本节只描述协议行为和已接入能力。

### 覆盖范围

| 领域 | 已接入 |
| --- | --- |
| 连接 | Webhook 验签/握手、WebSocket Identify、心跳 ACK、Resume、退避、会话额度、自动及指定分片 |
| 消息 | 普通群、群 AT、C2C、文本、提及、表情、引用正文及引用索引 |
| 高级消息 | Markdown、模板键盘/自定义键盘、Ark、Embed、预上传媒体；C2C 流式消息、提示键盘、操作按钮 |
| 文件 | URL、内联字节、预上传、无鉴权预签名 PUT、分片完成通知、合并、群/C2C 文件发送 |
| 通知 | 好友增删、收发开关、机器人入退群、群消息开关、成员入退群、入群申请、交互、审核结果 |
| 群管理 | 成员列表/详情、群资料、Bot 状态、禁言设置、申请列表/审批、移除成员、黑名单查询/修改 |
| 撤回 | 按明确目标撤回群/C2C 消息；通用撤回能力可撤回本客户端发送记录中的消息 |

### 平台 API

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
返回值为 JSON 字典，不是第三方适配器的 Pydantic 模型；字段与路径保持参考协议语义。

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

### 消息声明

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
高级格式与图片/音频/视频/文件或预上传 media 混合时，在任何上传前拒绝；单独的预上传 media 仍可使用。
upload_id 合并请求保留 srv_send_msg 开关，上传不自动变成发送。通知缺失 d 对象按输入错误返回 400。
成员与禁言查询在解析边界验证必需字段，结构异常为 ActionFailed，不把部分成员页伪装成完整结果。
被动消息自动使用当前消息的 msg_id/msg_seq；支持事件被动回复的通知只使用 event_id，不自动添加 msg_seq。
按官方列表接受交互、机器人入群、群/C2C 恢复消息接收、好友新增事件；成员入群事件按本地参考
`bot.py` 的 `GroupMemberAddEvent` 分支同样支持事件回复。其他通知按主动发送处理，
仍需账号具备主动消息权限，不会借用不支持的 event_id。
通知处理器可从 `ctx.event.metadata["qq_data"]` 获取原始业务字段。

### 生命周期与错误边界

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

最新回归：全量测试与标准库隔离测试通过；相关文件类型检查与打包、安装模拟闭环通过。
成员通知时间字段依据[官方成员入群事件](https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/group_member_add.html)；
成员入群事件回复行为依据本地参考版本 `11c0991` 的 `Bot.send` 分支。

## 插件注册与后台通知

插件不区分内置和外部。core.plugins 负责统一发现、名称校验和安装，
core.commands 提供响应器创建函数和 Command 权限声明，权限默认值由
Authorizer.declare_commands() 写入，核心不依赖具体业务插件或权限存储实现。
已删除 plugins/catalog.py 和 plugins/_native.py，不保留旧入口或名称别名。

核心固定扫描 src/fxbot/plugins 的直接子包，插件名就是目录名。
无需 plugin.json、plugin_packages 或注册清单，这两项声明和配置机制已删除，不保留兼容解析。
插件直接子包会被导入，必须提供 __plugin_meta__ = PluginMetadata(name="中文名", ...)。
下划线开头或没有 __init__.py 的目录不作为插件；元信息或声明错误直接报告。
启动时还会读取插件目录的 requirements.txt，自动补装缺失项，不升级已安装版本；
安装失败只记录安装器输出（版本冲突单独标记）并继续启动，导入时缺依赖的插件记录后跳过，
其他插件和核心仍可运行。插件依赖的自动安装详见[启动配置说明](startup.md)。

命令声明使用 on_command、on_regex，事件声明使用 on_event、on_message，均返回 Matcher。
通过 @matcher.handle() 顺序添加处理器；支持无参数、单个上下文或明确类型的内建参数绑定，
不按名字猜测业务依赖。响应器参数在注册时编译，配置与匹配函数只读。
核心收集插件及其已导入子模块中的响应器，去重并生成目录名:响应器名权限节点。
name 参数用于稳定权限 ID，display_name 用于中文展示；on_command 的 arguments 是命令后的参数，
on_regex 的 arguments 是完整匹配文本。权限由核心声明，插件不手工安装路由。
控制台 /plugins、/plugins/details、/commands 从当前应用返回插件中文元信息和命令名称。

Rule 使用 & 组合且全部通过，Permission 使用 | 组合且任一通过；均要求严格 True。
自定义权限只能进一步限制，必须与统一权限节点同时通过，不能通过 OR 绕过管理权限。
两种组合按声明顺序短路执行，不并发运行有状态检查。
每个响应器调用只建立一次 HandlerContext；规则、权限和多处理器共享它，state 不跨事件共享。
matcher.send/finish 从当前执行作用域取得上下文，并发和嵌套调用隔离；结束、异常、取消均恢复上下文。
继承了上下文的后台任务在响应器结束后不能继续使用 matcher.send，后台通知应使用捕获的目标。

LoadedPlugin 负责读取和验证定义，PluginRuntime 负责应用内绑定与资源所有权。
先读取全部插件定义并检查全部路由冲突再安装，模块不保存当前应用或连接；
响应器绑定为不可变快照，加载后禁止增添处理器。核心聊天、会员和控制台命令也通过 bind_matchers 装配。
原 CommandDeclaration、函数元数据收集和 @on_command(...) 直接装饰处理器的 API 已删除，无兼容分支。

定时业务使用 @scheduled(interval=60, run_on_start=True)，由核心按插件名隔离任务，
同名任务不重叠、失败由受管任务边界记录，关闭时取消并等待该任务及其清理退出。
任务 context.first_run 只在成功完成一次后变为 False，内部执行状态独立于业务 state 字典，
按应用隔离，失败后仍可重新校准。ScheduledTask 持有排期与执行上下文，不使用循环闭包捕获任务。
直播入口只有普通 store/poller 和定时业务声明，不使用 resource 或后台 Task 管理代码；
首次执行只校准基线，之后才推送。订阅状态路径和文件格式不变。
其余四个插件同样不使用 resource；旧 resource 声明 API 已删除，不保留兼容分支。
验证码每 3 秒检查、扫码每 2 秒检查、一言每 600 秒刷新，周期与业务判断均由各插件自己声明。
日报使用 @scheduled(plan=daily_plan, watch_config=True)；plan 只返回通用排期或 None，
核心负责订阅配置变化和重新排期，不识别日报配置字段。
视频处理保留本次请求的文件 finally 清理，不注册伪周期任务。

普通无状态业务对象可由模块持有；有应用身份或待验证会话的对象通过 context.service(factory)
按插件和应用缓存，避免在模块全局覆盖运行中应用的连接。工厂接收当前应用服务，只在首次使用时调用。
确实有待释放状态的业务对象实现 Service.close；核心先停任务、等待清理，再逆序关闭对象并清空缓存。
插件没有 start/stop 或框架任务生命周期代码，Service.close 只清理自己的待验证/扫码等业务状态。
图片外显使用 @outgoing 同步转换声明，核心负责安装和撤销，不修改平台全局消息类。
不支持旧 Plugin/configure/setup 或 JSON 清单，不提供兼容注册分支。

群管理通过同一个领域服务处理命令和 AI 工具，动作前查询真实成员角色并再次检查授权和连接。
身份未知、权限撤销、连接被替换均不执行写操作；自动处罚只处理已确认的普通成员。
验证码按完整群会话和用户保存待验证状态，关闭、离群、通过或超时后移除；退出清空待验证状态。
V11 扩展调用前自动识别实现，不配置实现白名单；未知实现明确拒绝，禁言列表/全部解禁命令会反馈失败。
V12 未定义的操作直接拒绝。

娱乐功能保留运势卡、资料卡、语录、Doro、点赞、欢迎语、音乐搜索/扫码与 AI 工具。
欢迎语使用结构化消息段及服务生成的图片文件名，不解释文本中的文件路径占位符。
音乐扫码只允许私聊；选择结果隔离群会话及用户，账号共享模式也不跨 Bot。
后台扫码任务由应用拥有，退出取消并等待清理，不改用同账号的新连接发送。
点赞每人每日一次、单次最多十个，不循环调用直到接口报错。
点赞持久化按 Bot QQ 号、用户 QQ 号分层；运势按用户 ID 保存，同一用户当天跨 Bot 共享。
这两项沿用各自的业务数据结构，不强制套用完整会话键，也不要求迁移现有记录。
图片外显使用应用级出站转换，不修改消息类或入站消息；一言缓存每十分钟更新，
不在启动时访问外部服务，首次刷新前使用 Ciallo~。

视频解析仅匹配受支持域名；媒体下载验证公网目标及每次重定向，并限制重定向次数和大小。
不换 HTTP 库、不修改 URL 重试，图片批次有失败则整批失败。取消时回收 FFmpeg。
每次请求只清理自己登记的下载目录，不删除旧缓存或其他请求目录。
`use_base64=true` 直接发送文件内容；false 明确要求 Bot 与 FxBot 共享同一路径，不自动探测。
协议字段依据 OneBot V11 参考实现核对，参考源码不作为依赖导入。
订阅操作从 `HandlerContext.event.target` 捕获收件人；持久化数据必须明确平台和 Bot 身份。

`NotificationService` 在后台任务中按完整目标查找连接，复用领域权限与会员策略，
但不构造虚假的用户事件，也不继承订阅操作者的管理员豁免。
通知在异步准入检查后再次验证订阅与连接；离线或已撤销订阅不能改用其他 Bot 发送。
原生权限检查要求插件和命令节点实际存在；缺失节点不表示开放访问。

群日报使用 `bot.capabilities.require(Capability.HISTORY).fetch_group_history()` 与核心 `HistoryMessage`。
OneBot V11 客户端提供历史、精华和禁言成员接口，接口注册不等于远端能力确认。
首次调用扩展动作时通过 `get_version_info` 的 `app_name` 确认已支持的实现：
接受明确产品族名称（例如 NapCat.Onebot、SnowLuma）；SnowLuma 使用 `reverse_order=True` 与 `message_id`，
NapCat 使用 `reverseOrder=True` 与 `message_seq`，不通过动作失败切换参数。
未知实现明确拒绝扩展调用，不发送实际扩展动作；识别异常直接传播，不缓存失败、不猜测默认方言。
成功识别在当前客户端缓存十分钟，使用单调时钟与实例锁；同账号重连和不同应用实例均重新识别。
历史页面在解析边界按时间稳定排序，用最早消息作为锚点；同秒消息保留上游相对顺序，不按短 ID 猜先后。
响应结构、身份不符、分页不前进或超预算均作为 ActionFailed，由业务反馈查询失败；调用参数错误仍为 ValueError。
多消息页面锚点不前进时明确失败，不能把部分历史当作完整结果。
查询失败、身份不符或分页预算耗尽不能伪装为空历史。
日报记录、运行锁和图片目录使用完整群会话身份，自动任务在生成与发送两端检查准入。

验证：`python -I -S -X utf8 -m unittest discover -s tests/migration -t .`；
`python -m unittest tests.runtime.test_chat_native`。

## 内置插件行为

内置插件包括群管理/入群验证、娱乐、视频解析、B 站直播、群聊日报、表情包制作、图库、帮助、
无限二代、洛克王国、服务器运维、实用工具、鸣潮表情和 gscore 连接器；插件资源随包提供。
群管理、娱乐、视频解析、B 站直播和群聊日报的行为已在上文说明，其余插件的关键约束：

- 权限与会员使用完整身份，例如 `onebot11:100:private:200`、`onebot11:100:group:300`；
  旧的裸用户/群 ID 不进入新权限判定，未知归属不自动猜测，应先明确平台和 Bot 再备份迁移。
- 表情包关键词匹配不等待网络；冷缓存只登记需求，由周期任务初始化，完成前提示稍后重试。
- 图库只响应已配置的 API 名称或已安装的本地目录；同名时 API 优先，其余消息继续后续路由。
- 洛克王国资源索引只允许直接子项，解码后校验文件名和落盘边界，不跟随重定向；
  远行商人订阅键与隔离语义保持不变。
- 服务器运维的免确认配置按完整命令精确匹配，管道和重定向等 shell 组合仍需确认；
  确认时先展示命令和目录，由同一用户的后续消息明确确认，否定或提问不算；
  执行任务属于触发它的应用，应用关闭会取消并等待命令清理。
- 帮助、无限二代、实用工具和鸣潮表情分别按帮助图配置、聊天 hook、评分服务和角色缓存工作，
  不访问真实平台或外部服务也能在测试中加载。
- gscore 连接器在控制台配置 gsuid_core 地址、Token 与连接 Bot ID，转发 OneBot11/12 和 QQ 官方事件；
  平台适配器按注册表扩展，新增平台只需实现 `adapters/base.py` 的接口并注册。

## 服务、能力与错误边界

`ServiceScope` 按工厂对象身份缓存实例，并保留工厂引用；同名 lambda、闭包、partial
和不可哈希的可调用对象不会串用缓存。作用域属于当前应用，关闭后拒绝创建服务；
按创建逆序关闭 `Service`，同一实例只关闭一次，单项失败不阻止其他资源清理。

`BotClient` 只要求身份、能力注册表、出站转换与 `send()`。
可选操作使用独立 Protocol；平台仅注册真实实现，不提供未支持操作的空壳方法。
`Capabilities.provide()` 返回新的不可变注册表，`require()` 的返回类型由能力键确定，
允许能力由客户端以外的对象提供。缺失能力在这一边界明确抛出 `UnsupportedCapability`。

配置读取只对 I/O 错误、格式错误和声明的校验失败执行既定历史回退。
校验器以 `ValueError` 表示输入不合法；`RuntimeError`、`TypeError` 等内部错误直接暴露，
不能伪装成损坏配置并静默使用默认值。

核心上下文通过 `Configuration`、`Authorization`、`Notifications` 和 `ApplicationServices`
窄协议引用服务，不把权限实现或业务类型导入标准库核心。
当前事件使用 EventContext / current_event_context；HandlerContext.app_services 和 TaskContext.app_services
显式指向应用服务。配置订阅直接使用 configuration，不再绕经权限服务。
通用 BotOffline / ActionFailed 位于 core.errors，平台层不依赖 runtime.actions 的异常定义。
`uvx --from "mypy>=1.10,<2" mypy` 检查核心接口；这是限定范围的类型检查，
不表示整个业务仓库已严格类型化，协议 JSON 数据边界仍保留 `Any`。
