"""``python -m sfly_lite`` —— 精简模式：一个进程跑完整个系统（Render 用）。

整个项目的中心论点在这里被压缩成一段可读的装配代码。看下面这几行：

    GraphRunner(ctx=ctx, graph=graph)      # 同 orchestrator 容器
    Coordinator(ctx=ctx, graph=graph)      # 同 orchestrator 容器
    Sweeper(ctx=ctx, graph=graph, ...)     # 同 orchestrator 容器
    WorkerPool(queue=..., store=...)       # 同三个 worker 容器
    create_app(deps=deps)                  # 同 api 容器

**五种角色，五个同一个类，一行都没改。** 换掉的只有两个地方：

    QUEUE_BACKEND=memory   → InMemoryQueue 而非 RedisStreamsQueue
    LOCK_BACKEND=memory    → InMemoryLock  而非 RedisLock

而 ``sfly_bus/factory.py`` 是整个代码库里**唯一**拿这两个值做判断的地方
（``tests/unit/bus/test_protocols.py`` 用 AST 扫着这条）。本文件连读都不读它们 ——
它只是调 ``open_dependencies()``，然后看 ``deps.opened`` 那条日志确认拓扑。

### 三个硬性约束

* **``create_app(deps=deps)`` 这个参数不能省。** 省掉的话 lifespan 会自己
  ``open_dependencies()`` 再开一套 —— 于是 API 手里是**另一个** ``InMemoryQueue``，
  webhook 投进那一条、``GraphRunner`` 在另一条上等。两边都「工作正常」，
  症状只有「返回 202 之后什么都没有」。这是本项目里最难查的一类故障的形状：
  没有异常、没有日志、只是永远不发生。
* **``workers=1``。** 多个 uvicorn worker 会各自起一套协调逻辑，而
  ``InMemoryQueue`` 和唤醒选举都只在单进程内成立。单进程 asyncio 并发
  对演示来说绰绰有余（一条审查的瓶颈是 LLM 的往返，不是 CPU）。
* **``PORT`` 从环境变量读。** Render 动态分配端口，写死会得到 502。

### 它和完整模式的差别只有这些

Redis 不存在（不是连不上，是不需要 —— 健康页上那条 ``redis`` 会报 skipped），
不能 ``--scale``（没有第二个进程可以加入消费者组），以及所有状态都在一个
事件循环里 —— 所以**这个进程死掉就是整个系统死掉**，恢复靠 Render 重启容器，
而不是靠 ``deadline_at`` 那条 SQL。断点恢复的演示要在完整模式下做。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable

import uvicorn
from langgraph.graph.state import CompiledStateGraph

from sfly_agent.github import GitHubClient
from sfly_agent.state import ReviewState
from sfly_api.main import create_app
from sfly_bus.factory import Dependencies, open_dependencies
from sfly_bus.postgres import migrate_on_startup
from sfly_orchestrator.checkpointer import open_checkpointer, setup_on_startup
from sfly_orchestrator.context import NodeContext, build_github_client
from sfly_orchestrator.coordinator import Coordinator
from sfly_orchestrator.graph import build_graph
from sfly_orchestrator.runner import GraphRunner
from sfly_orchestrator.sweeper import Sweeper
from sfly_shared.aio import run
from sfly_shared.config import Settings, get_settings
from sfly_shared.heartbeat import run_service
from sfly_shared.logging import get_logger
from sfly_workers.pool import WorkerPool

log = get_logger(__name__)

#: 停机时给 uvicorn 收流的上限。Render 发 SIGTERM 之后大约 30 秒才 SIGKILL，
#: 所以要明显小于 30 —— 后面还有几步清理要走，留出余量。
HTTP_DRAIN_S = 10.0


async def _body(stop: asyncio.Event) -> None:
    """精简模式的全部内容：依赖 → 图 → 五条长驻协程。

    ``run_service`` 负责日志、心跳和优雅停机（和另外四个 app 共用同一个骨架），
    依赖的生命周期归这里 —— **谁开的谁关**，因为下面传给 API 的就是这一份。
    """
    settings = get_settings()
    github = build_github_client(settings)
    deps = await open_dependencies(settings)
    try:
        # 一个进程一个 ctx：图和四条协程共用它，于是只有一个 GitHub 客户端、
        # 一个连接池。各建各的会留下一个永远不会被关掉的。
        ctx = _context(deps, github=github)

        # 建表（幂等）。完整模式下五个容器会同时做这件事，这里只有我们一个 ——
        # 但仍然走同一条代码路径：给精简模式开一条「专用」的建表路径，
        # 两条路会慢慢分叉，而分叉只在空库上暴露。
        await migrate_on_startup(deps.store)

        # checkpointer 有自己的连接池（autocommit，见 checkpointer.py），
        # 而且它不能复用仓储那个 —— 理由在那个模块里写了一整段。
        async with open_checkpointer(settings.database_url) as saver:
            await setup_on_startup(saver)
            graph = build_graph(ctx, checkpointer=saver)
            await _serve(stop, settings, deps, ctx, graph)
    finally:
        if github is not None:
            await github.aclose()
        await deps.close()


async def _serve(
    stop: asyncio.Event,
    settings: Settings,
    deps: Dependencies,
    ctx: NodeContext,
    graph: CompiledStateGraph[ReviewState],
) -> None:
    """一个事件循环上并排跑五条长驻协程 —— 这就是「精简模式」的全部。"""
    # 刻意不用 uvicorn.run()：它在 Windows 上把循环工厂写死成 ProactorEventLoop
    # （见 uvicorn/loops/asyncio.py），而 psycopg 的异步模式在 Proactor 上连不上
    # 数据库。循环的选择已经由 sfly_shared.aio.run 做完了，这里只负责驱动 Server。
    server = uvicorn.Server(
        uvicorn.Config(
            # **传 deps**：见模块文档第一条。API 和编排器必须看见同一个队列。
            create_app(deps=deps),
            host="0.0.0.0",  # noqa: S104 - 容器里必须绑全网卡，Render 从外面打进来
            port=settings.port,
            # 见模块文档：多 worker 会各自起一套状态。Render 免费档也没有
            # 让这件事有意义的内存。
            workers=1,
            log_config=None,
            access_log=False,
        )
    )

    # HTTP 单独拿着：停机时它的处置和另外四条**不一样**（见下面的 finally）
    http = asyncio.create_task(server.serve(), name="http")
    tasks: list[asyncio.Task[None]] = [
        asyncio.create_task(GraphRunner(ctx=ctx, graph=graph).run(stop), name="graph-runner"),
        asyncio.create_task(Coordinator(ctx=ctx, graph=graph).run(stop), name="coordinator"),
        asyncio.create_task(
            Sweeper(ctx=ctx, graph=graph, interval_s=settings.sweeper_interval_s).run(stop),
            name="sweeper",
        ),
        asyncio.create_task(
            WorkerPool(queue=ctx.queue, store=deps.store, settings=settings).run(stop),
            name="worker-pool",
        ),
        http,
    ]

    # 等「第一条结束」，而不是像完整模式那样只等 ``stop``。这五条都是
    # ``while True`` 的形态，正常路径下谁都不该先返回 —— 任何一条返回都意味着
    # 这个进程已经不是完整的系统了（HTTP 没了 → 前端全 502；协调协程没了 →
    # run 永远等不到唤醒）。**带着一半功能继续跑**比干脆退出难查得多，
    # 因为 Render 那边看着还是健康的。退出 → 容器被重启 → 干净的进程。
    watcher = asyncio.create_task(stop.wait(), name="stop")
    try:
        done, _ = await asyncio.wait({watcher, *tasks}, return_when=asyncio.FIRST_COMPLETED)
        finished = next(iter(done))
        if finished is not watcher:
            log.error(
                "lite.task_exited",
                task=finished.get_name(),
                error=_task_error(finished),
                hint="一条长驻协程结束了，整个进程退出 —— 交给容器重启",
            )
    finally:
        # HTTP **先收流再退**：``should_exit`` 让 uvicorn 停止接受新连接、
        # 把手上的请求做完。直接 cancel 会打断正在处理的请求，而 webhook
        # 被打断的后果不是「少一条日志」—— 是 GitHub 收到 5xx 然后重投，
        # 但那一次投递其实已经处理了一半。
        server.should_exit = True
        # 另外四条不等：它们是幂等的长驻循环（屏障检查、扫描、消费），
        # 随时掐掉都不会留下半个状态 —— 掐早了重新跑一遍就是。
        for task in (*tasks, watcher):
            if task is not http:
                task.cancel()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(http, timeout=HTTP_DRAIN_S)
        await asyncio.gather(*tasks, watcher, return_exceptions=True)
        log.info("lite.stopped")


def _task_error(task: asyncio.Task[object]) -> str | None:
    """任务的异常文本。**正常返回和取消都不算错误** —— 前者是 uvicorn 收到
    ``should_exit``，后者就是我们自己在 ``finally`` 里干的。"""
    if task.cancelled():
        return None
    exc = task.exception()
    return f"{type(exc).__name__}: {exc}" if exc is not None else None


def _context(deps: Dependencies, *, github: GitHubClient | None) -> NodeContext:
    """收窄 ``Dependencies.queue``。

    ``NodeContext`` 声明队列非空（没有它图就跑不动，见那个类的文档），而
    ``Dependencies.queue`` 在类型上可空。在所有节点里各判一次空会得到六种
    不同的处理方式，所以在装配这一层收窄一次。
    """
    if deps.queue is None:  # pragma: no cover - factory 保证不会
        raise RuntimeError("精简模式需要一个队列，但 factory 没有装配它")
    return NodeContext(store=deps.store, queue=deps.queue, lock=deps.lock, github=github)


def main() -> None:
    # 用 sfly_shared.aio.run 而不是 asyncio.run：Windows 默认的 ProactorEventLoop
    # 跑不了 psycopg 的异步模式（见该模块）。run_service 是四个 app 共用的骨架 ——
    # 日志、心跳、SIGTERM 都归它。
    body: Callable[[asyncio.Event], Awaitable[None]] = _body
    run(run_service("lite", body))


if __name__ == "__main__":
    main()
