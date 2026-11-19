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
处理器也可调用 `ctx.stop_event()`（或 `hello.stop_event()`）动态停止传播，
`ctx.is_stopped()` 查询状态。它不立即跳出当前函数，但当前函数返回后不再运行本响应器的
后续处理器，也不进入低优先级路由，即使声明了 `block=False`。
需要立即结束当前函数时，先 `ctx.stop_event()`，再 `await ctx.finish()`；`finish()` 的原有语义不变。
同优先级响应器并发执行，即使其中一个设置 block，其他同级响应器仍会执行；
动态 `stop_event()` 同样不会取消同级响应器。
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

### 开放式 AI 对话

正式入口在 `fxbot.chat.extensions`，使用本项目现有 Provider、工具、历史和发送实现。
插件是可信 Python 代码，上下文不是沙箱。完整设计见 [开放式 AI 对话扩展设计](chat-extension-design.md)。

```python
from fxbot.chat.extensions import ChatExtension, LLMRequest, chat_service

extension = ChatExtension("my_chat")


@hello.handle()
async def chat_command(ctx):
    ctx.stop_event()
    chat = await chat_service.context(ctx)
    await chat.request_llm(LLMRequest(
        provider_id="coding",  # 已配置的 Provider ID；省略则按现有路由选择
        messages=[
            {"role": "system", "content": "只分析 bug 和编写代码"},
            *chat.history(),
            {"role": "user", "content": ctx.arguments},
        ],
        temperature=0.3,
        max_tokens=8192,
    ))
```

`chat_service.context(ctx)` 建立独立的聊天上下文和发送目标，不保存已结束的 HandlerContext。
托管执行重新核对聊天权限及 request_guard，再按现有轮次锁、历史和发送管线执行。
网页渠道和系统续话也使用同一 ChatContext，但没有真实平台 Bot / Event。

| 接口 | 行为 |
| --- | --- |
| `await chat.llm_generate(request)` | 单次模型调用，不自动执行工具、落聊天历史或发送 |
| `await chat.tool_loop_agent(request)` | 标准工具循环，返回最终 LLMResponse，不自动落历史或发送 |
| `await chat.request_llm(request)` | 托管模型及工具执行，自动落库和最终发送 |
| `await chat.submit(result)` | 提交 str、LLMResponse 或 ChatResponse，核心落库并最终发送 |
| `await chat.send(message)` | 发送进度或普通消息，走 send_guard，不当作最终 AI 回复记账 |
| `chat.new_turn(instruction)` | 创建独立主动轮次，不把唤醒指令记录为用户发言 |

低层调用仍使用 Provider 租约、用量统计和模型调用追踪。
`LLMRequest` 支持 model、temperature、max_tokens、timeout、max_steps、tool_timeout、use_hooks、phase。
默认单次模型超时 240 秒、工具超时 120 秒、工具循环 20 步；超时可传 None，步数可显式调整。
达到工具步数上限报错，不自动重试副作用工具，也不回落到默认聊天。

`system_prompt` 非空时作为第一条 system 消息；`image_urls` 附到最后一条 user 消息。
原始 messages 不被原地改写。显式请求不自动添加人格或增强参考。

#### 动态工具

```python
from fxbot.chat.extensions import ToolRegistry, ToolSpec

tools = ToolRegistry()


async def lookup(tool_context, runtime, query):
    return {"query": query, "answer": "工具结果"}


tools.register(ToolSpec(
    name="lookup",
    description="查询业务数据",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    handler=lookup,
))
```

把注册表传入 `LLMRequest(tool_registry=tools)`；工具循环使用现有参数校验、权限与 tool_guard。
单次调用也可以仅传 tools 定义，自行处理模型的 tool_calls。
独立注册表可 `select(names)` 或 `merge(other)`，无需污染全局 `@tool` 注册表。
`chat.tools()` 获取默认过滤后的工具；`chat.run_tool(..., registry=tools)` 手动执行，
`chat.tool_message(call, result)` 复用截断和暂存生成可回填消息，不偷偷调用结果整理模型。

#### 可修改 hook

用 `extension.on(kind, callback, priority=...)` 注册，返回注销句柄；`extension.close()` 撤销该扩展注册和调度所有权。
从插件自己的 hook 或任务中 close 时，不主动取消当前任务，仍取消该扩展的其他任务；
外部关闭则照常取消其运行任务。关闭不会绕过仍然有效的权限、发送守卫和会话失效检查。
同步与异步回调均可；通过参数原地修改或 `chat.set_result(...)` 设置结果，不返回替代值。

