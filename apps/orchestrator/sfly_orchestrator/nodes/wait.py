"""``wait`` —— 屏障。**全项目唯一用到 ``interrupt()`` 的地方，也是风险最高的地方。**

### 为什么是 interrupt 而不是轮询

``interrupt()`` 会 checkpoint 然后**退出图执行** —— 进程不再持有这个 run 的任何
内存状态。图挂起之后 orchestrator 崩掉，恢复靠的是一条 SQL 查询
（``review_runs.status + deadline_at``），而不是某个还活着的定时器。
这是「断点恢复」这个故事唯一能成立的地方。

``WAIT_STRATEGY=poll`` 是逃生开关：同样是循环检查屏障，只是不挂起、原地等。
节点签名完全相同，所以切过去只影响这一个函数。**卡住超过一天就切过去** ——
先跑通优于先优雅。它有已知代价：轮询期间整条消费协程被占住，
一次只能推进一个 run（``interrupt`` 之下 ``ainvoke`` 几十毫秒就返回了）。
所以它是开关，不是默认值。

### 循环，不是一次判断

节点被唤醒后 LangGraph 会**从头重跑它**（这是 ``interrupt()`` 的语义，
不是实现细节）。所以屏障检查写成 ``while``：

* 被唤醒 → 重新查一次屏障 → 闭合了就往下走，没闭合就**再挂起一次**
* 扫描器提前唤醒（deadline 判断在别处）→ 同上，不会带着不完整的屏障往下跑

**判断依据是数据库，不是唤醒信号携带的内容。** 唤醒方传什么值都不影响结果 ——
协调协程、超时扫描器、甚至手工 resume，走的都是同一条路径、得到同一个结论。

### 超时兜底

过了 deadline 还有 Worker 没上报时，这里给它们各补一条 ``status=failed`` 的结果。
**这是让屏障能闭合的唯一手段**（约定 #2）：不补的话屏障永远差一个，
而 aggregate 又必须等屏障 —— run 会挂到天荒地老。

副作用要知道：``worker_results`` 的主键是 ``(task_id, worker_type)``，
所以超时结果一旦写入，**晚到的真实结果会被吸收掉**。这是刻意的 ——
报告已经从「超时」这个视角生成了，让一个迟到的结果去改它，
会让数据库和报告对不上。deadline 是一个承诺，不是一个建议。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from langgraph.types import interrupt

from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import ErrorClass, RunStatus, WorkerResult, WorkerType
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 唤醒信号的内容。**它不被使用** —— 节点靠重新查库得出结论
#: （见模块文档）。留着它是为了让 ``Command(resume=...)`` 有个非空值，
#: 以及在 ``aget_state`` 里能一眼看出「这个 run 是被谁唤醒的」。
WAKE_REASON = "barrier_check"

#: ``WAIT_STRATEGY=poll`` 之下两次屏障检查之间的间隔。
#:
#: 比 ``SWEEPER_INTERVAL_S``（15s）短得多：扫描器是「兜底恢复」，
#: 晚 15 秒无所谓；而轮询是**正常路径**，它的间隔直接等于每次审查的额外延迟。
_POLL_INTERVAL_S = 2.0


async def wait(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    task_id = state["task_id"]
    planned = [WorkerType(w) for w in state.get("planned_workers", [])]
    deadline = _deadline(state)
    polling = ctx.settings.wait_strategy == "poll"

    while True:
        completed = await ctx.store.completed_workers(task_id)
        missing = [w for w in planned if w not in completed]

        if not missing:
            log.info("node.wait_closed", task_id=task_id, workers=len(completed))
            return {
                "completed": {w.value: True for w in completed},
                "deadline_missed": [],
                "degraded": False,
            }

        now = datetime.now(UTC)
        if now >= deadline:
            await _fail_stragglers(state, ctx, missing, deadline)
            return {
                "completed": {w.value: True for w in completed},
                # 用 ``deadline_missed`` 而不是 ``missing_workers``：后者在报告里
                # 的含义是「哪一路没有产出可用的结果」（见 aggregate/pipeline.py），
                # 而这里是「哪一路在 deadline 之前没有上报」。两者在超时路径上
                # 恰好一致，在「Worker 快速失败并上报了 failed 结果」那条路上不一致 ——
                # 而那种不一致的表现是「同一个词在两处指的是不同的人」。
                "deadline_missed": [w.value for w in missing],
                "degraded": True,
            }

        if polling:
            log.info(
                "node.wait_polling",
                task_id=task_id,
                waiting_on=[w.value for w in missing],
                remaining_s=int((deadline - now).total_seconds()),
            )
            await asyncio.sleep(_POLL_INTERVAL_S)
            continue

        # 挂起之前把状态落库：扫描器就是靠 ``status IN ('dispatched','waiting')``
        # 找到「卡在屏障后面的 run」的。不写这一行的话，一个在 wait 挂起的 run
        # 在数据库里看起来还停在 dispatched —— 那还算能被扫到，
        # 但状态机的语义就错了（``dispatched`` 的含义是「已派发、还没进屏障」）。
        await ctx.store.set_status(task_id, RunStatus.WAITING)
        log.info(
            "node.wait_suspend",
            task_id=task_id,
            waiting_on=[w.value for w in missing],
            remaining_s=int((deadline - now).total_seconds()),
        )
        # 这一行之后图就退出了。被唤醒时会**从头重跑本函数**。
        interrupt(
            {
                "task_id": task_id,
                "waiting_on": [w.value for w in missing],
                "deadline_at": deadline.isoformat(),
            }
        )


async def _fail_stragglers(
    state: ReviewState, ctx: NodeContext, missing: list[WorkerType], deadline: datetime
) -> None:
    """给还没上报的 Worker 各补一条失败结果。见模块文档「超时兜底」。"""
    task_id = state["task_id"]
    for worker in missing:
        result = WorkerResult.failed(
            task_id,
            worker,
            f"等待超时：deadline {deadline.isoformat()} 之前没有上报结果",
            ErrorClass.TRANSIENT,
        )
        await ctx.store.save_result(result)
        await ctx.emit(
            task_id,
            "worker.failed",
            {
                "worker_type": worker.value,
                "error_class": ErrorClass.TRANSIENT.value,
                "reason": "deadline_exceeded",
            },
        )
    log.warning(
        "node.wait_timeout",
        task_id=task_id,
        missing=[w.value for w in missing],
        deadline=deadline.isoformat(),
        hint="已为掉队的 Worker 补发 failed 结果 —— 屏障因此闭合，run 以 degraded 收尾",
    )


def _deadline(state: ReviewState) -> datetime:
    """从状态里取 deadline。**缺了就直接报错。**

    给一个默认值（比如「现在 + 600 秒」）看起来更宽容，但那会让一个
    「plan 没跑过」的图静静地跑满一轮超时，而正确行为是当场失败 ——
    deadline 是 plan 的核心产物，它不在就说明图被从错误的入口启动了。
    """
    raw = state.get("deadline_at")
    if not raw:
        raise ValueError("状态里没有 deadline_at —— plan 节点没有被执行过，图不能进入 wait")
    parsed = datetime.fromisoformat(raw)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
