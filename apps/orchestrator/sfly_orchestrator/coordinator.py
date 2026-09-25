"""``coordinator`` —— 消费 ``review_results``，做屏障检查，闭合时唤醒图。

### 为什么它是一个协程而不是一个 LangGraph 节点

图在 ``wait`` 节点 ``interrupt()`` 之后就**退出了**，进程里没有它的执行栈。
「另一个 Worker 上报了」这件事发生在图已经不在场的时候，所以它必须由
一个**图之外的东西**来接住 —— 那就是这个协程。

这也是这套设计里最关键的一处分离：**图负责决策，协调协程负责搬运。**
协调协程不做任何判断（除了「屏障关了没有」这一个查询），它甚至不读结果的内容。

### 屏障查询读的是数据库，不是流

``completed_workers(task_id)`` 查 ``worker_results`` 表。结果是 Worker
**先写库、再 XADD** 的（约定 #1），所以协调协程被唤醒时结果一定已经在库里了。
反过来的顺序会有真实的竞态：XADD 先到，协调协程去查库查不到，屏障看起来
没闭合 —— 然后那条消息被 ack 掉，再也没人来叫醒这个 run。

### 它一条事件都不写

``worker.result`` / ``worker.failed`` 由 **Worker 自己**在写完结果时写
（见 ``sfly_workers.pool._record_result_event``）。这里曾经是写事件的地方，
而实测下来它会写出错误的顺序：协调协程和图是并发的两条路径，图判断屏障读的是
**数据库**而不是消息流 —— 所以「三条结果都在库里了、图已经 aggregate 完、
协调协程才开始消费第一条消息」是完全可能的，时间线上于是出现
``worker.result`` 排在 ``run.finished`` 后面。

那不是排序问题，是写事件的人站错了位置。把它挪回因果起点之后，
这个协程只剩一件事：**屏障检查和唤醒**。少一件事就少一类竞态。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from langgraph.graph.state import CompiledStateGraph

from sfly_agent.state import ReviewState
from sfly_bus.base import MessageHandle, RunStore, TaskQueue
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.runner import wake_graph
from sfly_shared.contracts import TERMINAL_STATUSES, WorkerResult
from sfly_shared.logging import bind_task, get_logger

log = get_logger(__name__)

#: 存储层故障之后取下一条之前的退避（和 Worker 那边同一个理由）：
#: 依赖挂掉时每条消息都会立刻失败，不退避就是一串说同一件事的异常日志。
FAILURE_BACKOFF_S = 1.0


class Coordinator:
    """一条消费 ``review_results`` 的常驻协程。

    结果流用 ``orchestrator-group`` 这个消费者组，**组内只有一个成员** ——
    所以同一时刻只有一个协调协程在处理一条结果。这也是为什么它的屏障检查
    可以很简单（不需要考虑并发写）：真正的并发在**唤醒**那一步，
    而那里由 ``wake_graph`` 的选举锁兜住。
    """

    def __init__(
        self,
        *,
        ctx: NodeContext,
        graph: CompiledStateGraph[ReviewState],
    ) -> None:
        self._ctx = ctx
        self._graph = graph
        self._store: RunStore = ctx.store
        self._queue: TaskQueue = ctx.queue

    async def run(self, stop: asyncio.Event) -> None:
        log.info("coordinator.consuming", stream="review_results", group="orchestrator-group")
        stream: AsyncIterator[tuple[MessageHandle, WorkerResult]] = self._queue.consume_results()
        async for handle, result in stream:
            try:
                await self.on_result(handle, result)
            except asyncio.CancelledError:
                raise
            except Exception:
                # **不 ack**：消息留在 PEL 里，被 reclaim 重投。屏障检查是幂等的，
                # 重投最坏的结果是多一条事件，比丢掉一次唤醒好得多
                # （丢掉唤醒 = run 挂到超时，而超时的报告是 degraded 的）。
                log.exception(
                    "coordinator.result_failed",
                    task_id=result.task_id,
                    worker_type=result.worker_type.value,
                    attempt=handle.attempt,
                    hint="没有 ack —— 消息留在 PEL，等依赖恢复后重投",
                )
                await asyncio.sleep(FAILURE_BACKOFF_S)

    async def on_result(self, handle: MessageHandle, result: WorkerResult) -> None:
        bind_task(result.task_id, result.worker_type.value)

        run = await self._store.get_run(result.task_id)
        if run is None or run.status in TERMINAL_STATUSES:
            # run 已经跑完了（或者被清理了）。结果本身早就落库了
            # （Worker 先写库再发消息），所以这里什么都不用做。
            log.info(
                "coordinator.result_after_finish",
                task_id=result.task_id,
                status=run.status.value if run else None,
            )
            await handle.ack()
            return

        done = await self._store.completed_workers(result.task_id)
        missing = [w for w in run.planned_workers if w not in done]
        if missing:
            log.info(
                "coordinator.waiting",
                task_id=result.task_id,
                done=[w.value for w in done],
                missing=[w.value for w in missing],
            )
            await handle.ack()
            return

        log.info("coordinator.barrier_closed", task_id=result.task_id, workers=[w.value for w in done])
        await wake_graph(self._graph, result.task_id, lock=self._ctx.lock)
        await handle.ack()
