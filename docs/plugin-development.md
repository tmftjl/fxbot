# 插件开发规范

插件是 `src/fxbot/plugins/` 下的直接子包，目录名是稳定 ID；中文名用于展示。
不需要注册清单、安装类或启动/停止模板，不依赖外部框架。

## 最小插件

在插件目录的 `__init__.py` 中声明：

```python
from fxbot.core.plugin import PluginMetadata, on_command

__plugin_meta__ = PluginMetadata(
    name="问候",
    description="回复问候消息",
    usage="#问候 名字",
)

greeting = on_command("问候", name="greet", display_name="发送问候", level="all")


@greeting.handle()
async def greet(ctx):
    await greeting.finish(f"你好，{ctx.arguments or '朋友'}")
```

默认命令前缀是 `#`、`＃`、`/`，参数通过空白分隔；命令别名通过 `aliases` 声明。
`name` 是权限 ID，`display_name` 是展示名，不依赖处理函数改名。
权限节点为 `插件目录名:greet`，已有权限配置不会被默认值覆盖。

响应器须保存在模块变量中；子模块由插件显式导入，核心收集插件及已导入子模块中的响应器。
重复引用同一响应器只注册一次，不自动递归导入未使用的业务模块。

## 多处理器与执行状态

```python
hello = on_command("你好", level="all")


@hello.handle()
async def prepare(ctx):
    ctx.state["name"] = ctx.arguments or "朋友"


@hello.handle()
async def reply(ctx):
    await hello.finish(f"你好，{ctx.state['name']}")
```

同一响应器的处理器顺序执行，共享本次调用的 `ctx.state`；其他事件、响应器和应用不共享这个字典。
处理器可以无参数，或者接收一个未注解的上下文参数。多个参数必须明确声明类型，
支持 HandlerContext、Event、BotClient、Matcher、State、CommandArguments、MatchResult 和 ApplicationServices。
参数在注册时编译绑定，未知类型、可变参数在启动前报错；不按参数名字猜测业务依赖。
类型注解必须能在声明模块中解析，不提供任意业务对象的自动注入容器。
`hello.send()` 发送后继续；`hello.finish()` 可选发送并结束整个响应器处理链。
`ctx.send()`、`ctx.finish()` 是上下文自身的操作，不是旧 API 转发入口。

`block=True` 阻止更低优先级路由；`finish()` 不自动改变 block。
同优先级响应器并发执行，即使其中一个设置 block，其他同级响应器仍会执行；
整组完成后才进入下一优先级，完成顺序不保证。需要明确先后依赖时使用不同 priority，
或者将步骤放在同一响应器的多个 handle 中。
禁止在已经结束的响应器之外使用 `hello.send()`；后台业务通过通知服务和明确目标发送。

## 规则与附加权限

```python
from fxbot.core.plugin import Permission, Rule


async def addressed(ctx):
    return ctx.event.to_me


async def enabled(ctx):
    return ctx.arguments != "关闭"


async def allowed_user(ctx):
    return ctx.event.user_id == "明确允许的用户ID"


hello = on_command(
    "你好",
    name="hello",
    rule=Rule(addressed) & enabled,
    permission=Permission(allowed_user),
    level="member",
)


@hello.handle()
async def respond():
    await hello.finish("你好")
```

检查函数接收一个上下文并异步返回布尔值；只接受严格的 `True`。
`Rule(a) & b` 要求全部通过，`Permission(a) | b` 任一通过；均按声明顺序短路执行。
顺序为匹配、统一权限、附加权限、规则、处理器，禁止用附加权限绕过统一权限节点。
`scene="group"` 或 `scene="private"` 限定会话，不改变业务数据的共享范围。
其他入口为 `on_regex(pattern, flags=...)`、`on_event(event_type)` 和 `on_message()`；
正则参数是完整匹配文本，事件参数为空字符串，普通消息参数是去除首尾空白的文本。

## 定时业务与资源

定时业务仍使用 `@scheduled(interval=60)`，按配置排期可用 `plan` 与 `watch_config`。
核心负责不重叠执行、取消、等待与记录异常；业务决定周期和首次校准逻辑。
`ctx.first_run` 仅在任务成功后改变，不受业务 `ctx.state["completed"]` 影响。

普通无状态对象可以在模块中创建；绑定应用、需要释放状态的对象通过 `ctx.service(factory)` 获取。
工厂是同步函数，接收运行时服务；有待释放资源的业务服务实现 `Service.close()`。
请求临时文件仍在业务自己的 `finally` 中清理，不改成周期任务。

