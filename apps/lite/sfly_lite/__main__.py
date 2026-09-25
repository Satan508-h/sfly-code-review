"""``python -m sfly_lite`` —— 精简模式：一个进程跑完整个系统（Render 用）。

这是「一套代码、两种拓扑」的证明。注意下面这行：

    runner = GraphRunner(bus, store, checkpointer)
    pool   = WorkerPool(bus, store, SPECS, ...)

``GraphRunner`` 和 ``WorkerPool`` 是**完整模式下 orchestrator 容器和三个
worker 容器用的同一个类**，一行都没有改。区别只有两个：

    QUEUE_BACKEND=memory   → InMemoryQueue 而非 RedisStreamsQueue
    LOCK_BACKEND=memory    → InMemoryLock 而非 RedisLock

而 ``sfly_bus/factory.py`` 是整个代码库里**唯一**读这两个变量的地方。
grep 一下应该只返回一个文件 —— 这是这个卖点能被验证的方式。

两个硬性约束：
  * ``workers=1`` —— 多个 uvicorn worker 会各自起一套 coordinator，
    进程间没有共享状态，屏障检查和唤醒选举都会乱掉。单进程 asyncio
    并发对演示来说绰绰有余。
  * ``PORT`` 从环境变量读 —— Render 动态分配端口，写死会 502。
"""

from __future__ import annotations

import uvicorn

from sfly_api.main import create_app
from sfly_shared.aio import run
from sfly_shared.config import get_settings
from sfly_shared.heartbeat import Heartbeat, shutdown_event
from sfly_shared.logging import get_logger, setup_logging

log = get_logger(__name__)


async def _main_async() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)

    # 这里同样不读 queue_backend / lock_backend —— 见的模块文档，
    # 只有 factory.py 允许读它们。要确认当前拓扑，看 factory 的 deps.opened 日志。
    log.info(
        "lite.starting",
        port=settings.port,
        mode=settings.mode,
        llm_provider=settings.llm_provider,
        enable_real_llm=settings.enable_real_llm,
        status="skeleton — M10 接入 GraphRunner / WorkerPool",
    )

    heartbeat = Heartbeat()
    stop = shutdown_event()
    await heartbeat.start()

    # ----------------------------------------------------------------- #
    # M10 在这里组装。所有对象都是完整模式下的同一批类。
    # ----------------------------------------------------------------- #
    #
    # from sfly_bus.factory import make_bus, make_lock, make_store
    # from sfly_agent.checkpoint import make_checkpointer
    # from sfly_orchestrator.coordinator import GraphRunner
    # from sfly_orchestrator.sweeper import sweep_loop
    # from sfly_workers.runner import WorkerPool
    # from sfly_workers.specs import SPECS
    #
    # bus    = make_bus(settings)          # InMemoryQueue
    # lock   = make_lock(settings)         # InMemoryLock
    # store  = make_store(settings)        # Postgres，两种模式都必须有
    # await store.migrate()
    #
    # cp     = make_checkpointer(settings)
    # await cp.setup()                     # 幂等；不调会在首次写入时报
    #                                      # "relation does not exist"
    #
    # runner = GraphRunner(bus, store, cp)
    # pool   = WorkerPool(bus, store, SPECS, concurrency=settings.worker_concurrency)
    #
    # await pool.start()
    # await runner.start()
    # tasks = [
    #     asyncio.create_task(runner.run(stop), name="graph-runner"),
    #     asyncio.create_task(sweep_loop(bus, store, runner, settings.sweeper_interval_s, stop),
    #                         name="sweeper"),
    # ]

    # ----------------------------------------------------------------- #
    # HTTP
    # ----------------------------------------------------------------- #
    # 刻意不用 uvicorn.run()：它在 Windows 上把循环工厂写死成
    # ProactorEventLoop（见 uvicorn/loops/asyncio.py），而本地开发要在
    # Windows 上跑，Proactor 上 psycopg 连不上数据库。交给
    # sfly_shared.aio.run 决定循环，这里只负责起服务器。
    config = uvicorn.Config(
        create_app(),
        host="0.0.0.0",  # noqa: S104
        port=settings.port,
        # 见模块文档：多 worker 会各自起一套协调逻辑，屏障查询和唤醒选举都乱
        workers=1,
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(config)

    try:
        # M10 时换成 asyncio.gather(server.serve(), *tasks)
        await server.serve()
    finally:
        stop.set()
        await heartbeat.stop()
        log.info("lite.stopped")


def main() -> None:
    # 注意这里是 asyncio.run 而不是 uvicorn.run：本函数自己驱动 Server.serve()
    # （见 _main_async），所以循环的选择归我们。Windows 默认的 ProactorEventLoop
    # 跑不了 psycopg 异步模式，sfly_shared.aio.run 会换成 Selector。
    run(_main_async())


if __name__ == "__main__":
    main()
