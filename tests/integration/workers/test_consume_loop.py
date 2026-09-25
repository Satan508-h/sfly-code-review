"""Worker 常驻消费循环的端到端验证（M4）。

**这个文件测的是 ``_process_one`` 里那三行的顺序** —— 它是 CLAUDE.md 约定 #1
（投递顺序铁律）在代码里唯一出现的地方，而它的两个反例都是灾难：

* 先 ``XADD`` 后写库 → 编排器被唤醒去读一个还不存在的结果，屏障检查失败
* 先 ``XACK`` 后写库 → 结果同时从 PEL 和数据库消失，**永久丢失**

所以这里用**真 Redis + 真 Postgres**，而不是两个假对象：这三行要成立，
靠的正是 Redis 的 PEL 与 Postgres 的主键约束各自的行为。假对象只会把
「我以为的行为」再确认一遍。

Mock LLM 在这个文件里不是占位符：它是确定性的规则扫描器，所以「补丁里有硬编码
密钥 → 结果里有 secrets 类的 finding」是一条可以断言的链路。拿一段随便的代码去测，
测试会在**零发现**那条路径上通过，而那条路径什么都验证不到。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import psycopg
import pytest

from factories import bootstrap, task
from redis_support import redis_test_url
from sfly_agent.llm.base import LLMProvider, LLMResponse
from sfly_agent.llm.registry import build_llm
from sfly_bus.postgres import PostgresRunStore
from sfly_bus.redis_streams import RedisStreamsQueue
from sfly_shared.config import Settings
from sfly_shared.contracts import (
    ErrorClass,
    FilePatch,
    ResultStatus,
    WorkerType,
)
from sfly_shared.errors import SchemaUnrecoverableError, TransientError
from sfly_workers.__main__ import _process_one
from sfly_workers.runner import WorkerRunner
from sfly_workers.specs import spec_for

pytestmark = pytest.mark.integration

TASK_ID = "01JTESTRUN0000000000000001"

#: 一次「应该立刻发生」的等待上限。挂住的测试比失败的测试糟得多。
_WAIT_S = 5.0

#: 一份**会被 Mock LLM 报出问题**的补丁：硬编码密钥 + f-string 拼 SQL。
#: 新增行的行号（新文件视角）是 2/3/4，与 ``changed_lines`` 一致 ——
#: 不一致的话 finding 会被降级成文件级评论（``source_line_verified=False``），
#: 而那是另一个话题。
#:
#: **``diff --git`` / ``---`` / ``+++`` 三行头不能省。** Mock 靠
#: ``iter_added_lines`` 从提示词里读新增行，而它需要文件头才能确定行号 ——
#: 只给一个 ``@@`` 的话它**静默地什么都不返回**（实测），于是 Mock 报出
#: 「0 条发现」，测试在「干净的补丁」那条路径上通过，看起来一切正常。
#: 真实的补丁（``parse_unified_diff`` 的产物）永远带着头，所以这只是
#: 「测试里的假数据不够真」这一类问题 —— 而它恰好最难发现。
_DIFF_PATCH = FilePatch(
    path="app/db.py",
    language="python",
    patch=(
        "diff --git a/app/db.py b/app/db.py\n"
        "--- a/app/db.py\n"
        "+++ b/app/db.py\n"
        "@@ -1,2 +1,5 @@\n"
        " import os\n"
        '+API_KEY = "sk-live-abcdef123456"\n'
        "+def get(uid):\n"
        '+    return db.execute(f"SELECT * FROM users WHERE id = {uid}")\n'
    ),
    additions=3,
    changed_lines=[2, 3, 4],
)


# --------------------------------------------------------------------------- #
# 桩
# --------------------------------------------------------------------------- #


class _CountingLLM:
    """包一层计数。``LLMProvider`` 是结构化协议，实现这三个成员就够。"""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.model = inner.model
        self.calls = 0

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls += 1
        return await self._inner.complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )


class _RaisingLLM:
    """一个必定失败的 provider。

    抛出的**异常类型决定重试策略**（可重试 → 三次机会；不可重试 → 直接进死信），
    所以这里通过构造不同的异常来走两条分支 —— 而不是在测试里直接造一条
    ``WorkerResult.failed``（那样就绕过了 ``classify()``，而它是被验证的一环）。
    """

    name = "raising"
    model = "raising-1"

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        raise self.exc


def _settings(**over: Any) -> Settings:
    """显式给出被测的那几项。

    开发机上有 ``.env``，不隔离的话测试结果会随本机配置变化 ——
    而「在我机器上是绿的」正是这类测试最没有价值的形态。
    """
    base: dict[str, Any] = {"llm_provider": "mock", "max_attempts": 3, "claim_idle_ms": 60_000}
    base.update(over)
    return Settings(**base)


@pytest.fixture
async def queue() -> AsyncIterator[RedisStreamsQueue]:
    q = RedisStreamsQueue(redis_test_url(), client_name="sfly-test-worker")
    await q.start()
    try:
        yield q
    finally:
        await q.close()


async def _wait_for(predicate: Callable[[], Awaitable[bool]], *, what: str) -> None:
    """等一个异步条件成立。超时就报出**在等什么**，而不是一句 assert False。"""
    deadline = time.monotonic() + _WAIT_S
    while time.monotonic() < deadline:
        if await predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"{_WAIT_S}s 内没有等到：{what}")


async def _next_task(queue: RedisStreamsQueue) -> Any:
    """取一条待处理任务 —— 每条测试都从这里开始，顺序固定。"""
    return await anext(queue.consume_tasks(WorkerType.SECURITY))


async def _deliver(queue: RedisStreamsQueue, store: PostgresRunStore, **task_over: Any) -> Any:
    """建 run → 投任务 → 取回句柄与消息。"""
    msg = task(TASK_ID, file_patches=[_DIFF_PATCH], **task_over)
    await store.create_run(bootstrap(task_id=TASK_ID))
    await queue.publish_task(msg)
    return await _next_task(queue)


def _runner(llm: LLMProvider, settings: Settings) -> WorkerRunner:
    return WorkerRunner(spec_for("security"), llm, settings)


# --------------------------------------------------------------------------- #
# 投递顺序铁律
# --------------------------------------------------------------------------- #


async def test_the_worker_saves_then_publishes_then_acks(
    queue: RedisStreamsQueue, store: PostgresRunStore
) -> None:
    """**M4 的核心交付。**

    三件事都要发生，而且顺序固定：结果先在数据库里（编排器的屏障查的就是它）、
    再出现在 ``review_results``（唤醒编排器）、最后才离开 PEL（ack）。

    顺序反过来的两种写法各自的后果写在 ``_process_one`` 的文档字符串里。
    这里能断言的是结果，不是顺序 —— 而结果的组合恰好只有一种写法能同时满足。
    """
    settings = _settings()
    handle, got = await _deliver(queue, store)
    llm = _CountingLLM(build_llm(settings, worker_types=(WorkerType.SECURITY,)))

    await _process_one(
        handle, got, runner=_runner(llm, settings), queue=queue, store=store, settings=settings
    )

    # 1. 落库了 —— 而且 findings 逐条展开（Mock 是确定性扫描器，坏代码必有发现）
    results = await store.get_results(TASK_ID)
    assert len(results) == 1
    assert results[0].status is ResultStatus.OK
    assert results[0].findings, "补丁里有硬编码密钥，Mock 必须报出来 —— 否则这条测试什么也没验证"
    assert {f.category for f in results[0].findings} & {"secrets", "sqli"}
    assert llm.calls == 1

    # 2. 结果进了 review_results —— 编排器靠这一条被唤醒
    result_handle, published = await anext(queue.consume_results())
    assert published.task_id == TASK_ID
    assert published.worker_type is WorkerType.SECURITY
    assert published.findings[0].file == "app/db.py"
    await result_handle.ack()

    # 3. 原消息离开了 PEL
    assert await queue.pending_count(WorkerType.SECURITY) == 0


async def test_a_redelivered_message_does_not_call_the_llm_again(
    queue: RedisStreamsQueue, store: PostgresRunStore
) -> None:
    """回收之后重投的消息，第二次不该再花一次 LLM 的钱。

    **正确性不靠这条快路径** —— 靠的是 ``worker_results`` 的主键。所以这里
    同时断言两件事：LLM 一次都没调（省钱），结果仍然只有一份（幂等）。
    分开断言是因为它们的失效方式不同：快路径失效只是多花钱，幂等失效是数据错。
    """
    settings = _settings()
    handle, got = await _deliver(queue, store)
    first = _CountingLLM(build_llm(settings, worker_types=(WorkerType.SECURITY,)))
    await _process_one(
        handle, got, runner=_runner(first, settings), queue=queue, store=store, settings=settings
    )
    assert first.calls == 1

    # 同一条任务再投一次（回收、重派都会造成这个）
    await queue.publish_task(task(TASK_ID, file_patches=[_DIFF_PATCH]))
    second = _CountingLLM(build_llm(settings, worker_types=(WorkerType.SECURITY,)))
    handle2, got2 = await _next_task(queue)

    await _process_one(
        handle2, got2, runner=_runner(second, settings), queue=queue, store=store, settings=settings
    )

    assert second.calls == 0, "已经写过的任务不该再调一次 LLM"
    assert await queue.pending_count(WorkerType.SECURITY) == 0
    assert len(await store.get_results(TASK_ID)) == 1


# --------------------------------------------------------------------------- #
# 失败路径
# --------------------------------------------------------------------------- #


async def test_a_crashed_review_still_writes_a_result(
    queue: RedisStreamsQueue, store: PostgresRunStore
) -> None:
    """**失败也是结果**（约定 #2）—— 它在本层的表现是「屏障能闭合」。

    如果不补发这条 ``failed`` 结果就 ack，``wait`` 节点永远等不到 style 那个
    Worker，整个 run 挂到超时；而日志里只有一条「Worker 报错」，看不出
    run 为什么不动了。
    """
    settings = _settings()
    handle, got = await _deliver(queue, store)

    await _process_one(
        handle,
        got,
        runner=_runner(_RaisingLLM(TransientError("连接超时")), settings),
        queue=queue,
        store=store,
        settings=settings,
    )

    results = await store.get_results(TASK_ID)
    assert len(results) == 1, "失败也必须留下一条记录"
    assert results[0].status is ResultStatus.FAILED
    assert results[0].error_class is ErrorClass.TRANSIENT
    assert results[0].attempt == 1
    # 屏障：失败也算完成
    assert await store.completed_workers(TASK_ID) == [WorkerType.SECURITY]

    # 结果照样发出去了 —— 编排器要的就是「有个结果了」
    _, published = await anext(queue.consume_results())
    assert published.status is ResultStatus.FAILED

    # 第一次失败还有两次机会，不该进死信
    assert await queue.dead_letters() == []
    assert await queue.pending_count(WorkerType.SECURITY) == 0


async def test_a_non_retryable_failure_lands_in_the_dead_letter(
    queue: RedisStreamsQueue, store: PostgresRunStore
) -> None:
    """不可重试的错误一次就进死信：再试一百次结果一样，而每次都要烧一份 prompt。

    死信**和 failed 结果并存** —— 它不是完成机制（约定 #2），只是运维可见性。
    所以这里同时断言两处都有东西：库里一条 failed、死信里一条带错误分类的记录。
    """
    settings = _settings()
    handle, got = await _deliver(queue, store)

    await _process_one(
        handle,
        got,
        runner=_runner(_RaisingLLM(SchemaUnrecoverableError("模型连坏三次")), settings),
        queue=queue,
        store=store,
        settings=settings,
    )

    letters = await queue.dead_letters()
    assert len(letters) == 1
    assert letters[0]["task_id"] == TASK_ID
    assert letters[0]["worker_type"] == "security"
    assert letters[0]["error_class"] == "schema_unrecoverable"
    assert letters[0]["attempt"] == "1"
    assert "连坏三次" in letters[0]["error"]

    # 并存：failed 结果仍然在库里（屏障靠它闭合）
    assert (await store.get_results(TASK_ID))[0].status is ResultStatus.FAILED
    # 死信自己会 ack，所以 PEL 是空的
    assert await queue.pending_count(WorkerType.SECURITY) == 0


async def test_a_store_failure_leaves_the_message_unacked(
    queue: RedisStreamsQueue, store: PostgresRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Postgres 不可达时绝不 ack** —— README 里「Postgres 不可达不丢结果」那一行。

    消息留在 PEL 里，等 Postgres 恢复后被 ``reclaim()`` 捞回来重跑，两处都不丢。
    先 ack 的话结果就同时从 PEL 和数据库消失 —— 那种丢失是**永久**的，
    而且不会有任何日志。
    """

    async def _boom(_result: Any) -> None:
        raise psycopg.OperationalError("postgres 挂了")

    monkeypatch.setattr(store, "save_result", _boom)
    settings = _settings()
    handle, got = await _deliver(queue, store)

    with pytest.raises(psycopg.OperationalError):
        await _process_one(
            handle,
            got,
            runner=_runner(build_llm(settings, worker_types=(WorkerType.SECURITY,)), settings),
            queue=queue,
            store=store,
            settings=settings,
        )

    assert await queue.pending_count(WorkerType.SECURITY) == 1, "没写进库的消息必须留在 PEL 里"
    assert await queue.dead_letters() == [], "存储层故障不是这条消息的错，不该进死信"


