# syntax=docker/dockerfile:1

# 固定 Debian 大版本：python:3.12-slim 会随上游迁移到下一个 Debian 版本，
# 同样的 Dockerfile 隔段时间构建出的基础系统就变了。
FROM python:3.12-slim-trixie

# 可选构建期代理，由 docker compose 的 build.args 注入（例如本机 Clash）。
# 只作为构建参数，不写进最终镜像的运行时环境。
ARG http_proxy
ARG https_proxy
ARG all_proxy
ARG no_proxy
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG ALL_PROXY
ARG NO_PROXY

# 构建期 apt 源，留默认值即为官方源。
# 不走代理时可选大厂/高校镜像（mirrors.cloud.tencent.com / mirrors.aliyun.com /
# mirrors.tuna.tsinghua.edu.cn）；
# 走代理时保持默认，官方源即可。
ARG APT_MIRROR=deb.debian.org
# PyPI 源，默认官方。不走代理时可换成大厂/高校镜像：
# https://mirrors.cloud.tencent.com/pypi/simple
# https://mirrors.aliyun.com/pypi/simple
# https://pypi.tuna.tsinghua.edu.cn/simple
# 注意：uv.lock 里锁定了源地址，单设环境变量不生效，
# 换源时会在下面先按新源重新锁定一次（只改下载地址，版本不变）。
ARG PYPI_INDEX=https://pypi.org/simple
# Playwright 的 Chromium 官方 CDN。默认直连；国内不稳定时优先挂代理，
# 不建议引入第三方镜像源。
ARG PLAYWRIGHT_DOWNLOAD_HOST=

# uv 使用系统 Python；浏览器与数据都放在 /app 下，方便统一授权。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/app/.playwright \
    UV_DEFAULT_INDEX=${PYPI_INDEX} \
    TZ=Asia/Shanghai

WORKDIR /app

# 换源必须发生在第一次 apt-get update 之前。
# Debian 12+ 用 deb822 的 debian.sources，旧版本用 sources.list，两个都兼容。
RUN if [ "$APT_MIRROR" != "deb.debian.org" ]; then \
        f=/etc/apt/sources.list.d/debian.sources; \
        [ -f "$f" ] || f=/etc/apt/sources.list; \
        sed -i "s|http://deb.debian.org|https://$APT_MIRROR|g" "$f"; \
    fi

# 下载失败自动重试，避免一次抖动就让整层重来。
RUN printf 'Acquire::Retries "5";\n' > /etc/apt/apt.conf.d/99-retries

# apt 依赖列表和 deb 包放进 BuildKit 缓存：重复构建不会重新下载；
# 缓存不会进入镜像层，不需要再 rm -rf /var/lib/apt/lists。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git tzdata libatomic1 \
    && git config --system --add safe.directory /repo

# pip 不读 UV_DEFAULT_INDEX，换源要显式传 --index-url，否则第一步仍会访问官方 PyPI。
RUN python -m pip install --no-cache-dir --index-url "$PYPI_INDEX" uv

# 先只装依赖，源码变化时不需要重装整个依赖树。
COPY pyproject.toml uv.lock ./

# uv.lock 记录了每个包的源地址，--frozen 会照着锁文件下载，所以换源时必须先重新锁定。
# 注意 uv lock 是重新解析，不是单纯改写下载地址：正常情况版本与哈希不变
# （本次实测 59 个包完全一致），但镜像缺版本时可能解析出不同结果。
RUN if [ "$PYPI_INDEX" != "https://pypi.org/simple" ]; then \
        uv lock --default-index "$PYPI_INDEX" --no-cache; \
    fi
# 构建期不保留 uv 缓存：默认会写进 /root/.cache/uv 并留在镜像层里，白占 GB 级空间。
RUN uv sync --frozen --no-dev --no-install-project --no-cache

# Playwright 的 Chromium 及系统依赖只依赖 Python 依赖，不依赖源码；
# 放在 COPY src 之前，改源码不会让浏览器层失效。
# 拆成两层：apt 依赖和 Chromium 本体分开。
# 浏览器下载失败时重试只需重下浏览器，不用再跑一遍 apt；反之亦然。
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    /app/.venv/bin/python -m playwright install-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk

RUN if [ -n "$PLAYWRIGHT_DOWNLOAD_HOST" ]; then \
        export PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_DOWNLOAD_HOST"; \
    fi; \
    /app/.venv/bin/python -m playwright install chromium

COPY src ./src
# editable 安装：运行时可被 docker-compose 挂载的 ./src 覆盖，改源码后重启容器即可生效。
RUN uv sync --frozen --no-dev --no-cache

# 预装内置插件依赖；运行时仍会检查并补装新增插件的依赖。
RUN cat src/fxbot/plugins/*/requirements.txt > /tmp/plugin-requirements.txt \
    && uv pip install --no-cache --python /app/.venv/bin/python -r /tmp/plugin-requirements.txt \
    && rm -f /tmp/plugin-requirements.txt

RUN groupadd --gid 1000 fxbot \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin fxbot \
    && mkdir -p /app/data \
    && chown -R fxbot:fxbot /app

ENV HOME=/home/fxbot \
    PATH=/app/.venv/bin:$PATH

# 镜像默认不降权：容器要写挂载进来的仓库（容器内 #更新 需要 git pull），
# 而宿主机仓库通常由 root 或其他用户维护，硬编码 uid 1000 会导致属主冲突。
# 需要最小权限时由 docker compose 的 user: 指定（见 docker-compose.yml 的 FXBOT_UID）。
# 上面的 fxbot 用户仍然保留，方便 user: 1000:1000 的场景。

EXPOSE 8683

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8683/health | grep -q '"ok":true' || exit 1

# --host 覆盖 data/config/runtime/config.json 里的 127.0.0.1，容器内必须监听 0.0.0.0。
CMD ["/app/.venv/bin/python", "-m", "fxbot", "--host", "0.0.0.0", "--port", "8683"]
