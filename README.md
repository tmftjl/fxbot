# FxBot

独立消息应用，当前正在直接替换旧框架运行链路，不提供旧路径别名或桥接层。
运行要求 Python 3.11 及以上；核心本身只使用标准库。

## 目录

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
scripts/            开发工具
docs/               工程记录
data/               本机运行数据（不入库）
temp/               本机验证产物（不入库）
```

## 开发验证

根 `pyproject.toml` 只声明核心依赖和通用资源规则，根 `requirements.txt` 直接安装项目本身。
插件专属依赖由各插件目录的 `requirements.txt` 声明，随插件一起打包；新插件不需要修改根配置。
安装所需插件依赖时，在项目根目录运行，例如：

```powershell
uv pip install -r src/fxbot/plugins/video_parser/requirements.txt
```

不在运行时自动补装依赖。以上只处理依赖归属，不表示旧插件已迁入独立运行链路。

下载资源、缓存、配置和数据库统一写入所属项目的 `data/`，不读取路径环境变量。
源码运行从 `src/fxbot/` 定位仓库根目录；安装包运行以当前工作项目为根目录。
下载资源不写入包目录；随包提供的模板、字体和图标不移动到数据目录。
尚未迁移的洛克等插件及其自带资源已归档到本机 `temp/legacy-plugins/`，不参与打包。

在仓库根目录运行，测试会显式加载 `src/`：

```powershell
.venv/Scripts/python.exe -I -S -X utf8 -m unittest discover -s tests/migration -t .
.venv/Scripts/python.exe -m unittest discover -s tests -t .
uvx --from "mypy>=1.10,<2" mypy
.venv/Scripts/python.exe -m build --no-isolation --outdir temp/packages
```

应用入口为安装后的 `fxbot` 命令或 `python -m fxbot`。
可选令牌和监听设置读取 `data/config/runtime/config.json`，不读取环境变量，不配置 Bot 白名单。
`token` 缺省或为 `""` 时不鉴权；填写后 OneBot V11/V12 必须携带匹配的 Bearer 令牌。
Bot 身份由连接声明，仍校验身份格式、入站事件归属和重复连接。
CLI 的 `--host` 和 `--port` 可显式覆盖监听地址与端口。
事件入口每个连接默认最多并发处理 8 条消息，运行配置 `event_concurrency` 可调整；
`queue_size` 单独控制等待队列。同优先级响应器并发，`block` 只阻止更低优先级，
同一响应器的多个 `.handle()` 仍顺序执行。同一会话不保证消息处理完成顺序。
独立入口已安装会员、权限、AI 聊天、会话命令与撤回处理；聊天与长期记忆维护使用原生调度器。
控制台已挂载聊天、长期记忆与网页独立对话接口，业务设置仍使用 `data/config/` 下原有配置。
共享服务仍在同一进程使用，应用统一负责启动和有序关闭；启动服务不会自动安装插件依赖。
插件放在 src/fxbot/plugins 下即可自动发现，不需要 JSON 声明、扫描配置或手动注册清单。
插件目录的 __init__.py 声明 __plugin_meta__ 中文元信息；on_command / on_regex / on_event /
on_message 创建响应器，通过 @matcher.handle() 注册处理器。核心按目录名统一注册路由、权限和控制台展示。
支持多处理器、Rule 的 AND 组合、Permission 的 OR 组合，以及 matcher.send() / matcher.finish()。
响应器选项只读，支持明确类型的内建参数绑定；ctx.match 保留正则捕获组，ctx.app_services 表示应用服务。
写法与约束见 [插件开发](docs/plugin-development.md)，旧的 @on_command(...) 函数装饰器已移除。
周期业务使用 @scheduled(interval=60, run_on_start=True)，任务执行、取消和等待清理由核心负责。
无需 setup、configure、Plugin 对象、JSON 或根打包配置清单。
`BotClient` 只要求身份、发送与能力注册表；业务通过 `capabilities.require(Capability.X)`
取得明确的可选接口，不要求每个平台实现一组只会报错的占位方法。
`ctx.service(factory)` 按工厂对象身份在当前应用内缓存，应用关闭时统一释放受管服务。
群管理/入群验证、娱乐和视频解析已接入原生路由，不再依赖 NoneBot。
群管理遇到未知成员身份时拒绝操作；音乐扫码仅在私聊进行；点赞固定为每日一次、最多十个。
点赞记录保持 Bot QQ 号 → 用户 QQ 号的结构；运势按用户 ID 保存，同一用户当天跨 Bot 共享。
欢迎语等群配置使用完整会话身份，旧裸群号配置需明确归属后迁移，不自动覆盖。
视频默认发送本地文件 URI，要求 Bot 共享文件系统；远程 Bot 请启用视频配置的 use_base64。
已原生接入 B 站直播订阅插件，支持按房间号或 UID 订阅、取消、查询及开播推送。
订阅与推送按完整会话身份隔离；旧的裸群号/用户号订阅文件须明确平台和 Bot 后单独迁移，
不会被自动猜测归属或覆盖为空记录。
群聊日报也已原生接入，允许群和自动推送群需填写 `onebot11:100:group:300` 这样的完整会话键。
V11 客户端提供历史、精华和禁言成员接口；首次调用扩展动作时以 `get_version_info` 确认实现。
SnowLuma 使用 `reverse_order` + `message_id` 锚点，NapCat 使用 `reverseOrder` + `message_seq`；
识别接受 `NapCat.Onebot`、`SnowLuma` 等明确产品族名称。历史页按时间稳定排序，不把查询方向当成数组顺序；
成功结果只在当前连接缓存十分钟，并发调用共用一次识别。未知实现明确不支持，探测失败或超时直接报错，
不猜测另一套参数、不缓存失败，不影响普通消息收发。

首次使用需创建该文件，例如以下不鉴权的本机配置：

```json
{
  "host": "127.0.0.1",
  "port": 8683
}
```

需要鉴权时添加 `"token": "你的随机令牌"`，并在协议端填写相同令牌。
不鉴权时不要直接向公网暴露 OneBot 入口；默认仅监听 `127.0.0.1`。

V11 反向连接地址为 `/onebot/v11/ws`，只接收 Universal 连接。
V12 使用 `/onebot/v12/{platform}/{bot_id}/ws`，例如 `/onebot/v12/wechat/bot/ws`，
握手要求 `12.*` 子协议，不需要提前声明身份。

### QQ 官方 Bot（Webhook / WebSocket）

独立 QQ 适配器位于 platforms/qq，不依赖 NoneBot。当前支持普通群、C2C、频道消息和频道私信，
支持官方 Webhook 回调和主动 WebSocket 网关；每个 AppID 明确选择一种模式，不与 OneBot 混用。
在同一运行配置里声明 AppID 和凭证：

    {
      "token": "replace-with-a-random-runtime-token",
      "qq_bots": {"123456789": {"secret": "replace-with-your-app-secret"}},
      "host": "127.0.0.1",
      "port": 8683
    }

QQ 开放平台回调地址配置为 https://你的域名/qq/webhook，公网 HTTPS 入口需自行配置。
AppID 不是 QQ 号；普通群和 C2C 的权限身份使用官方 OpenID，例如 qq:123456789:group:GROUP_OPENID。
`qq_bots` 独立配置 AppID 与凭证；禁止关闭验签，不读取环境变量或自动兼容旧配置。
可选 `token` 仅用于 OneBot 接入，不代替 QQ 回调的 Ed25519 签名或官方凭证。
频道 Bot 的真实用户 ID 从 /users/@me 查询，也可在凭证里显式配置 bot_user_id，不会用 AppID 猜。

WebSocket 模式把该 AppID 的凭证改为：

    {
      "secret": "replace-with-your-app-secret",
      "mode": "websocket",
      "intents": 251658240
    }

省略 mode 默认为 webhook。WebSocket 主动通过官方 /gateway/bot 获取 WSS 地址，
不需要配置公网回调地址，也不接受该 AppID 的 Webhook 请求。
intents 默认订阅普通群成员、普通群/C2C 消息、交互和审核事件，即位 24、25、26、27；
具体事件仍需官方授权，不会因权限不足自动换一组订阅。频道不属于本次对齐范围。
网关使用 READY 的真实 Bot 用户 ID，AppID 仍是内部连接身份。
支持心跳 ACK 检查、断线 Resume、无效会话重新 Identify、退避重连、额度等待和退出取消。
永久配置/权限错误会停止网关并记录日志，不反复重连。业务 HTTP 写操作始终不自动重试。
未配置 shard 时按官方返回数量自动启动全部分片，共享一个 Bot 客户端、HTTP 鉴权和事件队列；
也可明确配置本进程负责的 shard，例如 [0, 2]，由其他进程负责其余分片。
可在账号配置中明确设置 api_base、auth_url（HTTPS）与 gateway_url（WSS），不读取环境代理，
TLS 必须验证，不提供验签关闭开关。

普通群/C2C 已接入文本、引用、图片/音频/视频/文件、Markdown、键盘、Ark、Embed，
C2C 还支持流式消息、提示键盘和操作按钮；大于 10 MiB 的字节媒体走分片上传。
支持好友/机器人变化、群成员变化、入群申请、交互和审核通知，及参考实现中的普通群管理接口。
插件通过 QQ_API 获取平台接口，通用禁言、踢人、成员查询、撤回和文件发送也映射到核心能力。
参考实现未提供的普通群历史、点赞、合并转发和全群禁言等不作虚构。
详细覆盖范围与用法见 [QQ 适配器](docs/qq-adapter.md)。平台配额、权限和媒体格式仍受官方限制。
事件去重和队列只保存在本进程内，不提供进程崩溃后的持久化投递保证；新接入应先做测试环境验证。

主人、管理员、权限白黑名单和会员会话使用完整身份，例如 `onebot11:100:private:200`、
`onebot11:100:group:300`。旧的裸用户/群 ID 不能直接混入新权限判定；未知归属不会被自动猜测，
应先明确平台和 Bot，再备份并迁移数据。独立入口不会把旧黑名单不匹配当作公开权限。

运行源码与依赖声明已移除 NoneBot；尚未迁移的业务和旧测试归档在本机 `temp/legacy-*`，
不参与当前导入、测试和打包，后续迁移需按新核心契约实现。
完整业务迁移尚未完成，不能视为生产部署版本。
