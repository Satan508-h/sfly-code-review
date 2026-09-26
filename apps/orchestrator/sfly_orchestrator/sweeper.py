"""``sweeper`` —— 超时扫描器。**断点恢复故事唯一成立的地方。**

每 ``SWEEPER_INTERVAL_S`` 秒跑一次：

    SELECT * FROM review_runs
     WHERE status IN ('dispatched','waiting') AND deadline_at <= now()

捞出来的 run 全部唤醒。**这条查询就是全部的恢复逻辑** —— 没有内存里的定时器、
没有「谁还记得有个 run 在等」，只有一行带 ``deadline_at`` 的记录。

### 它同时扮演两个角色

1. **超时兜底**：Worker 卡住了、永远不上报时，是它把图叫醒，
   让 ``wait`` 节点走超时分支给掉队者补发 failed 结果（屏障因此闭合）。
2. **崩溃恢复**：图挂起之后 orchestrator 死了 —— 重启后的进程对之前那些 run
   一无所知，而这条查询会把它们全部找回来。

两个角色用同一段代码，因为它们要的是同一件事：「这个 run 该往前走了」。
分开写会得到两个机制去救同一个 run，而它们之间没有任何协调。

### 为什么 ``queued`` 不在查询里

一个还没被编排器消费的 bootstrap，它的状态活在**队列里**（PEL）。
那种 run 由队列层的 ``reclaim`` 负责恢复 —— 扫描器那一侧没有「bootstrap
消息还在不在」的信息，它只能盲目唤醒，唤醒一个**没有 checkpoint 的图**。
``due_runs`` 的文档里写着同一件事。

### 为什么它不会和协调协程打架

两边都可能在同一时刻唤醒同一个 run（屏障刚闭合、deadline 刚好到）。
``wake_graph`` 的选举锁（``SETNX resume:{task_id}``）让其中一个胜出。
**就算锁失效了，最坏的后果是图的 ``aggregate`` 跑两遍** ——
它是幂等的（``save_report`` 是 upsert），代价是重复劳动，不是错数据。
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime

from langgraph.graph.state import CompiledStateGraph

from sfly_agent.state import ReviewState
from sfly_bus.base import RunStore
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.runner import wake_graph
from sfly_shared.logging import get_logger

log = get_logger(__name__)


class Sweeper:
    """周期性地把过期的 run 叫醒。**每个副本都跑它** —— 多跑几个是安全的。"""

    def __init__(
        self,
        *,
        ctx: NodeContext,
        graph: CompiledStateGraph[ReviewState],
        interval_s: float,
    ) -> None:
        self._ctx = ctx
        self._graph = graph
        self._interval_s = interval_s
        self._store: RunStore = ctx.store

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._sleep(stop)
            if stop.is_set():
                return
            try:
                await self.sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # 扫描失败不该让协程退出：它下一轮还会再试。数据库恢复之后
                # 那些 run 会被下一轮捞出来 —— 这正是「恢复靠一条 SQL」的意思。
                log.exception("sweeper.tick_failed", interval_s=self._interval_s)

    async def _sleep(self, stop: asyncio.Event) -> None:
        """睡一个周期，或者被停机信号提前叫醒。

        用 ``wait_for(stop.wait(), timeout)`` 而不是 ``asyncio.sleep``：
        后者会让停机多等最多一整个周期，而 Docker 只给 10 秒就 SIGKILL。
        """
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=self._interval_s)

    async def sweep_once(self) -> list[str]:
        """扫一轮，返回被唤醒的 ``task_id`` 列表。"""
        # **先回收，再扫描。**
        #
        # 这是 ``queued`` 那个状态**唯一**的恢复路径，而它以前是断的：
        # ``reclaim`` 只有 Worker 池在调（回收 ``review_tasks``），
        # 于是「一条消费失败的 bootstrap 留在 PEL 里」没有任何东西来捞 ——
        # 而超时扫描器够不着它（``due_runs`` 的判据是 ``deadline_at``，
        # 而 ``queued`` 的 run 还没有 deadline，那一行是 ``plan`` 写的）。
        # 症状是**这个 run 再也不动了**：没有报错、没有日志、UI 上一直「排队中」。
        #
        # 两边实现的 ``_target_groups(None)`` 早就写好了要覆盖
        # ``review_bootstrap``（那段文档就在讲这件事），契约测试也断言了 ——
        # 缺的只是这个调用者。M10 把 ``plan`` 的拉代码接上之后它才真的会发作：
        # 在那之前 ``plan`` 几乎不抛异常，这条路径走不到。
        await self._reclaim()

        now = datetime.now(UTC)
        due = await self._store.due_runs(now)
        woken: list[str] = []
        for run in due:
            overdue_s = int((now - run.deadline_at).total_seconds())
            if await wake_graph(self._graph, run.task_id, lock=self._ctx.lock):
                woken.append(run.task_id)
                log.warning(
                    "sweeper.woke",
                    task_id=run.task_id,
                    status=run.status.value,
                    overdue_s=overdue_s,
                    hint="过了 deadline 还没闭合屏障 —— wait 会走超时分支给掉队者补发 failed 结果",
                )
            else:
                # ``wake_graph`` 返回 False 的两种原因：锁被别人拿了（正常），
                # 或者这个 run 根本没有 checkpoint。后者值得单独说一句 ——
                # 它意味着一个 run 卡住了且没有任何东西能救它。
                log.warning(
                    "sweeper.not_woken",
                    task_id=run.task_id,
                    status=run.status.value,
                    overdue_s=overdue_s,
                    hint="没有可唤醒的 checkpoint，或另一个副本正在唤醒它",
                )
        if due:
            log.info("sweeper.tick", due=len(due), woken=len(woken))
        return woken

    async def _reclaim(self) -> None:
        """把空闲超时的消息放回「可投递」—— **含未被 ack 的 bootstrap**。

        ``None`` 是「全部消费者组」（``review_bootstrap`` / ``review_tasks``
        各 lane / ``review_results``），理由见 :meth:`sweep_once`。
        **不用 worker_type 是因为这里不是 Worker** —— 编排器要捞的是自己那两条流。

        失败不向上抛：回收需要 Redis，而扫描需要 Postgres，两者是独立的依赖。
        让回收的失败把这一轮扫描也带走，等于让 Redis 的抖动顺带停掉超时兜底，
        而后者正是「Redis 挂了」时唯一还能救 run 的东西。
        """
        try:
            count = await self._ctx.queue.reclaim(None)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sweeper.reclaim_failed")
            return
        if count:
            log.warning(
                "sweeper.reclaimed",
                count=count,
                hint="有消息空闲超过 CLAIM_IDLE_MS 没被确认 —— 多半来自一个崩掉的消费者",
            )
