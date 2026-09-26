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

**它有两种宿主形态**（``create_app`` 的 ``deps`` 参数决定）：

* **自己当家**（完整模式，默认）：lifespan 开依赖、建表、管心跳，退出时关掉。
* **做客**（精简模式）：宿主 ``sfly_lite`` 已经开好了这一切，并且**必须**是
  同一份 —— 那个 ``InMemoryQueue`` 得同时被 API 和 ``GraphRunner`` 看见。
  各开一套的后果见 ``create_app`` 的文档，它不会报错。

路径约定：业务接口全部挂在 ``/api`` 下；``/healthz`` 留在根路径给容器探针。
nginx（完整模式）做直通代理，所以本地和线上用的是同一套 URL。
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# get_deps 住在 deps.py（不是这里）—— 路由要 Depends 它，而它们由本模块 import，
# 放在这里就成了循环导入。``as get_deps`` 是**显式再导出**（写成一个名字
# 只是为了绕开 mypy 的 no_implicit_reexport），单测一直是
# ``from sfly_api.main import get_deps``，那条路径不该因为搬家而断。
from sfly_api.deps import get_deps as get_deps
from sfly_api.routes import events, runs, webhook
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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if getattr(app.state, "host_deps", False):
        # **宿主模式**（精简模式）：这个 app 跑在别的进程里 —— 依赖、心跳、
        # 日志、建表全部归宿主。这里一件事都不做。
        #
        # 尤其是**不能 close 依赖**：那个队列还活在同一个事件循环的另外几条
        # 协程手里（GraphRunner / WorkerPool）。关掉它，图会在下一次派发时
        # 报一个和「谁关的」毫无关系的错。
        #
        # 这段分支必须走在这个函数的**最前面**：下面每一行都是「自己拥有一套
        # 进程级资源」的假设，包括那个模块级的 ``_heartbeat`` 单例。
        log.info("api.ready", mode=get_settings().mode, deps="injected")
        yield
        return

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


def create_app(*, deps: Dependencies | None = None) -> FastAPI:
    """建网关 app。

    ``deps`` 留给**精简模式**：那个进程里 API 和编排器在同一个事件循环上，
    它们必须看见**同一个队列对象**。让 lifespan 各开一套的后果不是报错，
    而是 webhook 返回 202、然后什么都不会发生 —— 消息投进了 API 自己那个
    ``InMemoryQueue``，而 ``GraphRunner`` 在另一个上游荡。两个队列都是好的，
    各自也都「工作正常」，所以没有任何一层会报警。

    完整模式（``deps=None``，也是默认）下生命周期归 lifespan 自己，
    和 M0 以来的行为一模一样。
    """
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

    if deps is not None:
        # 宿主模式：依赖接过来，并且**声明我们不是它的主人**。lifespan 靠这个
        # 标记决定什么都不做（见那里）。写在 create_app 里而不是 lifespan 里，
        # 是因为「依赖从哪来」是构造期的事实，不是运行期的判断。
        app.state.deps = deps
        app.state.host_deps = True

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
                # **只回显配没配，不回显密钥本身。** webhook 不验签这件事必须
                # 在一个能看见的地方 —— 否则它就是「本地一直好好的，上线第一天
                # 被人刷爆了额度」那种发现问题的方式。
                "webhook_secret": "configured" if s.github_webhook_secret else "missing",
            },
            **report.as_dict(),
        }
        return JSONResponse(payload, status_code=200 if report.ok else 503)

    # --------------------------------------------------------------------- #
    # 业务路由
    # --------------------------------------------------------------------- #
    # 全部挂在 /api 下：nginx 做的是直通代理（不改路径），所以本地和线上
    # 用的是同一套 URL，前端只需要换 VITE_API_BASE 的值。
    app.include_router(webhook.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(events.router, prefix="/api")

    return app


app = create_app()
