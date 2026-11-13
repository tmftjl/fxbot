# 核心协议与消息路由

`fxbot.core` 位于 `src/fxbot/core/`，只依赖标准库，导入时不注册入口、建立连接或启动后台任务。
平台协议放在 `src/fxbot/platforms/`，核心只保留平台无关契约，业务通过核心事件和客户端接口处理消息。

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
不经过 OneBot 的 echo 动作代理。AppID 白名单及 secret 来自项目运行配置。
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
Identify 本地额度不会因为重新发现网关而补回，每次至少间隔 5 秒，额度耗尽等待官方 reset_after。
网络故障使用最高 30 秒退避；HTTP 业务写入不因重连自动重试，401/403 鉴权错误不盲目重连。
每代连接使用新客户端，旧引用离线后不能借用新连接发送；被动回复 msg_seq 缓存跨重连保留，
避免在结果不确定时复用序号。仅排队尚未开始的事件可重新回放；已经开始的事件不会自动重做，
以免部分成功的业务写入重复执行。此语义不是事务或崩溃持久化交付。
单进程只负责一个明确分片；推荐多分片时必须指定 shard，不自动创建冲突的同身份客户端。
关闭取消并等待网关与事件消费者，重连/额度等待可被取消，再关闭 HTTP 资源，不留下心跳任务。

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
V11 反向 WebSocket 地址为 `/onebot/v11/ws`，使用 Bearer 令牌和按平台隔离的白名单校验 Universal 连接。
V12 使用 `/onebot/v12/{platform}/{bot_id}/ws`，要求 `12.*` 子协议，事件和动作都明确携带实际平台身份。
动作通过 echo 关联响应，事件使用有界队列顺序分发；超时不自动重试。
动作请求由 `ActionBroker` 独占管理，连接只组合请求代理、事件队列和传输关闭接口。
应用状态使用 `ApplicationState` 枚举，连接登记必须提供关闭函数；资源栈确保部分启动失败及
正常关闭时均释放所有已登记资源。清理失败在其余资源释放后向上抛出，不逐层吞掉异常。
并发停止等待同一次关闭操作，取消某个等待者不取消实际清理；关闭失败不会伪装成功。
受管任务通过 `Application.cancel(task)` 请求取消；取消请求幂等，调用方仍需等待任务退出。
连接消费者和定时任务使用同一取消入口，避免再次取消正在执行的异步清理。

调度器按名称保留执行槽位直到旧任务真正完成。任务重排、移除后重新添加或连续改配置时，
同名新任务必须等待旧执行及其清理结束；只执行最后保存的定义，不补跑错过的周期。
取消中的旧定义不会出现在公开任务列表中，但执行句柄仍由应用持有并等待回收。
独立入口已接入配置、数据库、原生调度、会员门禁与自助命令，并挂载基础控制台。
原生路由权限通过 `Authorizer` 直接消费核心事件；旧调用链复用同一领域策略，未被接入新分发。
控制台的账户、Bot、会员、配置、权限、日志和缓存端点独立工作；未知 HTTP 错误统一返回通用响应。
日志缓冲和写线程显式启停，同时采集核心标准日志和业务 Loguru 日志。
AI 原生路由已接入应用；其余平台和旧插件尚未全部迁移，当前不是完整独立应用验收状态。

## 聊天消息契约

`Event.occurred_at` 是协议边界校验后的带时区时间；聊天存储转换为 naive UTC。
语音段使用 `voice`，媒体引用通过 `BotClient.resolve_media()` 解析；业务不读取 OneBot 文件字段。
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
会话展示名通过 `BotClient.target_name()` 查询，必须同时匹配平台与 Bot，缓存按应用隔离。
V12 用户及成员名称使用[标准字段](https://12.onebot.dev/interface/user/actions/)，不复用 V11 的 nickname 别名。

V11 转发字段按[官方消息段规范](https://github.com/botuniverse/onebot-11/blob/master/message/segment.md)读取，
不接受多种响应结构猜测，也不把消息查询失败转换成另一种 ID 查询。

## 插件注册与后台通知

插件不区分内置和外部。core.plugins 负责统一发现、名称校验和安装，
core.commands 提供 Command 和 register_commands()，权限默认值由
Authorizer.declare_commands() 写入，核心不依赖具体业务插件或权限存储实现。
已删除 plugins/catalog.py 和 plugins/_native.py，不保留旧入口或名称别名。

核心固定扫描 src/fxbot/plugins 的直接子包，插件名就是目录名。
无需 plugin.json、plugin_packages 或注册清单，这两项声明和配置机制已删除，不保留兼容解析。
插件直接子包会被导入，必须提供 __plugin_meta__ = PluginMetadata(name="中文名", ...)。
下划线开头或没有 __init__.py 的目录不作为插件；元信息错误、导入错误直接报告。

命令声明使用 on_command、on_regex，事件声明使用 on_event、on_message。
核心收集插件及其已导入子模块里定义的处理器，去重并生成目录名:命令名权限节点。
name 参数用于稳定权限 ID，display_name 用于中文展示；on_command 的 arguments 是命令后的参数，
on_regex 的 arguments 是完整匹配文本。权限由核心声明，插件不手工安装路由。
控制台 /plugins、/plugins/details、/commands 从当前应用返回插件中文元信息和命令名称。

定时业务使用 @scheduled(interval=60, run_on_start=True)，由核心按插件名隔离任务，
同名任务不重叠、失败由受管任务边界记录，关闭时取消并等待该任务及其清理退出。
任务 context.first_run 只在成功完成一次后变为 False，状态按应用隔离，失败后仍可重新校准。
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
标准 V11 不提供精华及禁言到期扩展，必须明确选择 NapCat；V12 未定义的操作直接拒绝。

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
协议字段依据本地 `temp/adapter-onebot-reference` 的 V11 源码核对，参考仓库不作为依赖导入。
订阅操作从 `HandlerContext.event.target` 捕获收件人；持久化数据必须明确平台和 Bot 身份。

`NotificationService` 在后台任务中按完整目标查找连接，复用领域权限与会员策略，
但不构造虚假的用户事件，也不继承订阅操作者的管理员豁免。
通知在异步准入检查后再次验证订阅与连接；离线或已撤销订阅不能改用其他 Bot 发送。
原生权限检查要求插件和命令节点实际存在；缺失节点不表示开放访问。

群日报使用 `BotClient.fetch_group_history()` 与核心 `HistoryMessage`，只对明确声明 `HISTORY`
能力的客户端开放。标准 V11/V12 不包含该能力；`onebot11_implementations` 按账号明确选择 NapCat。
NapCat 请求使用 `reverse_order` 和返回的短消息 ID 作为 `message_seq` 参数的锚点，
依据[实现源码](https://github.com/NapNeko/NapCatQQ/blob/main/packages/napcat-onebot/action/go-cqhttp/GetGroupMsgHistory.ts)，
不通过动作异常或版本探测选择另一种接口。查询失败、身份不符或分页预算耗尽不能伪装为空历史。
日报记录、运行锁和图片目录使用完整群会话身份，自动任务在生成与发送两端检查准入。

验证：`python -I -S -X utf8 -m unittest discover -s tests/migration -t .`；
`python -m unittest tests.runtime.test_chat_native`。
