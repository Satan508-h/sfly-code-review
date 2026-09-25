"""``python -m sfly_orchestrator`` —— 容器 2：LangGraph 状态机 + 协调协程。

M4 起这里会打开依赖并在启动时建表（幂等，和另外四个容器并发也不冲突），
但**还没有消费任何东西**。M5 把三件事接进来：

  1. **GraphRunner** —— 消费 ``review_bootstrap``，用 ``thread_id=task_id``
     启动图。图在 ``wait`` 节点 ``interrupt()`` 后退出，进程不再持有任何
     该 run 的状态。
  2. **coordinator 协程** —— 消费 ``review_results``，落库后做屏障检查
     （``RunStore.completed_workers``），闭合时用 ``Command(resume=...)``
     唤醒对应的图。唤醒前抢 ``SETNX resume:{task_id}`` 防止多副本重复唤醒。
  3. **sweeper 协程** —— 每 ``SWEEPER_INTERVAL_S`` 秒扫 ``due_runs()``，
     把过了 ``deadline_at`` 还停在 dispatched/waiting 的 run 捞出来唤醒。

第 3 条是整个断点恢复故事成立的地方：**图暂停期间本容器崩了，恢复靠的是
一条 SQL 查询，不是内存里的定时器。**
"""

from __future__ import annotations

import asyncio

from sfly_bus.factory import open_dependencies
from sfly_bus.postgres import migrate_on_startup
from sfly_shared.aio import run
from sfly_shared.config import get_settings
from sfly_shared.heartbeat import run_service
from sfly_shared.logging import get_logger

log = get_logger(__name__)

NODE_SEQUENCE = ("ingest → plan → dispatch → wait(interrupt) → aggregate → finalize → publish",)


async def _body(stop: asyncio.Event) -> None:
    settings = get_settings()

    deps = await open_dependencies(settings)
    try:
        # 建表（幂等）。五个容器同时启动时它们会一起走到这里 ——
        # 串行化靠 pg_advisory_xact_lock，见 sfly_bus/migrations/。
        await migrate_on_startup(deps.store)

        log.info(
            "orchestrator.skeleton",
            wait_strategy=settings.wait_strategy,
            sweeper_interval_s=settings.sweeper_interval_s,
            run_deadline_s=settings.run_deadline_s,
            conflict_resolver=settings.conflict_resolver,
            pipeline=NODE_SEQUENCE[0],
            status="skeleton — M5 接入图与协调协程",
        )

        # M5 在这里起三个长驻任务，用的就是上面那两个句柄：
        #   runner    = GraphRunner(deps.queue, deps.store, checkpointer)
        #   consumer  = coordinator(bus, store, runner)      # 消费 review_results
        #   sweeper   = sweep_loop(bus, store, runner, settings.sweeper_interval_s)
        #
        # await asyncio.gather(runner.run(stop), consumer.run(stop), sweeper.run(stop))

        # 骨架阶段就安静地等停机信号，让心跳维持容器健康
        await stop.wait()
    finally:
        # 关在 finally 里：停机信号、异常、取消三条路径都要走到 ——
        # 漏掉的话容器停止时会留下没关的连接，而 Postgres 侧要等到
        # TCP 超时才发现（表现为重启后的第一波查询变慢）。
        await deps.close()


def main() -> None:
    # 用 sfly_shared.aio.run 而不是 asyncio.run：Windows 默认的 ProactorEventLoop
    # 跑不了 psycopg 的异步模式。见 packages/shared/sfly_shared/aio.py。
    run(run_service("orchestrator", _body))


if __name__ == "__main__":
    main()
