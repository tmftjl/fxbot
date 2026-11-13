# FxBot

独立消息应用，当前正在直接替换旧框架运行链路，不提供旧路径别名或桥接层。

## 目录

```text
src/fxbot/
  __main__.py       模块启动入口
  bootstrap.py      应用装配
  core/             标准库核心：事件、路由、插件与生命周期
  platforms/        独立平台协议编解码与客户端
  runtime/          网络服务、协议连接与命令行
  adapter/          尚待替换的平台业务适配
  chat/             AI 与记忆
  config/           业务配置
  console/          控制台后端及 web 前端
  db/               数据模型
  membership/       会员
  permission/       权限
  plugin/           尚待替换的业务插件封装
  plugins/          内置业务插件与显式原生安装清单
  resources/        公共资源
  system_control/   系统管理
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
例如洛克运行时图标位于 `data/rocom/resources/`；仓库自带的图鉴模板、字体和属性图标
仍保留在 `src/fxbot/plugins/rocom/resources/`，不会移动到数据目录。

在仓库根目录运行，测试会显式加载 `src/`：

```powershell
.venv/Scripts/python.exe -I -S -X utf8 -m unittest discover -s tests/migration -t .
.venv/Scripts/python.exe -m unittest discover -s tests -t .
.venv/Scripts/python.exe -m build --no-isolation --outdir temp/packages
```

应用入口为安装后的 `fxbot` 命令或 `python -m fxbot`。
令牌、Bot 白名单和监听设置读取 `data/config/runtime/config.json`，不读取环境变量。
CLI 的 `--host` 和 `--port` 可显式覆盖监听地址与端口。
独立入口已安装会员、权限、AI 聊天、会话命令与撤回处理；聊天与长期记忆维护使用原生调度器。
控制台已挂载聊天、长期记忆与网页独立对话接口，业务设置仍使用 `data/config/` 下原有配置。
共享服务仍在同一进程使用，应用统一负责启动和有序关闭；启动服务不会自动安装插件依赖。
插件放在 src/fxbot/plugins 下即可自动发现，不需要 JSON 声明、扫描配置或手动注册清单。
插件目录的 __init__.py 声明 __plugin_meta__ 中文元信息，命令使用 @on_command / @on_regex，
事件使用 @on_event / @on_message。核心按目录名统一注册路由、权限和控制台展示。
周期业务使用 @scheduled(interval=60, run_on_start=True)，任务执行、取消和等待清理由核心负责。
无需 setup、configure、Plugin 对象、JSON 或根打包配置清单。
群管理/入群验证、娱乐和视频解析已接入原生路由，不再依赖 NoneBot。
群管理遇到未知成员身份时拒绝操作；音乐扫码仅在私聊进行；点赞固定为每日一次、最多十个。
点赞记录保持 Bot QQ 号 → 用户 QQ 号的结构；运势按用户 ID 保存，同一用户当天跨 Bot 共享。
欢迎语等群配置使用完整会话身份，旧裸群号配置需明确归属后迁移，不自动覆盖。
视频默认发送本地文件 URI，要求 Bot 共享文件系统；远程 Bot 请启用视频配置的 use_base64。
已原生接入 B 站直播订阅插件，支持按房间号或 UID 订阅、取消、查询及开播推送。
订阅与推送按完整会话身份隔离；旧的裸群号/用户号订阅文件须明确平台和 Bot 后单独迁移，
不会被自动猜测归属或覆盖为空记录。
群聊日报也已原生接入，允许群和自动推送群需填写 `onebot11:100:group:300` 这样的完整会话键。
需要 NapCat 历史查询时，在运行配置中显式添加 `"onebot11_implementations": {"100": "napcat"}`，
对应 Bot ID 必须已经在 `bots.onebot11` 中声明；未配置时使用标准 V11，不提供历史查询扩展。

首次使用需创建该文件，例如以下内容；示例令牌和 Bot ID 必须替换为自己的配置：

```json
{
  "token": "replace-with-a-random-token",
  "bots": {"onebot11": ["100000"]},
  "host": "127.0.0.1",
  "port": 8683
}
```

V11 反向连接地址为 `/onebot/v11/ws`，只接收 Universal 连接。
V12 使用 `/onebot/v12/{platform}/{bot_id}/ws`，例如 `/onebot/v12/wechat/bot/ws`，
并在 `bots` 中声明对应的 `onebot12.wechat` 身份；握手要求 `12.*` 子协议。

### QQ 官方 Bot（Webhook / WebSocket）

独立 QQ 适配器位于 platforms/qq，不依赖 NoneBot。当前支持普通群、C2C、频道消息和频道私信，
支持官方 Webhook 回调和主动 WebSocket 网关；每个 AppID 明确选择一种模式，不与 OneBot 混用。
在同一运行配置里声明 AppID 和凭证：

    {
      "token": "replace-with-a-random-runtime-token",
      "bots": {"qq": ["123456789"]},
      "qq_bots": {"123456789": {"secret": "replace-with-your-app-secret"}},
      "host": "127.0.0.1",
      "port": 8683
    }

QQ 开放平台回调地址配置为 https://你的域名/qq/webhook，公网 HTTPS 入口需自行配置。
AppID 不是 QQ 号；普通群和 C2C 的权限身份使用官方 OpenID，例如 qq:123456789:group:GROUP_OPENID。
bots.qq 与 qq_bots 必须一一对应；禁止关闭验签，不读取环境变量或自动兼容旧配置。
token 是现有运行时令牌，不代替 QQ 回调的 Ed25519 签名。
频道 Bot 的真实用户 ID 从 /users/@me 查询，也可在凭证里显式配置 bot_user_id，不会用 AppID 猜。

WebSocket 模式把该 AppID 的凭证改为：

    {
      "secret": "replace-with-your-app-secret",
      "mode": "websocket",
      "intents": 1107296256
    }

省略 mode 默认为 webhook。WebSocket 主动通过官方 /gateway/bot 获取 WSS 地址，
不需要配置公网回调地址，也不接受该 AppID 的 Webhook 请求。
intents 默认订阅普通群/C2C 消息及频道 AT 消息，即 (1 << 25) | (1 << 30)；
频道私信等事件需按官方权限明确设置位掩码，不会因权限不足自动换一组订阅。
网关使用 READY 的真实 Bot 用户 ID，AppID 仍是内部连接身份。
支持心跳 ACK 检查、断线 Resume、无效会话重新 Identify、退避重连、额度等待和退出取消。
永久配置/权限错误会停止网关并记录日志，不反复重连。业务 HTTP 写操作始终不自动重试。
默认单分片；官方要求多个分片时，必须配置本进程负责的 shard，例如 [0, 2]，
其他进程负责其余分片。本版不在一个进程内自动启动多个同 AppID 的分片连接。
所有网络端点来自官方接口，TLS 必须验证，不读取环境代理，不提供任意自定义网关或验签关闭开关。

支持文本、引用 ID、群/频道提及、单项图片/音频/视频；频道及频道私信媒体目前只支持图片。
普通群历史、好友点赞、普通群管理、合并转发、文件分块上传、更多通知事件尚未实现，
相关能力明确拒绝，不通过失败后更换接口重试。平台配额、媒体格式和主动发送权限仍由官方限制。
事件去重和队列只保存在本进程内，不提供进程崩溃后的持久化投递保证；新接入应先做测试环境验证。

主人、管理员、权限白黑名单和会员会话使用完整身份，例如 `onebot11:100:private:200`、
`onebot11:100:group:300`。旧的裸用户/群 ID 不能直接混入新权限判定；未知归属不会被自动猜测，
应先明确平台和 Bot，再备份并迁移数据。独立入口不会把旧黑名单不匹配当作公开权限。

依赖声明已移除 NoneBot，但尚未迁移的业务源码和旧业务测试仍引用它；
这些模块尚不能在无 NoneBot 环境下使用，也未接入新的运行入口。
完整业务迁移尚未完成，不能视为生产部署版本。