| kind | callback 参数 |
| --- | --- |
| `message` | `(chat)`，基础准入后、默认门控和 Focus 之前观察消息 |
| `gate` | `(chat, decision)`，修改 allow、force、debounce |
| `chat_request` | `(chat)`，设置 model_request、executor 或 result，替换默认 Planner |
| `llm_request` | `(chat, request)`，修改完整消息、Provider、模型参数和工具 |
| `llm_response` | `(chat, request, response)` |
| `tool_start` | `(chat, spec, arguments)`，参数修改后重新校验 |
| `tool_end` | `(chat, spec, arguments, result)`，修改工具成功结果封装 |
| `reply` | `(chat, response)`，修改最终正文或 metadata["reply_parts"] |
| `after_send` | `(chat, message, receipt)`，实际发送成功后调用 |
| `chat_done` | `(chat)`，生成执行结束，读取 status、error、last_response；不代表平台发送已成功 |

```python
async def choose_chat(chat):
    if chat.session_id != "目标会话":
        return
    chat.model_request = await chat.prepare_request(persona=False, expressions=False)
    chat.model_request.system_prompt = "你是代码审查助手"
    chat.stop_event()


extension.on("chat_request", choose_chat)
```

`ChatContext.stop_event()` 停止当前聊天入口后续处理，与 HandlerContext 的平台事件传播不是同一作用域。
仅返回 None 不表示接管；未设置自定义请求/执行器/结果且未停止时，继续默认 Planner / 直聊。
executor 是接收 ChatContext 的异步函数，可返回 str、LLMResponse、ChatResponse 或 None（结束不回复）。

message / chat_request 的异常结束当前入口，不回落；其他修改 hook 记录异常继续；guard 保持拒绝优先。
取消始终传播。同类 hook 嵌套时不再次派发该类；模型请求还可 `use_hooks=False` 显式跳过模型 hook。
回复与 Planner 的模型请求派发模型 hook，摘要、学习和结果整理等辅助任务默认不派发。
用 chat.phase / request.phase 区分普通回复、Planner 和 Focus 规划阶段。
流式 Planner 同样派发 llm_request；流式分片不派发 llm_response，只在完整结果上执行一次响应 hook。
流式调用使用整次读取预算，取消、超时和轮次失效都会关闭原生流，未完成结果不作为成功回复派发。
模型 hook 的 tool_registry 仅用于该次请求，框架从 response.request 取回执行映射，不修改共享 runtime.registry。
推理续写及正文重生成也经过模型 hook，`request.operation` 分别为 `continuation`、
`regeneration`，普通调用为 `chat`。`response.request` 保存实际调用的 Provider、模型、参数和消息快照，
续写、重生成沿用这份快照，而不是重新猜测默认路由。
只有目标 Provider 与模型不变时才复用原生续写状态；hook 换目标或增加工具时使用可移植消息重新生成，
不把旧 Provider 的 response id、reasoning items 等私有状态交给新目标。
续写返回的工具调用仍执行标准工具循环，不静默丢弃；use_hooks=False、超时和取消同样适用。

reply hook 修改正文时正文覆盖旧分段；只修改 reply_parts 时使用新分段。
直聊、普通回复和插件结果提交使用同一规则，并保留 hook 添加的其他 metadata，不重建结果对象丢失扩展信息。
核心按最终分段落库、发送。未发送内容不会进入可见助手历史，重复提交同一 ChatContext 报错。

#### 门控和自建心流

```python
def improve_gate(chat, decision):
    if chat.session_id == "目标会话":
        decision.force = True  # 跳过默认概率、冷却；不能绕过基础权限和总开关
        decision.debounce = False


extension.on("gate", improve_gate)
```

`allow=False` 禁止本条消息启动默认回复，仍可按配置记录原始上下文。
`force=None` 沿用默认判断；True 强制触发；False 取消本条消息的强制标记、交回概率门控。
防抖批按每条消息分别判断，再汇总为是否强制回复；后续普通消息的 False 不会取消前面的 @ 触发。
显式 force 覆盖使用普通调度路径，而非交给 Focus。

完全替换时使用 `await extension.claim_session(chat, receive)`。同会话存在不同所有者时报错，
显式 `replace=True` 才交接。交接清理默认待处理、防抖、续话和活动执行，并脱离 Focus。
所有者接收消息，默认调度不会同时启动。默认允许所有者绕过作息策略，可设 allow_rest_time=False；AI 总开关仍生效。
平台向所有者投递前同样去重并刷新会话活动序号，重连重投不应重复启动业务操作。

