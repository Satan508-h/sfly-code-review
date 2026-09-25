"""``GraphRunner`` —— 消费 ``review_bootstrap``，启动或唤醒一张图。

### 三种进入方式，一条判据

一条 bootstrap 到达时，数据库里的 run 只可能是三种状态之一：

| run.status | 图的 checkpoint | 做什么 |
|---|---|---|
| 终态（published / failed / skipped / publish_failed） | 随便 | **什么都不做**，直接 ack。重投是正常路径（GitHub 超时重投同一个 webhook） |
| ``queued`` | 没有 | 从头跑一张新图 |
| 其它（dispatched / waiting / aggregating） | 有 | 唤醒挂起的那张图 |

第三行里的 ``not snapshot.next`` 那一支要单独说：run 被标成 ``dispatched``
（``plan`` 已经把 deadline 写进库了）但 checkpoint 还没有 —— 说明进程崩在
「写完库、checkpoint 还没落盘」之间。这时**没有东西可以唤醒**，
正确做法是从头跑一张新图（plan 是幂等的，重派的任务会被 Worker 的
``exists_result`` 快路径吸收）。

### 幂等键撞车时改写 task_id

GitHub 重投同一个 webhook 时，第二次的 ``task_id`` 是新的，而幂等键
（``repo:pr:head_sha``）指向**已有的** run。这时图必须跟着已有的 run 走 ——
否则同一个 PR 会产出两份报告、两条评论，而且**两边都不会报错**。

改写发生在 ``bootstrap`` 上而不是「让图同时认两个 id」：
``thread_id``、``run_events.task_id``、``worker_results.task_id`` 全部来自
同一个字符串，让它们分叉就是在给每一个查询埋一个静默的漏读。

### 失败是有上界的

图抛异常时**不 ack**（消息留在 PEL，由 ``reclaim`` 重投 —— 和 Worker 那边
同一条路径）。但重投不是无限的：``attempt >= MAX_ATTEMPTS`` 时把 run 标成
``failed`` 并 ack。没有这一步的话，一条注定失败的 bootstrap 会
**永远在 PEL 里转圈**，而它在数据库里看起来只是「一个很久没动的 run」——
``due_runs`` 扫不到它（它是 ``dispatched``，扫得到，但扫描器也救不了它，
因为图一跑就崩）。上界让这件事有一个明确的终点。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from sfly_agent.state import ReviewState, initial_state
from sfly_bus.base import Lock, MessageHandle, RunStore, TaskQueue
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.graph import thread_config
from sfly_orchestrator.nodes.wait import WAKE_REASON
from sfly_shared.config import Settings, get_settings
from sfly_shared.contracts import BootstrapMessage, RunStatus
from sfly_shared.logging import bind_task, get_logger

log = get_logger(__name__)

#: run 的终态。到了这里就**没有任何东西需要被唤醒**了。
#:
#: ``publish_failed`` 也在里面：那是一次失败的**投递**，报告已经落库，
#: M7 之后由「重新发布」按钮处理，不该再让图跑一遍。
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.PUBLISHED,
        RunStatus.PUBLISH_FAILED,
        RunStatus.FAILED,
        RunStatus.SKIPPED,
    }
)

#: 唤醒选举锁的 TTL。见 :func:`wake_graph` —— 它只需要覆盖一次 ``ainvoke``，
#: 而那次调用里除了 ``wait`` 的屏障查询就是几条写库，正常在秒级。
WAKE_LOCK_TTL_MS = 60_000

#: 图失败之后、重投到来之前本进程的退避。见 ``GraphRunner.run``。
FAILURE_BACKOFF_S = 1.0


async def wake_graph(
    graph: CompiledStateGraph[ReviewState],
    task_id: str,
    *,
    lock: Lock | None = None,
) -> bool:
    """唤醒一个挂起的图。返回「是否真的推进了它」。

    两种挂起方式必须用两种恢复方式，判据是 checkpoint 里**有没有 interrupt**：

    * 有 interrupt（``wait`` 节点的正常暂停）→ ``Command(resume=...)``
    * 没有，但有 pending task（进程在某个节点执行到一半时崩了）→ 传 ``None``
      继续执行。给这种图传 ``Command(resume=...)`` 会得到一个
      "no matching interrupt" 的错误，而那个错误完全不指向真正的原因。

    ``lock`` 是**唤醒选举**：扫描器在每个副本里都跑，同一个 run 会被多个副本
    同时盯上；而 LangGraph 不阻止同一个 thread 被并发 invoke ——
    两个并发执行会各自跑一遍 ``aggregate``、各自发一次评论。

    锁只覆盖这一次调用（TTL 一分钟）。**它不是正确性机制**（``Lock`` 的协议
    文档里写了）：TTL 到了、或者 Redis 重启丢了键，选举就失效。真正的兜底是
    M7 的两道防重复评论闸（``github_comment_id`` + 正文里的隐藏标记）。
    锁在这里省的是**重复劳动**，而那正是它该干的活。
    """
    key = f"resume:{task_id}"
    if lock is not None and not await lock.acquire(key, WAKE_LOCK_TTL_MS):
        log.info("graph.wake_skipped", task_id=task_id, reason="另一个副本正在唤醒这个 run")
        return False

    try:
        config = thread_config(task_id)
        snapshot = await graph.aget_state(config)
        if not snapshot.next:
            # 跑完了，或者从来没开始过。两种都不该在这里处理 ——
            # 「从来没开始过」的恢复路径是 bootstrap 的 reclaim（见模块文档）。
            log.warning(
                "graph.wake_noop",
                task_id=task_id,
                hint="没有挂起的任务：图要么已经跑完，要么 checkpoints 里根本没有它",
            )
            return False

        if snapshot.interrupts:
            # ``Command[Any]`` 显式写出：``Command`` 的泛型参数是从 ``goto``
            # 推出来的，而这里只用 ``resume``，mypy 会推出 ``Command[Never]`` ——
            # 然后 ``ainvoke`` 的重载就一个都对不上。
            await graph.ainvoke(Command[Any](resume={"reason": WAKE_REASON}), config)
        else:
            await graph.ainvoke(None, config)
        return True
    finally:
        if lock is not None:
            await lock.release(key)


class GraphRunner:
    """一条消费 ``review_bootstrap`` 的常驻协程。"""

    def __init__(
        self,
        *,
        ctx: NodeContext,
        graph: CompiledStateGraph[ReviewState],
        settings: Settings | None = None,
    ) -> None:
        self._ctx = ctx
        self._graph = graph
        self._settings = settings or get_settings()
        self._store: RunStore = ctx.store
        self._queue: TaskQueue = ctx.queue

    async def run(self, stop: asyncio.Event) -> None:
        """一直消费到 ``stop`` 被 set（或协程被取消）。"""
        log.info("graph.consuming", stream="review_bootstrap", group="orchestrator-group")
        stream: AsyncIterator[tuple[MessageHandle, BootstrapMessage]] = self._queue.consume_bootstrap()
        async for handle, msg in stream:
            try:
                if not await self.handle(handle, msg):
                    await asyncio.sleep(FAILURE_BACKOFF_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                # ``handle`` 自己已经处理过一遍异常了；能到这里的只有
                # ``create_run`` 都没跑成的情况（数据库不可达）。不 ack，
                # 让消息留在 PEL 里等 reclaim —— 和 Worker 那边同一条路径。
                log.exception("graph.bootstrap_crashed", task_id=msg.task_id, attempt=handle.attempt)
                await asyncio.sleep(FAILURE_BACKOFF_S)

    async def handle(self, handle: MessageHandle, msg: BootstrapMessage) -> bool:
        """处理一条 bootstrap。返回**是否已 ack**（False = 留给 reclaim 重投）。"""
        run = await self._store.create_run(msg)
        task_id = run.task_id
        bind_task(task_id)
        if task_id != msg.task_id:
            # 幂等键撞车：跟着已有的 run 走。见模块文档。
            msg = msg.model_copy(update={"task_id": task_id})

        try:
            if run.status in TERMINAL_STATUSES:
                log.info(
                    "graph.bootstrap_ignored",
                    task_id=task_id,
                    status=run.status.value,
                    attempt=handle.attempt,
                    hint="run 已到终态，重投的 bootstrap 直接丢弃",
                )
            else:
                await self._advance(run.status, msg, task_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception(
                "graph.bootstrap_failed",
                task_id=task_id,
                status=run.status.value,
                attempt=handle.attempt,
            )
            if handle.attempt >= self._settings.max_attempts:
                # 见模块文档「失败是有上界的」。
                await self._store.set_status(task_id, RunStatus.FAILED)
                log.error(
                    "graph.run_given_up",
                    task_id=task_id,
                    attempts=handle.attempt,
                    hint="图连续失败到尝试上限，run 标记为 failed 并丢弃消息",
                )
            else:
                return False

        await handle.ack()
        return True

    async def _advance(self, status: RunStatus, msg: BootstrapMessage, task_id: str) -> None:
        """真正推进这个 run：跑一张新图，或者唤醒旧的那张。"""
        config = thread_config(task_id)
        snapshot = await self._graph.aget_state(config)
        if status is RunStatus.QUEUED or not snapshot.next:
            # 新 run，或者「库写了、checkpoint 没落」的那种半途崩溃。
            log.info("graph.starting", task_id=task_id, status=status.value, fresh=not snapshot.next)
            await self._graph.ainvoke(initial_state(msg), config)
            return
        log.info("graph.resuming", task_id=task_id, status=status.value)
        await wake_graph(self._graph, task_id, lock=self._ctx.lock)
