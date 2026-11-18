# syntax=docker/dockerfile:1

FROM python:3.12-slim

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

# uv 使用系统 Python；浏览器与数据都放在 /app 下，方便统一授权。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PLAYWRIGHT_BROWSERS_PATH=/app/.playwright \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git tzdata libatomic1 \
    && git config --system --add safe.directory /repo \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir uv

# 先只装依赖，源码变化时不需要重装整个依赖树。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Playwright 的 Chromium 及系统依赖只依赖 Python 依赖，不依赖源码；
# 放在 COPY src 之前，改源码不会让浏览器层失效。
RUN /app/.venv/bin/python -m playwright install --with-deps chromium \
    && apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*

COPY src ./src
# editable 安装：运行时可被 docker-compose 挂载的 ./src 覆盖，改源码后重启容器即可生效。
RUN uv sync --frozen --no-dev

# 预装内置插件依赖；运行时仍会检查并补装新增插件的依赖。
RUN cat src/fxbot/plugins/*/requirements.txt > /tmp/plugin-requirements.txt \
    && uv pip install --python /app/.venv/bin/python -r /tmp/plugin-requirements.txt \
    && rm -f /tmp/plugin-requirements.txt

RUN groupadd --gid 1000 fxbot \
    && useradd --uid 1000 --gid 1000 --create-home --shell /usr/sbin/nologin fxbot \
    && mkdir -p /app/data \
    && chown -R fxbot:fxbot /app

ENV HOME=/home/fxbot \
    PATH=/app/.venv/bin:$PATH

USER fxbot

EXPOSE 8683

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8683/health | grep -q '"ok":true' || exit 1

# --host 覆盖 data/config/runtime/config.json 里的 127.0.0.1，容器内必须监听 0.0.0.0。
CMD ["/app/.venv/bin/python", "-m", "fxbot", "--host", "0.0.0.0", "--port", "8683"]
