"""FastAPI 网关 —— 容器 1。

职责边界（刻意的窄）：
  * 校验 GitHub webhook 的 HMAC 签名
  * 按 ``X-GitHub-Delivery`` 去重（GitHub 超时后会重投同一个 delivery）
  * 写一条 ``BootstrapMessage`` 到 ``review_bootstrap`` 流，返回 202
  * 读 run 状态与报告
  * 推 SSE 进度

**它不做的事**：不做文件风险排序、不做规则检索、不直接写 ``review_tasks``。
那些是编排层 ``plan`` 节点的职责。API 保持又薄又快，webhook 路径上只有
一次数据库写和一次 XADD。

路径约定：业务接口全部挂在 ``/api`` 下；``/healthz`` 留在根路径给容器探针。
nginx（完整模式）做直通代理，所以本地和线上用的是同一套 URL。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sfly_shared.config import get_settings
from sfly_shared.heartbeat import Heartbeat
from sfly_shared.logging import get_logger, setup_logging

log = get_logger(__name__)

_started_at = time.time()
_heartbeat = Heartbeat()


@asynccontextmanager
async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    # HTTP 服务本来就有探针，心跳是给统一排查用的
    await _heartbeat.start()
    log.info("api.ready", mode=settings.mode, port=settings.port)
    try:
        yield
    finally:
        await _heartbeat.stop()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="sfly — 多 Agent 代码审查",
        version="0.1.0",
        lifespan=lifespan,
        # 文档路径也挪到 /api 下，保持前缀统一
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )

    # 完整模式下前端由 nginx 同源代理，用不上 CORS。
    # 精简模式下前端在 Vercel、后端在 Render，**必须**显式列出来，
    # 否则浏览器的 EventSource 会被拦。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,  # 不用 cookie，所以不需要 True（也不该开）
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        # 前端 SSE 重连时要读 Last-Event-ID 的回显
        expose_headers=["X-Request-Id"],
    )

    # --------------------------------------------------------------------- #
    # 探针
    # --------------------------------------------------------------------- #

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        """存活探针。**故意不去探依赖** —— 依赖挂了不该让容器被重启，
        那只会让故障扩大。依赖状态在 ``/api/health`` 里看。"""
        return JSONResponse(
            {"ok": True, "service": "sfly-api", "uptime_s": round(time.time() - _started_at, 1)}
        )

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        """就绪探针 + 依赖状态。M0 会在这里加上 Postgres / Redis 的真实探测。"""
        s = get_settings()
        return {
            "ok": True,
            "service": "sfly-api",
            "version": "0.1.0",
            "mode": s.mode,
            "uptime_s": round(time.time() - _started_at, 1),
            "config": {
                "queue_backend": s.queue_backend,
                "lock_backend": s.lock_backend,
                "llm_provider": s.llm_provider,
                "wait_strategy": s.wait_strategy,
                "conflict_resolver": s.conflict_resolver,
            },
            "checks": {},  # M0 填充
        }

    # --------------------------------------------------------------------- #
    # 业务路由（M6 实现）
    # --------------------------------------------------------------------- #
    # from sfly_api.routes import events, runs, webhook
    # app.include_router(webhook.router, prefix="/api")
    # app.include_router(runs.router,    prefix="/api")
    # app.include_router(events.router,  prefix="/api")

    return app


app = create_app()
