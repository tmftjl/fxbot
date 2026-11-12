# 独立迁移审计基线

审计日期：2026-11-12。起点：`794a1a2ed73ba949186f0059d427bf3cce2321a4`。
本环境由用户确认为开发环境，不是生产运行环境；本轮未删除原有数据、连接平台或启动服务。

## 可复现检查

在仓库根目录运行，使用仓库内虚拟环境；历史基线数据保留在下文，不代表当前独立运行时已完成。

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:LOG_LEVEL='CRITICAL'
.venv/Scripts/python.exe -m unittest discover -s tests -t .
.venv/Scripts/python.exe -I -S -m unittest discover -s tests/migration -t .
.venv/Scripts/python.exe scripts/audit_nonebot.py
.venv/Scripts/python.exe scripts/audit_nonebot.py --details
ruff check . --output-format concise
ruff format --check . --output-format concise
```

旧测试在新增测试前运行：1309 项，65.232 秒，成功、跳过 1 项。
新增测试单独运行：16 项成功；使用 `-I -S` 禁用第三方包后，这 16 项仍全部通过。
新增代码后已重跑全量测试：1325 项，68.018 秒，成功、跳过 1 项。
使用 Python 3.12.9、NoneBot 2.5.0、OneBot 适配器 2.4.6、QQ 适配器 1.7.2、
NoneBot APScheduler 插件 0.5.0、SQLModel 0.0.42、FastAPI 0.141.1、Ruff 0.16.6。

原有 lint 失败：`console/routes/meta.py`、`tests/chat/test_personas.py` 的 I001。
原有格式检查失败：15 个文件，涉及 adapter、chat/memory、console、membership、permission、plugins 和 system_control。
本轮只格式化新增 Python 文件，未批量修复原有文件。新增文件 lint 和格式检查通过。
测试输出中还有 SQLite datetime adapter 弃用警告与 asyncio 慢任务诊断，未导致测试失败。
Ruff 配置排除了 `chat/amemorix`，所以全仓静态检查不能证明该目录无问题。

## 依赖清单与替换边界

审计脚本读取 Git 跟踪及未忽略的 Python 文件，只解析 AST，不导入项目。
当前运行时代码 442 个文件（含新增核心 3 个），164 个文件直接导入框架，350 个导入符号。
其中 87 个文件的直接框架导入仅为 logger；这不代表它们没有间接依赖。

| 区域 | 直接依赖文件数 | 必须替换的边界 |
| --- | ---: | --- |
| 根入口、bootstrap | 2 | 插件元数据、生命周期、动态加载 |
| adapter | 12 | Bot/Event、消息对象、参数注入、Bot 注册表、上下文、异常继承 |
| chat | 62 | 消息和通知路由、Matcher、日志、后台发送与运行上下文 |
| config / db / utils | 5 | 日志、路径配置读取及根包导入副作用 |
| console | 12 | 宿主应用、在线 Bot、状态和发送边界 |
| membership / permission / plugin | 7 | 全局预处理、门禁拒绝、Rule/Permission、Matcher 工厂 |
| plugins | 60 | 命令与通知、参数注入、finish、生命周期与后台任务 |
| system_control | 4 | 连接钩子、统计补丁、退出与重启 |

关键源码证据：

- `__init__.py` 在导入时就访问 driver；`bootstrap.py` 使用 `load_plugins`。
  直接把独立核心放在旧根包下面并按旧包名导入，会先执行旧根包入口。
  因此当前新增顶层 `core`，但打包规则尚未迁移，不能声称已经支持独立安装。
- `plugin/builder.py` 返回真实 NoneBot Matcher，通过 `__getattr__` 动态查找工厂。
  保留 `P.on_*` 外观不等于保留了行为，必须逐项迁移调用者和控制流。
- `adapter/core/bot.py` 的 `PlatformError` 继承 `IgnoredException`，`current_bot()` 回退到框架内部上下文。
  `bind_bot()` 只设置 ContextVar，没有 token 恢复入口；新核心必须支持恢复和并发隔离。
- `adapter/uninfo/params.py` 将会话查询包装成 `Depends`；ORM 经 `get_bots()` 反查在线连接。
  原有 `nonebot_plugin_uninfo_*` 表名是历史数据名称，不应当作待删除模块依赖。
- `adapter/platforms/qq.py` 在导入时修改 QQ 适配器发送响应类型和 `send_to_group`。
  `system_control/stats_patches.py` 动态导入 QQ/wxauto 并包裹发送方法、接收处理和 API 钩子。
- `utils/paths.py` 使用 driver 配置选择数据路径；`console/server.py` 通过 `get_app()` 获得宿主。
- `membership/tasks.py`、`chat/memory/tasks.py`、群分析和洛克王国调度依赖调度插件；
  其他子插件还通过启停钩子管理循环、资源预热和清理。
- `tests/chat/support.py` 自动初始化 NoneBot 并指定临时目录；config 测试经包导入间接依赖它。
  现有测试通过不等于独立运行可用。`tests/migration` 不导入这套支持模块。
- `pyproject.toml` 仍声明 NoneBot 插件入口和旧包发现规则；`requirements.txt` 与项目依赖不完全一致。
  QQ 适配器已安装且源码直接导入，但两份仓库依赖声明均未声明它，干净安装存在缺口。

## 最小行为兼容清单

静态调用候选：17 个 `on_command`、84 个 `on_regex`、12 个 `on_message`、4 个 `on_notice`，
327 个 `finish()` 调用。`--details` 输出每个候选的文件、行号、位置参数、priority、block、rule 和 permission 表达式。
工厂循环可能运行多次，分支可能不运行；候选不是有效命令数量或覆盖率。
通用 `.on()` 与动态字符串需要人工复核：业务扩展钩子不是 Matcher，数据库表名也不是动态模块。
脚本不建立完整调用图，也不能识别所有任意别名、反射或运行时生成的注册。

- 普通命令：匹配方式、捕获参数、别名、命令参数注入、权限和场景、展示名。
- 路由顺序：常见命令优先级 5；音乐登录存在 4；AI 上下文记录为 98 且不阻断，兜底为 99 且阻断。
  同级处理、规则检查和阻断的准确语义仍需特征测试，不能仅照抄数字。
- 控制流：`finish()` 的发送及提前结束；本次扫描没有发现 reject/pause/got/receive 调用候选。
  无候选不证明不存在动态调用，后续批量迁移前必须复核。
- 预处理：消息片段归一化、忽略特定提及、Bot 绑定、会员门禁；注册先后不等于框架实际执行顺序已验证。
  当前 `should_process_fxbot_message()` 直接返回 True，不能凭名字把它视作已实现的过滤器。
- 会员：数据库故障拒绝、管理员豁免、续费恢复通路、缓存失效与 UTC 到期判断。
  当前门禁源码对续费查询失败的特殊放行以实际正则分支为准，其他恢复命令需补特征测试。
- AI：事件 ID、消息 ID、轮次与回复目标分离；去重、历史只写一次、普通/Focus 分流、取消与部分发送。
- 插件：13 个含入口的内置插件目录，模型必须先于建表导入；配置 schema 和帮助资源需继续可发现。
- 后台能力：断线发送、调度热重载、启动中途失败、取消与清理、统计只记成功发送、重启逻辑。
- 控制台：认证、前端静态资源、Bot 在线状态、平台操作与聊天接口。

上述是必须保留及补测的清单，不是宣称本轮已逐项验收。
现有测试主要位于 chat，另有 config 和 plugins；缺少独立协议传输、完整生命周期与整套平台端到端测试。

## 平台与试点决定

源码注册 OneBot V11、OneBot V12、QQ 官方三个适配器，V12 中另有 wxauto/微信兼容判定。
统计模块也有 wxauto 动态接入线索；环境未找到名称含 wxauto 的已安装分发包，不能据此断言所有接入都不可用。
没有连接真实账号，也未读取令牌或推断用户线上平台分布，所以“实际线上使用哪些平台”仍未确认。

首个开发试点选 OneBot V11：已有明确能力实现，可用本地协议夹具覆盖消息、通知、API 请求关联和收发。
它是工程试点选择，不代表其余平台可以删除。后续顺序暂定 OneBot V12/微信兼容，再处理机制差异较大的 QQ 官方；
用户实际使用优先级可以调整该顺序。真实连接验证需与本地模拟验证分别记录。

## 首批核心的边界

`core` 当前仅包含类型和 ContextVar 上下文，不负责路由、连接、调度和日志。
会话键对分隔符进行转义，事件和目标以及绑定的客户端必须属于同一 Bot；跨任务绑定有隔离测试。
这些新键尚未写入旧数据库，不能直接替代现有聊天命名空间；数据读写接入前必须明确新旧键处理方式。
冻结的数据类仍包含可变消息与字典，不构成深层不可变安全快照；后续守卫和观察者不能直接依赖其不可变性。
`-I -S` 隔离测试确认解释器找不到 NoneBot 且能导入核心；这不是项目完整无依赖安装测试。
