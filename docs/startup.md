# 启动配置说明

本文只描述当前独立运行时的实际操作方式；协议与路由细节见[核心功能](core.md)。

## 安装与启动

```powershell
# 在仓库根目录创建/使用已有 venv 后，以可编辑方式安装
uv pip install -e .

# 启动：二选一
.venv/Scripts/python.exe -m fxbot
fxbot
```

源码运行时仓库根目录由 `src/fxbot/` 反推；安装包运行时以当前工作目录为项目根。
所有下载资源、缓存、配置和数据库都写入当前项目根下的 `data/`。

使用 uv 项目模式时可以直接同步并启动：

```powershell
uv sync          # 自动创建 .venv，按 uv.lock 安装核心项目与依赖
uv run fxbot     # 启动；插件依赖在启动时自动补齐
```

说明（与 gsuid_core 的实现一致）：

- `uv sync` 默认是 **exact**：环境精确对齐 `pyproject`/`uv.lock`，会移除插件依赖。
  下一次启动时核心会重新自动补装，所以功能不受影响，只是多一次安装
- `uv run` 默认是 **inexact**：只补齐项目依赖，**不会移除**启动时补装的插件依赖；
  只有显式 `uv run --exact` 才会删
- 频繁执行 `uv sync` 时用 `uv sync --inexact --extra dev`，可以避免重复补装插件依赖，
  并保留 ruff/mypy 等开发工具
- `uv sync` / `uv run` 在缺少 `.venv` 时会自动创建，不需要手动建环境
- 需要更新锁文件时运行 `uv lock`；`uv.lock` 提交后核心依赖版本可复现

gsuid_core 的更新路径也是 `uv sync --inexact --no-install-project`，日常运行用
`uv run core`（inexact），并不是反复执行精确同步。

帮助图和群聊日报长图使用 Playwright 截图 HTML。启动后核心会在后台检测 Chromium，
缺失时自动执行 `python -m playwright install chromium`，不阻塞服务就绪；也可手动安装：

```powershell
uv run playwright install chromium
```

自动安装失败（离线、磁盘不足等）只记录警告，帮助图回退到 PIL 摘要图，群聊日报回退到统计摘要图，
服务不会启动失败。所有 HTML 长图共用核心 `runtime.browser`：按需启动一个 Chromium、
限制并发页面数，应用关闭时统一回收。

## 运行配置

入口严格读取 `data/config/runtime/config.json`，不读取环境变量，拒绝未知字段。
首次运行需要手动创建该文件，最基本的鉴权配置示例：

