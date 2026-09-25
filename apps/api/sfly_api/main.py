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
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from sfly_bus.factory import Dependencies, open_dependencies
from sfly_bus.postgres import migrate_on_startup
from sfly_shared.config import get_settings
from sfly_shared.heartbeat import Heartbeat
from sfly_shared.logging import get_logger, setup_logging

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

log = get_logger(__name__)

_started_at = time.time()
_heartbeat = Heartbeat()


def get_deps(request: Request) -> Dependencies | None:
    """取本进程的依赖句柄。由 ``lifespan`` 写入 ``app.state.deps``。

    做成 FastAPI 依赖（而不是路由里直接 ``request.app.state.deps``）是为了
    **给单测一个干净的替换点**：``app.dependency_overrides[get_deps] = ...``
    就能让健康接口的测试不需要真的连 Postgres / Redis。

    没有它的话，这个测试只有两条路：要么起 TestClient 时跑 lifespan 去连
    真实依赖（单测从此需要 Docker，违背「< 10 秒、无外部依赖」的分层约定），
    要么在测试里手改 ``app.state``（可行，但那是绕过接口而不是使用接口，
    重构时会静默失效）。
    """
    deps: Dependencies | None = getattr(request.app.state, "deps", None)
    return deps


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)
    # HTTP 服务本来就有探针，心跳是给统一排查用的
    await _heartbeat.start()

    # 依赖在这里开一次，整个进程共用 —— 每条请求新建一个连接池是
    # 「看着能跑、压一下就崩」的经典写法。
    app.state.deps = await open_dependencies(settings)

    # 建表。**每个进程都在启动时做一遍**（api / orchestrator / 三个 Worker），
    # 并发的部分由 pg_advisory_xact_lock 串行化 —— 完整模式下五个容器同时
    # 启动是常态，不是边角情况。
    #
    # 放在这里而不是 open_dependencies 里面：那一步的语义是「把依赖装起来」，
    # 在它里面做 DDL 会让它带上副作用，而它明确允许数据库不可达
    # （连不上时进程照样起来，见 PostgresPool.open）。
    # migrate_on_startup 的处置策略：连不上 → 记一条 error 继续；漂移 → 抛出。
    await migrate_on_startup(app.state.deps.store)

    log.info("api.ready", mode=settings.mode, port=settings.port)
    try:
        yield
    finally:
        await app.state.deps.close()
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
        那只会让故障扩大。依赖状态在 ``/api/health`` 里看。

        这个刻意的不对称值得解释，因为它看起来像偷懒：

        * ``/healthz`` 决定 **Docker 要不要重启这个容器**。带着
          ``restart: unless-stopped`` 的容器如果因为「数据库连不上」被判死，
          就会进入无限重启循环 —— 数据库恢复后它还在退避等待，而且
          ``docker compose logs`` 里全是重复的启动日志，真正的错误被冲掉了。
        * ``/api/health`` 决定 **要不要把流量打过来**。依赖挂了它必须说 ``ok: false``。

        两者的失败代价不同，所以判据必须不同。合成一个接口就必然二选一：
        要么在数据库抖动时重启一堆无辜的容器，要么让负载均衡把流量送进一个
        干不了活的服务。
        """
        return JSONResponse(
            {"ok": True, "service": "sfly-api", "uptime_s": round(time.time() - _started_at, 1)}
        )

    @app.get("/api/health")
    async def health(deps: Annotated[Dependencies | None, Depends(get_deps)]) -> JSONResponse:
        """就绪探针：依赖的真实连通状态。

        依赖不可达时返回 **503**，让 ``docker compose`` / ``tasks.py health`` /
        任何探针都能靠 HTTP 状态码判断，而不是必须解析 JSON 体。

        ``ok`` 的语义是「这套部署现在能干活吗」——已经挂了的依赖不会让
        这个接口本身 500，因为调用方正是为了知道「坏没坏」才来问的。
        """
        s = get_settings()
        if deps is None:
            # 只有在 lifespan 没跑起来时才会到这里（正常由 uvicorn 保证）。
            # 明确报 503 而不是让 AttributeError 变成 500 ——
            # 一条能读的错误比一个堆栈有用。
            return JSONResponse(
                {"ok": False, "service": "sfly-api", "checks": {}, "detail": "依赖未初始化"},
                status_code=503,
            )
        report = await deps.probe()
        payload: dict[str, Any] = {
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
            **report.as_dict(),
        }
        return JSONResponse(payload, status_code=200 if report.ok else 503)

    # --------------------------------------------------------------------- #
    # 业务路由（M6 实现）
    # --------------------------------------------------------------------- #
    # from sfly_api.routes import events, runs, webhook
    # app.include_router(webhook.router, prefix="/api")
    # app.include_router(runs.router,    prefix="/api")
    # app.include_router(events.router,  prefix="/api")

    return app


app = create_app()