async def test_the_consumer_survives_a_store_failure_and_keeps_consuming(
    queue: RedisStreamsQueue, store: PostgresRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一条消息写库失败**不能让消费协程退出**。

    这是最危险的一种故障：协程死了，容器还在跑（心跳文件照写），
    ``docker ps`` 显示 healthy，而 ``review_tasks`` 的 lag 一直涨 ——
    没有任何地方报错。所以 ``_consumer`` 必须逐条接住存储层异常并继续。

    （业务错误在 ``_process_one`` 里就已经有自己的归宿了；能漏到这里的只有
    存储层故障。）
    """
    from sfly_workers.__main__ import _consumer  # 局部导入：只有这条测试驱动整个循环

    settings = _settings()
    handle, _ = await _deliver(queue, store)
    await handle.ack()  # 把投递过的这条先清掉，避免它干扰计数

    async def _boom(_result: Any) -> None:
        raise psycopg.OperationalError("postgres 挂了")

    monkeypatch.setattr(store, "save_result", _boom)
    await queue.publish_task(task(TASK_ID, file_patches=[_DIFF_PATCH]))

    consumer = asyncio.create_task(
        _consumer(
            spec_for("security"),
            _runner(build_llm(settings, worker_types=(WorkerType.SECURITY,)), settings),
            queue=queue,
            store=store,
            settings=settings,
        ),
        name="test-consumer",
    )
    try:
        # 消息被取走但没能处理完 → 它还在 PEL 里
        await _wait_for(lambda: _has_pending(queue), what="消息被取走但仍未确认（留在 PEL 里）")
        assert not consumer.done(), "消费协程不该因为一条消息写库失败就退出"
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


async def _has_pending(queue: RedisStreamsQueue) -> bool:
    return await queue.pending_count(WorkerType.SECURITY) > 0