匹配规则只做本地判断，不等待网络、元数据初始化锁或大文件操作；元数据未就绪时可登记需求，
交给 `@scheduled` 处理，不能让无关消息等待插件自己的外部服务。声明 `block=True` 的响应器应在
规则阶段确认业务目标有效，处理器里静默结束并不会让出低优先级路由。
长时间运行的执行任务必须由实际应用拥有；调用 `application.spawn()` 的真实资源服务应保留应用身份，
由核心关闭时取消并等待。不要只定义一个未注册的 shutdown 函数就假定框架会调用。

## 依赖声明与自动安装

插件第三方依赖写在插件目录的 `requirements.txt`，随插件一起打包。核心启动时按已安装发行版检查，
自动补装缺失项，不升级已安装版本，不需要额外配置。批量安装优先使用 uv，没有 uv 时退回 pip。
插件安装到运行核心的同一个 Python 环境，插件之间的版本冲突只记录安装器输出并继续启动，
核心不替插件做版本仲裁；导入时缺少依赖的插件会被记录后跳过，不影响其他插件。
涉及服务端资源的插件仍应在自己的 `requirements.txt` 中声明完整依赖。

## 事件并发

OneBot 和 QQ 的每个入站事件独立启动任务，不设固定并发上限或等待队列；动作响应和 QQ 心跳
不会等待业务槽位。同优先级响应器仍并发运行。

同一用户或群的消息也可能并发处理，不保证完成顺序。涉及共享状态的业务需自行使用
已有锁、事务或明确的占位状态；不能依赖“上一条消息应该已经执行完”。
例如每日点赞保留业务锁，验证码创建使用临时占位，身份查询期间退群会撤销待创建验证。
不要为这些业务在框架里添加特殊调度分支。
这是异步并发，不会让阻塞的同步计算自动变为多线程或多进程并行。

## 明确的匹配结果和应用服务

```python
from fxbot.core import CommandArguments, Event, State


@hello.handle()
async def reply(event: Event, arguments: CommandArguments, state: State):
    state["user"] = event.user_id
    await hello.finish(f"你好，{arguments}")
```

`ctx.match` 是不可变 MatchResult；`arguments` 保持既有命令／正则文本语义，
`group(1)` 取得从 1 开始的捕获组，`group("name")` 取得命名捕获组，不需要重新执行正则。
`ctx.app_services` 表示应用服务，`ctx.scope` 是插件在该应用中的业务对象缓存；不要与当前事件混淆。
`current_event_context()` 只在正在处理的事件内有效，继承上下文的子任务不能在分发结束后继续使用它。
响应器 `options` 和匹配函数只读，加载后不能继续增加处理器；运行状态只放在本次 ctx.state。

## 可选的控制台配置页

有配置页的插件在自己的模块中声明扩展：

```python
from .ui_schema import get_ui_schema

__plugin_extensions__ = {"console.config": get_ui_schema}
```

核心只保存通用扩展声明，控制台解释 console.config，不在请求时扫描或重新执行插件文件。
配置默认值由插件的 defaults.py 唯一声明，配置校验和 UI 都引用它；纯业务插件无需提供此扩展。

## 资源、帮助与测试

插件静态资源放在插件目录的 `resources/`，构建时随包打进 wheel；源码和安装包统一用
`package_root()` 或 `Path(__file__)` 定位，运行生成的缓存写入 `data/cache/<插件名>`，
不要把运行产物写回包目录。

帮助插件会扫描 `src/fxbot/plugins/*/help.json`（跳过 help 自身）。需要在帮助图里出现的插件，
在插件根目录放一个 `help.json`，结构沿用现有配置：

```json
{
  "title": "示例帮助",
  "sub_title": "一句话说明",
  "key": "示例",
  "aliases": ["example"],
  "col_count": 2,
  "groups": [
    {
      "group": "分组名",
      "list": [
        {"icon": 1, "title": "#示例命令", "desc": "命令说明"}
      ]
    }
  ]
}
```

`key` 和 `aliases` 用于 `#<关键词>帮助` 的解析；`background`、`icon`、`footer` 可选。
帮助图配置缺失或损坏时插件仍能启动，只是不出现在对应帮助页。

插件测试放在 `tests/plugins/test_<插件名>_native.py`，使用 `AsyncMock` 或本机回环，
不访问真实平台和外部服务；至少覆盖命令路由/权限、核心纯函数、一次成功发送和失败路径。

## 迁移边界

原来的 `@on_command(...)` / `@on_regex(...)` 直接函数装饰形式已删除；
改成模块级响应器和 `@响应器.handle()`，`when=` 改为 `rule=`，不保留两套加载链。
本实现采用声明与执行分离的设计，但不是任何框架的 API 兼容层。
当前不实现 `got`、`pause`、`reject` 的跨事件会话流程，也不支持第三方插件直接运行。
