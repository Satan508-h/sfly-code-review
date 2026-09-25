# 单文件多目标的 Python 服务镜像。
#
# 五个服务（api / orchestrator / workers / lite）共用这一个 Dockerfile，
# 只靠两个 build arg 区分：
#   APP         —— 发行包名，喂给 `uv sync --package`，决定装哪些依赖
#   APP_MODULE  —— 模块名，决定 CMD 跑什么
#
# 为什么不分五个 Dockerfile：五个文件 95% 内容是重复的，改一处要同步五遍。
# uv workspace 让「装哪个服务的依赖闭包」变成一个参数，那就用一个参数。
#
# 注意这里**故意没有 `# syntax=docker/dockerfile:1.x` 指令**：
# 那一行会让 BuildKit 先去 Docker Hub 拉一个前端镜像，在没有外网/代理的机器上
# 会让整个构建在第一步就失败，而报错信息指向的是 Dockerfile 第一行，很难定位。
# `RUN --mount=type=cache` 在现代 Docker 的内置前端里本来就支持，不需要它。

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # 用基础镜像自带的 3.12，不要 uv 去下载一个
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv

# uv 从官方镜像取，版本固定 —— 不用 latest，否则某天构建会突然因为 uv 升级而失败
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

WORKDIR /app

# --------------------------------------------------------------------------- #
# 第一层：只装第三方依赖。
# 这一层只依赖各 pyproject.toml + uv.lock，改业务代码不会让它失效。
# 配合 BuildKit 的 cache mount，重复构建基本是秒级。
# --------------------------------------------------------------------------- #

COPY pyproject.toml uv.lock ./
COPY apps/api/pyproject.toml             apps/api/
COPY apps/orchestrator/pyproject.toml    apps/orchestrator/
COPY apps/workers/pyproject.toml         apps/workers/
COPY apps/lite/pyproject.toml            apps/lite/
COPY packages/shared/pyproject.toml      packages/shared/
COPY packages/bus/pyproject.toml         packages/bus/
COPY packages/agent-core/pyproject.toml  packages/agent-core/

ARG APP=sfly-api

# --no-install-workspace：只装第三方依赖，不装 workspace 成员自己 ——
# 因为此刻成员目录里还没有源码。
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package "${APP}"

# --------------------------------------------------------------------------- #
# 第二层：拷源码，装 workspace 成员。只有业务代码变化时这一层才失效。
# --------------------------------------------------------------------------- #

COPY packages ./packages
COPY apps ./apps

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package "${APP}"

ENV PATH="/app/.venv/bin:$PATH" \
    VIRTUAL_ENV="/app/.venv"

# --------------------------------------------------------------------------- #
# 运行时
# --------------------------------------------------------------------------- #

ARG APP_MODULE=sfly_api

# 非 root 运行。uid 固定，方便挂载卷时的权限排查。
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin sfly \
 && mkdir -p /tmp \
 && chown -R sfly:sfly /app
USER sfly

# 存活探针基于**心跳文件**而不是进程存在性。
# 一个死循环卡住的 Worker 进程照样存在，但心跳会停 —— 这才是我们想探测的故障。
# 各服务在主循环里定期 touch 这个文件。
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,time; \
p='/tmp/sfly-heartbeat'; \
sys.exit(1) if not os.path.exists(p) else None; \
sys.exit(0 if time.time()-os.path.getmtime(p) < 60 else 1)"

ENV APP_MODULE=${APP_MODULE}
CMD ["sh", "-c", "exec python -m \"$APP_MODULE\""]
