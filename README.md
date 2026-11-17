# FxBot

独立消息应用，运行要求 Python 3.11 及以上；核心本身只使用标准库。

## 功能

- **消息平台**：OneBot V11/V12 反向 WebSocket；QQ 官方 Webhook 与主动 WebSocket 网关
- **插件系统**：目录自动发现，`on_command` / `on_regex` / `on_event` / `on_message` 声明响应器，
  支持权限、规则、多处理器、定时任务、控制台配置页和依赖自动补装
- **控制台**：`/fxbot` 提供聊天、长期记忆、配置、权限、会员、日志和缓存管理
- **AI 与记忆**：原生聊天管线、Provider 路由、上下文/分段/错字/表情处理与长期记忆
- **权限与会员**：完整会话身份，支持群角色、白名单/黑名单、会员门禁和自动维护
- **内置插件**：群管理/入群验证、娱乐、视频解析、B 站直播、群聊日报、表情包制作、图库、
  帮助、无限二代、洛克王国、服务器运维、实用工具、鸣潮表情和 gscore 连接器

## 快速开始

```powershell
uv sync && uv run fxbot
```

首次运行前创建 `data/config/runtime/config.json`，最少只需要监听地址；需要鉴权时填写
`token`。完整字段、QQ Bot 凭证、OneBot 连接地址和构建说明见[启动配置说明](docs/startup.md)。
安装后也可以使用 `fxbot` 命令或 `python -m fxbot`。

## 插件开发

插件放在 `src/fxbot/plugins/<目录名>/`，存在 `__init__.py` 并声明 `__plugin_meta__` 即会自动发现：

```python
from fxbot.core.plugin import PluginMetadata, on_command

__plugin_meta__ = PluginMetadata(name="问候", description="回复问候", usage="#问候 名字")

hello = on_command("问候", name="greet", display_name="发送问候", level="all")


@hello.handle()
async def greet(ctx):
    await ctx.finish(f"你好，{ctx.arguments or '朋友'}")
```

第三方依赖写在插件目录的 `requirements.txt`，启动时自动补装缺失项；插件之间冲突只报告，
不影响核心启动。响应器、权限、规则、定时任务、资源、`help.json` 和测试约定见
[插件开发规范](docs/plugin-development.md)。

## 文档

- [核心功能](docs/core.md)：目录结构、事件、路由、能力、生命周期、平台适配与内置插件行为
- [插件开发规范](docs/plugin-development.md)：响应器、权限、定时任务、资源与依赖声明
- [启动配置说明](docs/startup.md)：安装、运行配置、QQ Bot、插件依赖、构建与验证命令
