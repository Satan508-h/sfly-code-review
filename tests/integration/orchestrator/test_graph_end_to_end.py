"""M5 的端到端验收 —— 对着**真 Redis + 真 Postgres**跑完整张图。

这个文件和 M4 那个 ``test_consume_loop.py`` 的分工：那边测「一条任务从队列到
落库」，这边测「一条 bootstrap 从入队到出报告」。中间隔着的正是 M5 新增的东西：
``interrupt()``、屏障、协调协程、扫描器、聚合。

### 为什么不用 ``MockLLM`` 之外的替身

``LLM_PROVIDER=mock`` 是默认值，所以这里跑的是**生产路径上的同一套代码**，
唯一的差别是模型不花钱。用一个假的 GraphRunner 或者假的 store 来测这个文件，
等于把要验证的那几件事（checkpoint 序列化、``interrupt`` 的恢复语义、
屏障查询的时机）全部换成自己的实现 —— 那测的是测试自己。

### 三件事只有在这里才验证得了

1. ``interrupt()`` 真的挂起并且真的能恢复（单测里没有 checkpointer）。
2. 屏障靠**数据库查询**闭合，而不是靠消息到达的顺序。
3. 扫描器能救一个「图挂起了、没有任何人叫它」的 run。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph

from factories import bootstrap, demo_patches
from redis_support import redis_test_url
from sfly_agent.state import ReviewState
from sfly_bus.base import Lock, TaskQueue
from sfly_bus.postgres import PostgresRunStore
from sfly_bus.redis_streams import RedisLock, RedisStreamsQueue
from sfly_orchestrator.checkpointer import open_checkpointer
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.coordinator import Coordinator
from sfly_orchestrator.graph import build_graph, thread_config
from sfly_orchestrator.runner import GraphRunner
from sfly_orchestrator.sweeper import Sweeper
from sfly_shared.config import Settings
from sfly_shared.contracts import (
    ErrorClass,
    ResultStatus,
    RunStatus,
    WorkerResult,
    WorkerType,
)
from sfly_workers.pool import WorkerPool

pytestmark = pytest.mark.integration

#: 等一个 run 到终态的上限。本地的 Mock + 真 Redis 下实测在 1 秒以内 ——
#: 30 秒是给 CI 上偶发的慢启动留的余量，不是预期耗时。
_WAIT_S = 30.0


def _settings(**over: Any) -> Settings:
    """显式给出被测的那几项。

    开发机上有 ``.env``（``LLM_PROVIDER``、``RUN_DEADLINE_S`` 都可能被改过），
    不隔离的话测试结果会随本机配置变化 —— 而「在我机器上是绿的」
    正是这类测试最没有价值的形态。
    """
    base: dict[str, Any] = {
        "llm_provider": "mock",
        "max_attempts": 3,
        "claim_idle_ms": 60_000,
        # 单进程跑三条 lane，每条 lane 一个消费者就够 —— 默认的 4 会让
        # 12 条协程抢 3 条消息，日志噪音大而没有任何东西被多验证到。
        "worker_concurrency": 1,
        "run_deadline_s": 600,
    }
    base.update(over)
    return Settings(**base)


async def _wait_until(
    predicate: Callable[[], Awaitable[bool]], *, what: str, budget_s: float = _WAIT_S
) -> None:
    """等一个异步条件成立。超时时报出**在等什么**，而不是一句 assert False。"""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{budget_s}s 内没有等到：{what}")


async def _status_of(store: PostgresRunStore, task_id: str) -> RunStatus | None:
    run = await store.get_run(task_id)
    return run.status if run is not None else None


async def _wait_status(
    store: PostgresRunStore, task_id: str, status: RunStatus, *, budget_s: float = _WAIT_S
) -> None:
    await _wait_until(
        lambda: _is_status(store, task_id, status),
        what=f"run {task_id} 变成 {status.value}",
        budget_s=budget_s,
    )


async def _is_status(store: PostgresRunStore, task_id: str, status: RunStatus) -> bool:
    return await _status_of(store, task_id) is status


async def _has_event(store: PostgresRunStore, task_id: str, kind: str) -> bool:
    return any(e.kind == kind for e in await store.events_since(task_id, 0))


async def _wait_event(store: PostgresRunStore, task_id: str, kind: str, *, budget_s: float = _WAIT_S) -> None:
    """等一条事件出现。

    **不能用「状态到终态了」代替它**：``publish`` 先写状态、后写事件，
    所以状态翻过去的那一刻 ``run.finished`` 可能还没落库。那个窗口是刻意的
    （见 ``nodes/publish.py`` 里关于两种失败方向的说明），断言必须等它真正
    要断言的东西 —— 否则就是一个偶尔会红的测试。
    """
    await _wait_until(
        lambda: _has_event(store, task_id, kind), what=f"{task_id} 出现 {kind} 事件", budget_s=budget_s
    )


@pytest.fixture
async def queue() -> AsyncIterator[RedisStreamsQueue]:
    q = RedisStreamsQueue(redis_test_url(), client_name="sfly-test-graph")
    await q.start()
    try:
        yield q
    finally:
        await q.close()


@pytest.fixture
async def lock() -> AsyncIterator[RedisLock]:
    lk = RedisLock(redis_test_url(), client_name="sfly-test-graph-lock")
    await lk.start()
    try:
        yield lk
    finally:
        await lk.close()


@pytest.fixture
async def checkpointer() -> AsyncIterator[AsyncPostgresSaver]:
    """真 checkpointer，建在**测试库**上。

    ``setup()`` 会建 ``checkpoints`` / ``checkpoint_blobs`` / ``checkpoint_writes``
    三张表 —— 它们不归我们的迁移器管（LangGraph 有自己的 ``checkpoint_migrations``），
    所以 ``truncate_all`` 刻意跳过它们（见 ``postgres_support.CHECKPOINT_PREFIX``）。
    """
    from postgres_support import postgres_test_dsn

    async with open_checkpointer(postgres_test_dsn(), connect_timeout_s=5) as saver:
        await saver.setup()
        yield saver


@asynccontextmanager
async def running_pipeline(
    ctx: NodeContext,
    graph: CompiledStateGraph[ReviewState],
    *,
    workers: bool,
    sweeper_s: float = 0.2,
) -> AsyncIterator[None]:
    """起整条流水线（图 + 协调 + 扫描 + 可选的内置 Worker）。

    ``workers=False`` 用来测**屏障**：没有 Worker 就没有结果，
    图必然停在 ``interrupt()`` —— 那是唯一能确定性地走到挂起状态的方式。
    """
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(GraphRunner(ctx=ctx, graph=graph).run(stop), name="test-graph"),
        asyncio.create_task(Coordinator(ctx=ctx, graph=graph).run(stop), name="test-coordinator"),
        asyncio.create_task(
            Sweeper(ctx=ctx, graph=graph, interval_s=sweeper_s).run(stop), name="test-sweeper"
        ),
    ]
    if workers:
        tasks.append(
            asyncio.create_task(
                WorkerPool(queue=ctx.queue, store=ctx.store, settings=ctx.settings).run(stop),
                name="test-worker-pool",
            )
        )
    try:
        yield
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _build(ctx: NodeContext, checkpointer: AsyncPostgresSaver) -> CompiledStateGraph[ReviewState]:
    return build_graph(ctx, checkpointer=checkpointer)


@pytest.fixture
def ctx(store: PostgresRunStore, queue: TaskQueue, lock: Lock) -> NodeContext:
    return NodeContext(store=store, queue=queue, lock=lock, settings=_settings())


# --------------------------------------------------------------------------- #
# 1. 正常路径：三个 Worker 全部上报，图一路跑到底
# --------------------------------------------------------------------------- #


async def test_a_run_goes_end_to_end_and_produces_a_report(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """**M5 的验收测试。**一次真正的多 Agent 审查，从 bootstrap 到报告。"""
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=True):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_event(store, run.task_id, "run.finished")

    # 三个 Worker 都上报了，而且都是成功的结果 —— 不是「三个都失败了，
    # 所以屏障也闭合了」那种假通过。
    results = await store.get_results(run.task_id)
    assert {r.worker_type for r in results} == set(WorkerType)
    assert all(r.status is ResultStatus.OK for r in results), "Mock 在这份 fixture 上不该有失败"

    report = await store.get_report(run.task_id)
    assert report is not None
    assert report.findings, "这份 fixture 是刻意塞满问题的，零发现说明链路某处断了"
    assert not report.degraded
    assert report.missing_workers == []
    # 置信度是**重算过的**，不是模型自报的那个数：没有旁证的单条声称
    # 一定要低于模型的自报值（见 aggregate/confidence.py 的文档）。
    assert all(f.adjusted_confidence < f.confidence for f in report.findings)
    assert report.comment_body.startswith(f"<!-- sfly:run:{run.task_id} -->")

    # 时间线：图的七个节点各留下了痕迹。
    events = await store.events_since(run.task_id, 0)
    kinds = [e.kind for e in events]
    for expected in ("run.created", "worker.dispatched", "aggregate.done", "publish.done", "run.finished"):
        assert expected in kinds, f"时间线里没有 {expected}：{kinds}"
    assert kinds.count("worker.dispatched") == len(WorkerType)
    assert kinds.count("worker.result") == len(WorkerType)


async def test_the_graph_events_are_in_causal_order(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """图自己写的事件必须严格有序。

    ``worker.result`` 刻意**不在**这个断言里：它由 Worker 写（见
    ``sfly_workers.pool._record_result_event``），而 Worker 和图是并发的两条
    路径 —— 「Worker 写完了结果行、还没写事件」的那一瞬间，图完全可能已经
    从数据库看到三条结果并 aggregate 完了。那个窗口是几微秒，但它是真的，
    断言它等于写一个偶尔会红的测试。
    """
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=True):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_event(store, run.task_id, "run.finished")

    events = await store.events_since(run.task_id, 0)
    ordered = [e.kind for e in events if e.kind != "worker.result"]
    # 「某事件第一次出现的下标」必须单调递增 —— 用 index 而不是顺序相等，
    # 因为节点可以写多条同类事件（三次 worker.dispatched）。
    positions = [
        ordered.index(kind)
        for kind in (
            "run.created",
            "node.finished",  # plan
            "worker.dispatched",
            "aggregate.done",
            "publish.done",
            "run.finished",
        )
    ]
    assert positions == sorted(positions), f"事件顺序不对：{ordered}"


# --------------------------------------------------------------------------- #
# 2. 屏障：真的挂起，而且真的能恢复
# --------------------------------------------------------------------------- #


async def test_the_graph_suspends_at_the_barrier(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """没有 Worker 上报时图必须**挂起**（而不是空转或直接往下走）。

    这是断点恢复故事的前提：挂起之后进程里没有这个 run 的任何状态。
    断言两件事 —— 状态是 ``waiting``（扫描器靠它找到这个 run），
    以及 checkpoint 里真的有一个待处理的 ``interrupt``。
    """
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=False):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_status(store, run.task_id, RunStatus.WAITING)

        snapshot = await graph.aget_state(thread_config(run.task_id))
        assert snapshot.next == ("wait",), f"挂起的应该是 wait 节点，实际是 {snapshot.next}"
        assert snapshot.interrupts, "checkpoint 里没有 interrupt —— 那说明图是空转，不是挂起"
        # 挂起时给出的信息要够运维判断「在等谁」。
        payload = snapshot.interrupts[0].value
        assert set(payload["waiting_on"]) == {w.value for w in WorkerType}

        # 谁都没上报，状态不该自己变。
        await asyncio.sleep(0.3)
        assert await _status_of(store, run.task_id) is RunStatus.WAITING


async def test_the_barrier_closes_and_the_graph_resumes(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """补上最后一条结果，图应当被**唤醒**并跑完。

    唤醒的判据是数据库里的屏障查询，不是「消息到达」——
    所以这里直接调 ``store.save_result``，连队列都不用碰。
    """
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=False):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_status(store, run.task_id, RunStatus.WAITING)

        for worker in (WorkerType.SECURITY, WorkerType.PERFORMANCE):
            await store.save_result(
                WorkerResult(
                    task_id=run.task_id,
                    worker_type=worker,
                    status=ResultStatus.OK,
                    latency_ms=1,
                )
            )
        # 差一个的时候必须仍然挂着 —— 「两条到了就往下走」是屏障最典型的写错法。
        await asyncio.sleep(0.3)
        assert await _status_of(store, run.task_id) is RunStatus.WAITING

        # 现在**主动唤醒**一次：模拟协调协程收到最后那条结果消息。
        # 如果只靠 save_result，没有任何人会来叫醒这个 run —— 那正是
        # coordinator 存在的理由（它消费 review_results 并做屏障检查）。
        await store.save_result(
            WorkerResult(
                task_id=run.task_id, worker_type=WorkerType.STYLE, status=ResultStatus.OK, latency_ms=1
            )
        )
        from sfly_orchestrator.runner import wake_graph

        await wake_graph(graph, run.task_id, lock=ctx.lock)
        await _wait_event(store, run.task_id, "run.finished")

    report = await store.get_report(run.task_id)
    assert report is not None
    assert report.findings == [], "三条结果都没有 findings，报告不该凭空多出内容"


# --------------------------------------------------------------------------- #
# 3. 超时兜底：扫描器救一个没人叫的 run
# --------------------------------------------------------------------------- #


async def test_the_sweeper_wakes_an_overdue_run_and_it_finishes_degraded(
    store: PostgresRunStore, queue: RedisStreamsQueue, lock: RedisLock, checkpointer: AsyncPostgresSaver
) -> None:
    """**「图暂停期间 orchestrator 崩了」那条恢复路径。**

    这里没有真的崩进程，但效果一样：没有任何东西会叫醒这个 run ——
    没有 Worker 上报、协调协程收不到任何结果。唯一把它往前推的是
    ``due_runs()`` 那条 SQL。

    走完之后 run 应当是 ``published`` + ``degraded``，并且**三个掉队的 Worker
    各有一条 failed 结果**（约定 #2：不补的话屏障永远闭合不了）。
    """
    ctx = NodeContext(store=store, queue=queue, lock=lock, settings=_settings(run_deadline_s=1))
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=False, sweeper_s=0.2):
        await queue.publish_bootstrap(msg)
        await _wait_status(store, run.task_id, RunStatus.WAITING)
        await _wait_event(store, run.task_id, "run.finished", budget_s=15)

    results = await store.get_results(run.task_id)
    assert {r.worker_type for r in results} == set(WorkerType)
    assert all(r.status is ResultStatus.FAILED for r in results)
    assert all(r.error_class is ErrorClass.TRANSIENT for r in results)
    assert all("超时" in (r.error or "") for r in results)

    report = await store.get_report(run.task_id)
    assert report is not None
    assert report.degraded is True
    assert set(report.missing_workers) == set(WorkerType)
    assert "不完整" in report.comment_body, "降级必须写在评论正文的最上面"


async def test_the_timeout_result_wins_over_a_late_result(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """超时兜底写下的 failed 结果，会**吸收掉**之后才到的真实结果。

    这是 ``worker_results`` 的主键（``(task_id, worker_type)``）加上
    ``ON CONFLICT DO NOTHING`` 的直接后果，也是刻意的：报告已经从「超时」
    这个视角生成了，让一个迟到的结果去改它会让数据库和报告对不上。
    deadline 是一个承诺，不是一个建议。

    这条测试存在的意义是**把这个决定钉住** —— 否则某天有人觉得
    「晚到的真实结果应该覆盖超时标记」，那会是一次没有对照组的行为变更。
    """
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=False):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_status(store, run.task_id, RunStatus.WAITING)

        await store.save_result(
            WorkerResult.failed(run.task_id, WorkerType.SECURITY, "等待超时", ErrorClass.TRANSIENT)
        )
        # 迟到的真实结果：主键已占，写不进去。
        await store.save_result(
            WorkerResult(task_id=run.task_id, worker_type=WorkerType.SECURITY, status=ResultStatus.OK)
        )

    stored = {r.worker_type: r for r in await store.get_results(run.task_id)}
    assert stored[WorkerType.SECURITY].status is ResultStatus.FAILED


# --------------------------------------------------------------------------- #
# 4. 幂等与清理
# --------------------------------------------------------------------------- #


async def test_a_duplicate_bootstrap_reuses_the_same_run(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """同一个 PR 的同一个 head_sha 投两次 → 一个 run。

    这就是 M6 要演示的「投 3 次 → 1 run + 2 duplicate」，只是现在没有 webhook。
    第二次的 ``task_id`` 是新的（ULID），而幂等键指向已有的那一个 ——
    ``GraphRunner`` 必须跟着**已有的**那个走，否则会产出两份报告、两条评论，
    而且两边都不会报错。
    """
    graph = _build(ctx, checkpointer)
    first = bootstrap(file_patches=demo_patches())
    run = await store.create_run(first)

    async with running_pipeline(ctx, graph, workers=True):
        await ctx.queue.publish_bootstrap(first)
        await _wait_event(store, run.task_id, "run.finished")

        # 第二次投递：换了 task_id，幂等键不变。
        second = first.model_copy(update={"task_id": "01JTESTDUP0000000000000000"})
        assert second.task_id != first.task_id
        await ctx.queue.publish_bootstrap(second)
        # 给它足够的时间去犯错 —— 如果它真建了新 run，下面那条断言会红。
        await asyncio.sleep(0.5)

    assert await store.get_run(second.task_id) is None, "重投不该建出第二个 run"
    runs = await store.list_runs()
    assert len(runs) == 1


async def test_purge_also_deletes_the_checkpoints(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver, raw_pg: Any
) -> None:
    """M4 欠下的那条：``purge_older_than`` 里清 checkpoint 的分支**现在真的会被执行**。

    M4 交付时 checkpoint 表还不存在（要等 M5 接入 ``AsyncPostgresSaver``），
    所以那段代码是被 ``to_regclass`` 保护着的**未验证代码**。README 里当时写着
    「它是代码不是证据」。这条测试把它变成证据 —— 而且顺便验证了那条判断本身：
    表存在时必须真的删，表不存在时必须安静返回 0。
    """
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=True):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_event(store, run.task_id, "run.finished")

    def _count_checkpoints() -> int:
        row = raw_pg.execute(
            "SELECT count(*) FROM checkpoints WHERE thread_id = %s", [run.task_id]
        ).fetchone()
        return int(row[0]) if row else 0

    assert _count_checkpoints() > 0, "跑完一张图之后 checkpoint 不该是空的"

    # 把 run 变成「14 天前」的，然后清理。
    raw_pg.execute(
        "UPDATE review_runs SET created_at = now() - interval '30 days' WHERE task_id = %s",
        [run.task_id],
    )
    counts = await store.purge_older_than(days=14)

    # 一次 run 会写**很多**条 checkpoint（LangGraph 每个节点执行后各一条），
    # 所以这里只断言「删了、且删干净了」，不猜具体条数。
    assert counts["checkpoints"] > 0
    assert _count_checkpoints() == 0
    assert counts["review_runs"] == 1
    assert await store.get_run(run.task_id) is None
    assert await store.events_since(run.task_id, 0) == []


async def test_a_pr_with_nothing_to_review_is_skipped(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """没有可审文件时标 ``skipped``，**不产出一份「0 条发现」的报告**。

    后者会被读成「审查通过」，而实际上什么都没看 —— 那是最坏的一种误导
    （``sfly_workers --diff`` 在空 diff 上宁可退出码 3，说的是同一件事）。
    """
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=[])
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=True):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_status(store, run.task_id, RunStatus.SKIPPED)

    assert await store.get_report(run.task_id) is None
    kinds = [e.kind for e in await store.events_since(run.task_id, 0)]
    assert "run.finished" in kinds
    assert "worker.dispatched" not in kinds


async def test_the_run_row_records_the_plan(
    ctx: NodeContext, store: PostgresRunStore, checkpointer: AsyncPostgresSaver
) -> None:
    """``plan`` 的产物必须真的落库 —— 扫描器的恢复查询全靠这几列。"""
    graph = _build(ctx, checkpointer)
    msg = bootstrap(file_patches=demo_patches())
    run = await store.create_run(msg)

    async with running_pipeline(ctx, graph, workers=True):
        await ctx.queue.publish_bootstrap(msg)
        await _wait_event(store, run.task_id, "run.finished")

    planned = await store.get_run(run.task_id)
    assert planned is not None
    assert set(planned.planned_workers) == set(WorkerType)
    assert planned.files_reviewed == len(msg.file_patches)
    assert planned.deadline_at > datetime.now(UTC), "deadline 应当是未来（跑完时还没到）"
    assert planned.dispatched_at is not None
