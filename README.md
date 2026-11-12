# FxBot

独立消息应用，当前正在直接替换旧框架运行链路，不提供旧路径别名或桥接层。

## 目录

```text
src/fxbot/
  __main__.py       模块启动入口
  bootstrap.py      应用装配
  core/             标准库核心：事件、路由、插件与生命周期
  runtime/          网络服务、协议连接与命令行
  adapter/          尚待替换的平台业务适配
  chat/             AI 与记忆
  config/           业务配置
  console/          控制台后端及 web 前端
  db/               数据模型
  membership/       会员
  permission/       权限
  plugin/           尚待替换的业务插件封装
  plugins/          内置业务插件
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

下载资源、缓存、配置和数据库统一写入所属项目的 `data/`，不读取路径环境变量。
源码运行从 `src/fxbot/` 定位仓库根目录；安装包运行以当前工作项目为根目录。
例如洛克运行时图标位于 `data/rocom/resources/`；仓库自带的图鉴模板、字体和属性图标
仍保留在 `src/fxbot/plugins/rocom/resources/`，不会移动到数据目录。

在仓库根目录运行，测试会显式加载 `src/`：

```powershell
.venv/Scripts/python.exe -I -S -m unittest discover -s tests/migration -t .
.venv/Scripts/python.exe -m unittest discover -s tests -t .
.venv/Scripts/python.exe -m build --no-isolation --outdir temp/packages
```

应用入口为安装后的 `fxbot` 命令或 `python -m fxbot`。
使用 `FXBOT_ACCESS_TOKEN` 环境变量配置访问令牌，通过 `--bot-id` 指定连接身份。
反向连接地址为 `/onebot/v11/ws`，当前只接收 Universal 连接。

依赖声明已移除 NoneBot，但尚未迁移的业务源码和旧业务测试仍引用它；
这些模块尚不能在无 NoneBot 环境下使用，也未接入新的运行入口。
完整业务迁移尚未完成，不能视为生产部署版本。
