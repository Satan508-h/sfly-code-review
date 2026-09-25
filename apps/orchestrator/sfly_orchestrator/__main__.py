"""``python -m sfly_orchestrator`` —— 容器 2：LangGraph 状态机 + 协调协程。

Step 0 阶段这里只验证拓扑与心跳；M5 会把三件事接进来：

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

from sfly_shared.config import get_settings
from sfly_shared.heartbeat import run_service
from sfly_shared.logging import get_logger

log = get_logger(__name__)

NODE_SEQUENCE = ("ingest → plan → dispatch → wait(interrupt) → aggregate → finalize → publish",)


async def _body(stop: asyncio.Event) -> None:
    settings = get_settings()

    log.info(
        "orchestrator.skeleton",
        wait_strategy=settings.wait_strategy,
        sweeper_interval_s=settings.sweeper_interval_s,
        run_deadline_s=settings.run_deadline_s,
        conflict_resolver=settings.conflict_resolver,
        pipeline=NODE_SEQUENCE[0],
        status="skeleton — M5 接入图与协调协程",
    )

    # M5 在这里起三个长驻任务：
    #   runner    = GraphRunner(bus, store, checkpointer)
    #   consumer  = coordinator(bus, store, runner)      # 消费 review_results
    #   sweeper   = sweep_loop(bus, store, runner, settings.sweeper_interval_s)
    #
    # await asyncio.gather(runner.run(stop), consumer.run(stop), sweeper.run(stop))

    # 骨架阶段就安静地等停机信号，让心跳维持容器健康
    await stop.wait()


def main() -> None:
    asyncio.run(run_service("orchestrator", _body))


if __name__ == "__main__":
    main()
