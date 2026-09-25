"""``PostgresRunStore`` 的往返测试 —— 对着一个**真实的** Postgres 跑。

### 为什么这里没有 ``tests/contracts/store_contract.py``

``tests/contracts/`` 存在的理由是「**两个实现**必须表现一致」：队列和锁各有内存
与 Redis 两套实现，契约就是它们必须同时满足的那份行为，两个后端各继承一次。

``RunStore`` 只有 Postgres 一个实现 —— 精简模式去掉的是 Redis，不是数据库
（``wait`` 节点的屏障查询、幂等性的唯一约束、SSE 的事件日志、超时扫描全在上面）。
给一个实现写一份「契约」就只是换个文件名的测试，而且会让「契约」这个词在这个
仓库里变得含糊：以后看到契约文件的人得先判断它是「两个后端的共同行为」还是
「某个实现的测试」。

**所以这里就是一个普通的集成测试文件。** 这个判断本身值得写下来 ——
把「什么时候该建契约」写清楚，比多建一个契约有用。

跑之前需要有 Postgres：``python tasks.py up postgres``。测试跑在 ``<库名>_test``
上，会话开始时删掉重建（见 ``tests/postgres_support.py``）。

### 这些测试在测什么

不是「SQL 能不能跑通」，而是**协议里那些不显然的语义**：
幂等键冲突返回老行、失败也算完成、``None`` 参数表示「别动这一列」、
报告要能逐字段往返 jsonb、成本记录要比 run 活得久。
每一条都对应 CLAUDE.md 里某一条约定在存储层的落地。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest

from factories import bootstrap, finding, report, result
from sfly_bus.postgres import PostgresRunStore
from sfly_shared.contracts import (
    BootstrapMessage,
    ErrorClass,
    ResultStatus,
    ReviewReport,
    RunRow,
    RunStatus,
    Severity,
    WorkerResult,
    WorkerType,
)
from sfly_shared.ids import new_task_id

pytestmark = pytest.mark.integration

#: 一个不存在的 run id。所有「没有这条记录会怎样」的断言都用它 ——
#: 写成一个常量是为了让「我以为它不存在」这件事只有一个地方要检查。
NO_SUCH_RUN = "01JNOSUCHRUN00000000000000"


async def _run(store: PostgresRunStore, task_id: str) -> RunRow:
    """取一行 run —— 取不到就直接失败。

    测试里到处写 ``assert run is not None`` 会把断言的重点（字段的值）
    淹没在类型收窄里，所以收窄一次放在这里。
    """
    run = await store.get_run(task_id)
    assert run is not None, f"run {task_id} 应该存在"
    return run


async def _report(store: PostgresRunStore, task_id: str) -> ReviewReport:
    stored = await store.get_report(task_id)
    assert stored is not None, f"run {task_id} 的报告应该存在"
    return stored


def _run_message(*, pr_number: int = 7, head_sha: str = "a" * 40) -> BootstrapMessage:
    """一个新的 run 消息。

    ``task_id`` 每次重新生成：``bootstrap()`` 的默认 id 是固定字面量，
    拿它建第二个 run 会撞**主键**冲突 —— 而 ``create_run`` 幂等处理的只是
    「同一个 PR 被投递两次」这条路径（幂等键冲突）。
    """
    return bootstrap(task_id=new_task_id(), pr_number=pr_number, head_sha=head_sha)


# --------------------------------------------------------------------------- #
# run 生命周期
# --------------------------------------------------------------------------- #


async def test_a_run_round_trips(store: PostgresRunStore) -> None:
    msg = _run_message()
    created = await store.create_run(msg)

    assert created.task_id == msg.task_id
    assert created.idempotency_key == msg.idempotency_key
    assert created.status is RunStatus.QUEUED
    assert created.created_at.tzinfo is not None, "timestamptz 必须带着时区回来，否则时间比较会静默出错"
    # 「还没算」和「算出来是 0」是两件事：totals 为空表示 aggregate 还没跑过
    assert created.totals is None
    assert created.planned_workers == []
    assert created.block_merge is None, "可空 = 还没做决定，和 false（决定了不阻断）不同"
    assert created.github_comment_id is None

    # deadline 在**建 run 的那一刻**就落库了，不依赖 plan 节点跑过 ——
    # 「任何可能卡住的状态都必须是一行带 deadline 的记录」
    expected = datetime.now(UTC) + timedelta(seconds=600)
    assert abs((created.deadline_at - expected).total_seconds()) < 30

    assert await _run(store, msg.task_id) == created
    assert await store.get_run_by_key(msg.idempotency_key) == created
    assert await store.get_run(NO_SUCH_RUN) is None


async def test_the_same_pr_twice_creates_only_one_run(store: PostgresRunStore) -> None:
    """GitHub 超时后会重投同一个 webhook。**那不是故障，是常态。**

    返回老的那一行而不是抛错：调用方（API）拿它回 ``{"status": "duplicate"}``，
    而调用方不需要为此写 try/except —— 「重放」在幂等键这一层就已经被吸收了。
    """
    first = await store.create_run(_run_message())
    second = await store.create_run(_run_message())  # 同一个 PR、同一个 head_sha

    assert second.task_id == first.task_id
    assert second.created_at == first.created_at
    assert len(await store.list_runs()) == 1


async def test_due_runs_only_returns_runs_past_their_deadline(store: PostgresRunStore) -> None:
    now = datetime.now(UTC)

    overdue = await store.create_run(_run_message(head_sha="a" * 40))
    await store.set_plan(
        overdue.task_id,
        [WorkerType.SECURITY],
        files_total=1,
        files_reviewed=1,
        diff_truncated=False,
        deadline_at=now - timedelta(seconds=1),
    )

    in_time = await store.create_run(_run_message(head_sha="b" * 40))
    await store.set_plan(
        in_time.task_id,
        [WorkerType.SECURITY],
        files_total=1,
        files_reviewed=1,
        diff_truncated=False,
        deadline_at=now + timedelta(minutes=5),
    )

    due = [r.task_id for r in await store.due_runs(now)]
    assert due == [overdue.task_id]


async def test_a_queued_run_is_not_swept(store: PostgresRunStore, raw_pg: psycopg.Connection[Any]) -> None:
    """还在 ``queued`` 的 run 由**队列层**的 ``reclaim()`` 负责，不归扫描器管。

    两种失败的状态住在两个不同的地方，所以由两个机制各自兜住：

    * bootstrap 消息还没被消费 → 消息在 Redis 的 PEL 里 → 队列回收
    * 图已经派发出去、卡在等屏障 → 状态在 Postgres 里 → 扫描器按 deadline 唤醒

    把 ``queued`` 也塞进 ``due_runs`` 会让两个机制同时去救同一个 run，
    而扫描器那一侧没有「bootstrap 消息还在不在」的信息 —— 它只能盲目唤醒，
    唤醒一个没有 checkpoint 的图。这条测试钉住的就是这个边界。
    """
    run = await store.create_run(_run_message())
    # 直接把它改成「已经过期」—— 正常路径下 queued 的 deadline 在 10 分钟后
    raw_pg.execute(
        "UPDATE review_runs SET deadline_at = now() - interval '1 hour' WHERE task_id = %s",
        [run.task_id],
    )

    assert await store.due_runs(datetime.now(UTC)) == []

    # 一旦派发出去，同一个 run 就立刻归扫描器管了
    await store.set_status(run.task_id, RunStatus.DISPATCHED)
    assert [r.task_id for r in await store.due_runs(datetime.now(UTC))] == [run.task_id]


# --------------------------------------------------------------------------- #
# 结果与幂等
# --------------------------------------------------------------------------- #


async def test_a_result_is_written_once_even_if_reported_twice(
    store: PostgresRunStore, raw_pg: psycopg.Connection[Any]
) -> None:
    """**幂等性的真正保证在这里，不在 Redis 的 SETNX。**

    同一条任务被回收后重跑是设计内的路径（``CLAIM_IDLE_MS`` 一到就发生），
    所以「同一个 (task_id, worker_type) 上报两次」必须由数据库约束吸收掉。
    SETNX 只是省 token 的快路径：它会过期、会随 Redis 重启丢失、
    还可能在后续写库失败时已经被设上。
    """
    msg = await store.create_run(_run_message())
    first = result(msg.task_id, findings=[finding(line=1), finding(line=2), finding(line=3)])
    await store.save_result(first)

    # 第二次上报：内容不同、状态也不同（模拟重跑拿到了不一样的结果）
    await store.save_result(result(msg.task_id, findings=[], status=ResultStatus.FAILED, error="重跑失败"))

    got = await store.get_results(msg.task_id)
    assert len(got) == 1
    assert got[0].status is ResultStatus.OK, "先写的那条赢，第二次上报必须被静默丢弃"
    assert [f.line for f in got[0].findings] == [1, 2, 3]
    assert raw_pg.execute("SELECT count(*) FROM worker_results").fetchone() == (1,)
    assert raw_pg.execute("SELECT count(*) FROM findings").fetchone() == (3,), "重复上报不该重复写发现"
    assert await store.exists_result(msg.task_id, WorkerType.SECURITY) is True
    assert await store.exists_result(msg.task_id, WorkerType.STYLE) is False


async def test_a_failed_result_still_counts_as_completed(store: PostgresRunStore) -> None:
    """失败也是结果 —— 这是它在本层的落地（CLAUDE.md 约定 #2）。

    ``completed_workers`` 刻意**不带 status 过滤**：写成「哪些 Worker 成功了」
    的话，一个 Worker 失败之后屏障永远闭合不了，整个 run 挂到超时。
    """
    msg = await store.create_run(_run_message())
    await store.save_result(
        WorkerResult.failed(
            msg.task_id,
            WorkerType.STYLE,
            "模型连续三次返回坏 JSON",
            ErrorClass.SCHEMA_UNRECOVERABLE,
        )
    )

    assert await store.completed_workers(msg.task_id) == [WorkerType.STYLE]

    got = (await store.get_results(msg.task_id))[0]
    assert got.status is ResultStatus.FAILED
    assert got.error_class is ErrorClass.SCHEMA_UNRECOVERABLE, "枚举要能往返，不能退化成裸字符串"
    assert got.findings == []


async def test_a_result_for_an_unknown_run_is_rejected(store: PostgresRunStore) -> None:
    """外键是刻意留的：写一条野结果说明上游有 bug。

    这里宁可要一条 ``ForeignKeyViolation``，也不要一行没人认领的数据 ——
    后者会让 ``get_results`` 返回一条永远配不上任何 run 的结果，
    而排查它要从「哪来的 task_id」开始。
    """
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        await store.save_result(result(NO_SUCH_RUN))


async def test_findings_round_trip_field_by_field(store: PostgresRunStore) -> None:
    """逐字段往返 —— 包括 ``None`` 的字段和非 ASCII 的路径。

    ``end_line`` 那种可选字段最容易被写错：少写一个 ``if ... is not None``，
    ``None`` 就会在往返之后变成 0 或者空字符串，而**不会有任何报错**。
    """
    msg = await store.create_run(_run_message())
    findings = [
        finding(
            file="src/支付/api.ts",
            line=42,
            end_line=45,
            severity=Severity.CRITICAL,
            category="secrets",
            message="硬编码的 API key 会随仓库永久留存",
            evidence='api_key = "sk-live-…"',
            suggestion="改用环境变量并轮换这把 key",
            rule_id="sec-secrets-001",
            confidence=0.93,
            source_line_verified=True,
            fingerprint="9f2c1a",
        ),
        finding(line=7, end_line=None, severity=Severity.LOW, category="naming"),
    ]
    await store.save_result(result(msg.task_id, findings=findings))

    got = (await store.get_results(msg.task_id))[0].findings
    assert got == findings, "顺序也要一致（按写入顺序读回来）"
    assert got[1].end_line is None
    assert got[1].rule_id is None
    assert got[1].evidence is None


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #


async def test_set_status_only_touches_the_columns_it_is_given(store: PostgresRunStore) -> None:
    """不传的参数表示「别动这一列」，不是「置为空」。

    降级信息是**累积**的：``wait`` 节点补上缺失 Worker 时只想改这一个字段，
    而它不该顺手把 ``degraded`` 抹回 false（那会让降级徽章在最后一步消失）。
    """
    msg = await store.create_run(_run_message())
    await store.set_status(msg.task_id, RunStatus.WAITING, degraded=True, missing_workers=[WorkerType.STYLE])
    await store.set_status(msg.task_id, RunStatus.AGGREGATING)  # 只带 status

    run = await _run(store, msg.task_id)
    assert run.status is RunStatus.AGGREGATING
    assert run.degraded is True, "没传 degraded，应该保留旧值"
    assert run.missing_workers == [WorkerType.STYLE], "没传 missing_workers，应该保留旧值"


async def test_set_plan_does_not_move_a_waiting_run_backwards(store: PostgresRunStore) -> None:
    """图被恢复时会**重新执行** ``plan`` 节点（LangGraph 的重放语义）。

    所以这个写入必须是「从 queued 推进到 dispatched」，而不是「无条件设成
    dispatched」—— 后者会把一个已经在等屏障的 run 倒退回去，症状是扫描器
    开始盯上一个其实正在正常等待的 run。``dispatched_at`` 同理，只写第一次。
    """
    msg = await store.create_run(_run_message())
    planned_at = datetime.now(UTC) + timedelta(minutes=3)
    await store.set_plan(
        msg.task_id,
        [WorkerType.SECURITY, WorkerType.STYLE],
        files_total=4,
        files_reviewed=3,
        diff_truncated=True,
        deadline_at=planned_at,
    )

    dispatched = await _run(store, msg.task_id)
    assert dispatched.status is RunStatus.DISPATCHED
    assert dispatched.planned_workers == [WorkerType.SECURITY, WorkerType.STYLE]
    assert dispatched.diff_truncated is True
    assert dispatched.dispatched_at is not None

    await store.set_status(msg.task_id, RunStatus.WAITING)
    extended = planned_at + timedelta(minutes=5)
    await store.set_plan(
        msg.task_id,
        [WorkerType.SECURITY, WorkerType.STYLE],
        files_total=4,
        files_reviewed=3,
        diff_truncated=True,
        deadline_at=extended,
    )

    after = await _run(store, msg.task_id)
    assert after.status is RunStatus.WAITING, "重放不能让 run 从 waiting 倒回 dispatched"
    assert after.deadline_at == extended, "但 deadline 是可以延长的"
    assert after.dispatched_at == dispatched.dispatched_at, "首次派发时间不该被覆盖"


async def test_mark_published_records_the_comment_without_changing_status(
    store: PostgresRunStore,
) -> None:
    """``status`` 只能由 ``set_status`` 改 —— 两个写入方会让最终状态取决于调用顺序，
    而那是「本地跑得好好的、线上偶尔不对」的典型来源。
    """
    msg = await store.create_run(_run_message())
    await store.set_status(msg.task_id, RunStatus.WAITING)

    await store.mark_published(msg.task_id, 123456789)

    run = await _run(store, msg.task_id)
    assert run.github_comment_id == 123456789
    assert run.published_at is not None
    assert run.status is RunStatus.WAITING


# --------------------------------------------------------------------------- #
# 报告与事件
# --------------------------------------------------------------------------- #


async def test_report_round_trips_through_jsonb(
    store: PostgresRunStore, raw_pg: psycopg.Connection[Any]
) -> None:
    """整份报告（含被砍掉的 suppressed 与冲突记录）要能逐字段往返。

    这是 M9 评测的地基：评测读的是**已落库**的报告，而不是内存里那个 ——
    如果 jsonb 往返会丢字段（``stage``、``conflicts``、``totals.per_worker_ms``
    这类嵌套结构最容易），评测测的就是另一个东西。
    """
    msg = await store.create_run(_run_message())
    original = report(msg.task_id)
    await store.save_report(original)

    assert await _report(store, msg.task_id) == original
    assert await store.get_report(NO_SUCH_RUN) is None

    # 冗余列：运行列表和评测不该为了拿一个数字去解 jsonb
    row = raw_pg.execute(
        """SELECT findings_count, suppressed_count, conflicts_count, cost_usd, comment_body
             FROM review_reports WHERE task_id = %s""",
        [msg.task_id],
    ).fetchone()
    assert row is not None
    assert row[:3] == (1, 1, 1)
    assert float(row[3]) == pytest.approx(0.004321)
    assert "sfly 审查报告" in str(row[4])

    # 重新聚合（比如人工重跑一次）应该替换而不是新增
    await store.save_report(report(msg.task_id, comment_body="第二版"))
    assert (await _report(store, msg.task_id)).comment_body == "第二版"
    assert raw_pg.execute("SELECT count(*) FROM review_reports").fetchone() == (1,)


async def test_events_are_resumable_from_any_seq(store: PostgresRunStore) -> None:
    """``seq > after_seq`` —— SSE 带 ``Last-Event-ID`` 重连时的补齐路径。

    ``seq`` 只要求**对单个 run 单调**（全局自增所以必然如此），不要求连续：
    客户端回传的是它见过的最后一个号，服务端从这个号往后补齐，中间有空洞
    无所谓 —— 空洞意味着那几条属于别的 run。
    """
    msg = await store.create_run(_run_message())
    seqs = [await store.append_event(msg.task_id, "node.started", {"node": "plan", "i": i}) for i in range(5)]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 5

    all_events = await store.events_since(msg.task_id, 0)
    assert [e.seq for e in all_events] == seqs
    assert all_events[2].payload == {"node": "plan", "i": 2}, "payload 是嵌套结构，要能往返"
    assert all_events[0].created_at.tzinfo is not None

    assert [e.seq for e in await store.events_since(msg.task_id, seqs[2])] == seqs[3:]
    assert await store.events_since(msg.task_id, seqs[-1]) == []

    # 另一个 run 的事件不会混进来
    other = await store.create_run(_run_message(head_sha="c" * 40))
    await store.append_event(other.task_id, "run.created", {})
    assert [e.seq for e in await store.events_since(msg.task_id, 0)] == seqs


async def test_list_runs_is_newest_first(store: PostgresRunStore) -> None:
    """按 ``task_id`` 倒序 == 按时间倒序（ULID 前 48 位是毫秒时间戳）。

    这里用**写死的** id 而不是 ``new_task_id()``：同一毫秒内生成的两个 ULID
    之间，顺序由随机后缀决定 —— 拿它断言等于写一个偶尔红的测试。
    （对列表接口来说无所谓，同一毫秒内谁先谁后都可以。）
    """
    ids = [
        "01JTESTRUN0000000000000001",
        "01JTESTRUN0000000000000002",
        "01JTESTRUN0000000000000003",
    ]
    for i, task_id in enumerate(ids):
        await store.create_run(bootstrap(task_id=task_id, head_sha=f"{i:040d}"))

    assert [r.task_id for r in await store.list_runs()] == ids[::-1]
    assert [r.task_id for r in await store.list_runs(limit=2)] == ids[::-1][:2]
    assert [r.task_id for r in await store.list_runs(limit=2, offset=2)] == [ids[0]]


# --------------------------------------------------------------------------- #
# 成本
# --------------------------------------------------------------------------- #


async def test_llm_calls_are_summed_per_run(store: PostgresRunStore) -> None:
    """评测报告里的每个数字都来自这一条查询。"""
    msg = await store.create_run(_run_message())
    for tokens_in, cost in ((1000, 0.001), (2000, 0.002), (3000, 0.003)):
        await store.record_llm_call(
            task_id=msg.task_id,
            agent="security",
            model="mock-1",
            tokens_in=tokens_in,
            tokens_out=100,
            cached_tokens=tokens_in // 2,
            cost_usd=cost,
            latency_ms=1200,
            ok=True,
        )
    # 独立 CLI 跑的单次审查没有 run —— task_id 允许为空
    await store.record_llm_call(
        task_id=None,
        agent="security",
        model="mock-1",
        tokens_in=9999,
        tokens_out=0,
        cached_tokens=0,
        cost_usd=9.99,
        latency_ms=10,
        ok=False,
        error_class="llm_timeout",
    )

    totals = await store.sum_costs(msg.task_id)
    assert totals["tokens_in"] == 6000
    assert totals["cached_tokens"] == 3000
    assert totals["llm_calls"] == 3
    assert totals["cost_usd"] == pytest.approx(0.006)

    # 没有调用记录的 run 返回零而不是空 —— 调用方不必为此写分支
    empty = await store.sum_costs(NO_SUCH_RUN)
    assert empty["cost_usd"] == 0
    assert empty["llm_calls"] == 0


# --------------------------------------------------------------------------- #
# 清理
# --------------------------------------------------------------------------- #


async def test_purging_removes_a_run_and_everything_under_it(
    store: PostgresRunStore, raw_pg: psycopg.Connection[Any]
) -> None:
    """清理靠外键级联，不靠 Python 里挨个删。

    亲手逐个删的话，将来加一张表就会漏一张 —— 而漏掉的那张表不会报错，
    只会慢慢把 Neon 的 0.5GB 吃满。
    """
    old = await store.create_run(_run_message())
    await store.save_result(result(old.task_id))
    await store.save_report(report(old.task_id))
    await store.append_event(old.task_id, "run.created", {})
    raw_pg.execute(
        "UPDATE review_runs SET created_at = now() - interval '30 days' WHERE task_id = %s",
        [old.task_id],
    )

    fresh = await store.create_run(_run_message(head_sha="f" * 40))
    await store.save_result(result(fresh.task_id))

    counts = await store.purge_older_than(days=14)

    assert counts["review_runs"] == 1
    assert counts["run_events"] == 1
    assert await store.get_run(old.task_id) is None
    assert await store.get_results(old.task_id) == []
    assert await store.get_report(old.task_id) is None
    assert raw_pg.execute("SELECT count(*) FROM findings").fetchone() == (1,)
    assert raw_pg.execute("SELECT count(*) FROM worker_results").fetchone() == (1,)

    # 新的一眼没动
    assert await store.get_run(fresh.task_id) is not None
    assert len(await store.get_results(fresh.task_id)) == 1


async def test_cost_records_outlive_the_run_they_belong_to(
    store: PostgresRunStore, raw_pg: psycopg.Connection[Any]
) -> None:
    """``llm_calls`` 没有外键，这是**故意的**。

    「今天花了多少钱」不该因为清理了 14 天前的 run 而变小。代价是这张表里的
    ``task_id`` 可能指向一条已经被删掉的 run —— 那一列是「归属线索」，
    不是引用。
    """
    old = await store.create_run(_run_message())
    await store.record_llm_call(
        task_id=old.task_id,
        agent="security",
        model="mock-1",
        tokens_in=1200,
        tokens_out=300,
        cached_tokens=0,
        cost_usd=0.5,
        latency_ms=800,
        ok=True,
    )
    # 一条更早的、连 run 都没有的调用（独立 CLI）
    await store.record_llm_call(
        task_id=None,
        agent="security",
        model="mock-1",
        tokens_in=500,
        tokens_out=50,
        cached_tokens=0,
        cost_usd=0.25,
        latency_ms=300,
        ok=True,
    )
    raw_pg.execute("UPDATE llm_calls SET created_at = now() - interval '30 days' WHERE task_id IS NULL")
    raw_pg.execute(
        "UPDATE review_runs SET created_at = now() - interval '30 days' WHERE task_id = %s",
        [old.task_id],
    )

    counts = await store.purge_older_than(days=14)

    assert counts["review_runs"] == 1
    assert await store.get_run(old.task_id) is None
    # 30 天前的那条按时间清掉了
    assert counts["llm_calls"] == 1
    # 而 run 虽然没了，它的成本还在
    assert (await store.sum_costs(old.task_id))["cost_usd"] == pytest.approx(0.5)
