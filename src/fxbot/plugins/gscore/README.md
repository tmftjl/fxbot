# gscore 连接器

把 FxBot 平台事件转换为 gsuid_core（SayuCore）的 WebSocket 协议，并把 gsuid_core 下发的
`MessageSend` 交给当前应用在线的 FxBot `BotClient` 发送。

## 目录

```text
gscore/
  __init__.py       插件声明、入站 matcher、连接维护任务
  client.py         GscoreService：WS 连接、上行队列、退避重连、关闭清理
  dispatch.py       下行分发与 Bot 日志帧处理
  inbound.py        入站事件到 MessageReceive 的统一入口
  models.py         json + dataclass 协议模型
  config.py         gscore 配置注册与校验
  defaults.py       唯一默认值
  ui_schema.py      控制台配置页
  adapters/
    base.py         适配器协议、通用事件/发送实现
    onebot11.py     OneBot V11（bot_id = onebot）
    onebot12.py     OneBot V12（bot_id = onebot_v12）
    qq.py           QQ 官方（bot_id = qqgroup / qqguild）
```

连接器、入站 matcher 和下行 dispatch 都只依赖 `adapters` 注册表；新增平台不需要修改
`client.py`、`__init__.py` 或 `dispatch.py`。

## 新增适配器

1. 新建 `adapters/<platform>.py`。
2. 继承 `CoreAdapter`，至少声明：
   - `key`：注册表唯一标识，重复注册会替换旧实例并保持顺序。
   - `name`：日志展示名。
   - `platform_matches(platform)`：匹配 FxBot `Event.platform` / `BotClient.platform`。
   - `matches_send(bot_id)`：匹配 gsuid 下行 `MessageSend.bot_id`。
   - `receive_bot_id(event)`：上行 `MessageReceive.bot_id`。
3. 按需覆盖：
   - `receive_scope(event)`：上行 `user_type` 与 `group_id`。频道等非 `group` 核心目标需要覆盖。
   - `build_receive(...)`：默认实现已处理 text/at/reply/image/record/video/file、命令前缀、
     引用图片、`user_pm`；只有平台字段特殊时才覆盖。
   - `build_meta(event)`：默认已处理 `member_joined`、`member_left`、`notification` 中的 poke；
     其他 meta 事件在自己的适配器中添加，未映射事件返回 `None`。
   - `target(bot, msg)`：把 gsuid 的 `target_type/target_id` 转为 `SessionTarget`。
   - `build_message(...)`：平台高级消息段或媒体限制。
   - `check_passive(...)`：平台无法显式使用被动消息 ID 时必须记录限制。
   - `send_files(...)`：只有需要独立上传动作的平台（如 OneBot V11）才覆盖。
4. 在 `adapters/__init__.py` 的内置注册元组中增加一行实例。
5. 在 `tests/plugins/test_gscore_native.py` 增加：入站转换、下行发送目标/消息段、
   echo 回执、meta 转换，以及“注册新适配器后不改连接器即可 dispatch”的用例。
   不访问真实网络，WS 使用替身或本机回环。

`Adapter.send` 的返回值是 `SendResult(recall_id=...)`：没有平台消息 ID 时为 `None`，
一帧被平台展开成多条消息时为 `list[str]`。连接器负责在 `MessageSend.echo` 非空时回传
`recall_message_id` 回执。

## bot_id 与 target 约定

| gsuid bot_id | FxBot 平台 | target_type |
| --- | --- | --- |
| `onebot` | `onebot11` | `group` / `direct` |
| `onebot_v12` | `onebot12.*` | `group` / `direct` |
| `qqgroup` | `qq` 普通群 / C2C | `group` / `direct` |
| `qqguild` | `qq` 频道 / 频道私信 | `group`（频道）、`direct`（频道私信） |

QQ 频道私信的 `SessionTarget.target_id` 为 `dms/{guild_id}/{用户ID}`，guild_id 从下行
content 中的 `group` 段读取。上行 `qqguild` 频道消息的 `user_type` 保持 `group`，
与 gsuid_core 的 `guild_send` 语义一致。

## 配置

控制台命名空间为 `gscore`，字段：`enabled`、`host`、`port`、`token`、`bot_id`、`reply_image`。
连接地址为 `ws://{host}:{port}/ws/{bot_id}[?token=...]`。`client.py` 只在配置的连接字段
变化时重建连接，`reply_image` 只影响入站转换。