```json
{
  "token": "replace-with-a-random-token",
  "host": "127.0.0.1",
  "port": 8683
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `token` | `""` | OneBot V11/V12 的 Bearer 令牌。空串或缺省表示不鉴权；不鉴权时不要监听公网 |
| `host` / `port` | `127.0.0.1` / `8683` | HTTP 与 WebSocket 监听地址；CLI `--host` / `--port` 可覆盖 |
| `action_timeout` | `15.0` | 单次平台动作、鉴权与关闭等待的秒数，必须为有限正数 |
| `queue_size` | `128` | 每个连接的事件等待队列容量 |
| `event_concurrency` | `8` | 每个连接同时处理的事件数 |
| `max_pending` | `128` | 每连接未完成动作请求上限 |
| `close_timeout` | `5.0` | 连接关闭与任务回收等待秒数 |
| `qq_bots` | `{}` | QQ 官方 AppID 到凭证配置，见下文“QQ 官方 Bot” |

已删除 `bots` 白名单和 `onebot11_implementations`：Bot 身份由连接声明，
OneBot 扩展动作在首次使用时通过 `get_version_info` 自动识别 NapCat / SnowLuma。
鉴权不通过、身份格式无效或同一个 Bot 重复连接都会被拒绝。

## 平台接入

- OneBot V11 反向 WebSocket：`/onebot/v11/ws`，只接受 `X-Client-Role: universal`
- OneBot V12：`/onebot/v12/{platform}/{bot_id}/ws`，需要 `12.*` 子协议
- QQ 官方：`POST /qq/webhook`（Ed25519 验签）或主动 WebSocket 网关
- 控制台：`/fxbot`；健康检查：`/health` 与 `/fxbot/health`

V11/V12 在 `token` 非空时要求 `Authorization: Bearer <token>`；QQ 回调的验签与凭证不受该开关影响。

## QQ 官方 Bot

`qq_bots` 按 AppID 声明凭证，每个 AppID 明确选择一种传输方式：

```json
{
  "qq_bots": {
    "123456789": {
      "secret": "replace-with-your-app-secret",
      "mode": "webhook",
      "bot_user_id": "",
      "intents": 251658240
    }
  }
}
```

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `secret` | 必填 | QQ 开放平台 AppSecret，用于回调 Ed25519 验签或换取 access token |
| `mode` | `webhook` | `webhook` 或 `websocket`，同一 AppID 只能选一种 |
| `bot_user_id` | `""` | 可选，频道场景下的 Bot 真实用户 ID；不填时通过 `/users/@me` 查询 |
| `intents` | 位 24–27 | 31 位权限掩码，默认 `251658240`（群成员、群/C2C 消息、交互、审核） |
| `shard` | 不填 | `[分片序号, 分片总数]`；不填且网关要求多分片时自动连接全部分片 |
| `api_base` / `auth_url` | 官方地址 | 必须为 HTTPS，仅从项目配置读取 |
| `gateway_url` | 不填 | 自定义 WSS 网关地址，必须为 WSS |

Webhook 回调路径为 `POST /qq/webhook`，必须由公网 HTTPS 入口转发；验签强制开启，
时间窗口 300 秒，不接受关闭验签或兼容旧配置。`mode=websocket` 时不需要回调地址，
由核心主动连接官方网关。AppID 不是 QQ 号；普通群和 C2C 的身份使用官方 OpenID。

## 插件依赖

插件放在 `src/fxbot/plugins/<目录名>/`，只要存在 `__init__.py` 并声明 `__plugin_meta__` 就会被发现。
第三方依赖写在插件目录的 `requirements.txt`，随包提供。

启动时会检查这些声明并自动补装**缺失项**：同一进程只检查一次，批量执行
`uv pip install --python <当前解释器>`；没有 uv 时退回 `python -m pip install`。
不会升级已安装版本，也没有额外开关。插件安装到运行核心的同一个 Python 环境。

- 版本冲突：核心不仲裁，只打印安装器输出（明确标记“依赖冲突”）后继续启动
- 插件导入时缺依赖：记录错误并跳过该插件，其他插件和核心继续运行
- 依赖安装关闭标准输入，最多等待 180 秒；超时会终止并回收安装进程，不重复安装后再试。
  正常失败或超时记录日志后继续核心启动。
- 需要固定依赖版本时，由插件维护者在自己的 `requirements.txt` 中收紧范围

## 构建与发布

构建后端为 `pdm-backend`：

```powershell
uv pip install pdm-backend      # 或安装 dev 额外依赖：uv pip install -e ".[dev]"
python -m build --no-isolation --outdir dist
```

- 项目使用标准 `src` 布局，`pdm-backend` 自动发现 `src/fxbot`
- 包内资源（`resources/`、`prompts/`、`web/dist/`、插件 `help.json`、`persona.md` 等）默认随包打入
- `pyproject.toml` 的 `[tool.pdm.build] excludes` 排除了前端 `node_modules`
- 构建和 editable 安装不会在源码树生成 `.egg-info`

## 验证命令

```powershell
# 全量测试
.venv/Scripts/python.exe -m unittest discover -s tests -t .

# 无第三方包的标准库隔离测试
.venv/Scripts/python.exe -I -S -X utf8 -m unittest discover -s tests/migration -t .

# 静态检查
.venv/Scripts/python.exe -m ruff check src tests
.venv/Scripts/python.exe -m ruff format --check src tests
.venv/Scripts/python.exe -m mypy
```

新增插件测试放在 `tests/plugins/`，不访问真实平台和外部服务。