```python
async def receive(chat):
    # 插件自行决定是否回复、怎样使用历史和工具
    request = await chat.prepare_request()
    await chat.request_llm(request)


async def select_flow(chat):
    if chat.session_id == "目标会话":
        await extension.claim_session(chat, receive)


extension.on("message", select_flow)
```

后台持续心流或定时唤醒由 `chat.scheduling.spawn(chat, operation, key="main", delay=...)` 启动，
任务必须由实际 Application 持有；同 key 不隐式覆盖运行任务。
operation 内使用 `chat.new_turn(...)` 发起独立轮次。
`chat.service.running(session_id)` 查询活动执行，`chat.service.cancel(session_id, turn_id=...)` 取消指定执行。
`chat.service.queue_message(...)` 仅对明确进入 Planner 循环的轮次接受追加；自建心流自行选择排队或打断。
`await extension.release_session(session_id)` 归还调度权；关闭 extension 自动释放。
清空会话取消在途心流并使旧轮次失效，已完成工具副作用不会回滚。
并发交接按前一次清理完成的顺序推进，新所有者就绪前不投递消息或启动它的后台操作。
被取代的上下文不能向新所有者登记任务；取消交接会等待旧任务 finally 完成，不重复打断清理。
取消清理回调不能同步发起新的交接，以免形成“等待自己退出”的循环；需要续接时在清理结束后再安排。
应用关闭会取消并等待正在交接的任务，关闭期间拒绝新的 claim / spawn。

网页渠道同样在基础鉴权与 ingress_guard 之后、记录历史和默认回复之前派发 message / gate，
可以选择调度所有者，也可以通过 allow=False 或 stop_event() 阻止默认执行。
网页私聊仍是即时手动输入，不额外引入群聊概率、防抖；force / debounce 不把它改成普通群调度。
网页接入前按当前会话策略或已有调度所有者检查作息；存在全局 message / gate hook 本身不提供豁免，
不可接入的会话仍返回 disabled，不先写历史再静默丢弃。需要作息豁免时应显式声明会话策略或调度所有权。
网页 ChatContext 具有真实 Application 和当前流的文字发送器，但不伪造 Bot / Event。
临时发送器仅对当前网页流有效：断开会取消并等待从该流派生的心流任务，
不会取消后来连接的新发送器任务；它不是离线推送或持久订阅通道。

#### 记忆、表达和历史

默认管线保留原有增强与学习行为。插件可组合调用：

- `await chat.prepare_request(persona=..., memory=..., expressions=..., summary=..., vision=...)`。
- `await chat.recall()` 读取默认长期记忆参考；传 query 时调用现有记忆检索。
- `await chat.expressions(query)` 获取表达参考；`chat.system_text(persona=False)` 返回空人格。
- `chat.history()` 获取真实用户与已发送助手正文的快照，排除当前消息、工具和内部规划。
- `await chat.append_history(...)` 显式导入用户发言；`await chat.update_history(message_id, text)` 更新历史。
- `await chat.clear_history(full=False)` 清空，full=True 同时使用现有完整重置流程。
- `chat.sessions / repository / providers / runtime / request` 开放已有底层服务与工作对象。

设置 `chat.learn=False` 关闭该上下文后续的学习写回触发，历史仍按标准路径保存；
若要控制原始入站记录触发的学习，应在 message hook 中设置，chat_request 阶段不能撤回已经投递的学习任务。
此开关不删除已学习内容，也不是把这条历史从所有未来检索中永久隐藏。
临时模型消息不自动成为用户发言或独立学习证据。

旧 persona / system_prompt / prompt / reply_text hook 和 PersonaOverride / SystemPromptSection 已删除。
依赖旧接口的业务插件需自行迁移；导入失败时回收已注册回调并跳过该插件，不恢复旧接口。
内置 server_admin、infinite_gen2 已使用新的 llm_request 注入独立系统消息，
保留原有权限、确认及直聊策略，并在工具循环、续写和热更新时刷新自身提示段，避免重复注入。

原来的 `@on_command(...)` / `@on_regex(...)` 直接函数装饰形式已删除；
改成模块级响应器和 `@响应器.handle()`，`when=` 改为 `rule=`，不保留两套加载链。
本实现采用声明与执行分离的设计，但不是任何框架的 API 兼容层。
当前不实现 `got`、`pause`、`reject` 的跨事件会话流程，也不支持第三方插件直接运行。
