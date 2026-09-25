"""两种传输实现**共用**的行为契约 —— 「一套代码、两种拓扑」的证据本身。

M3 会加一个 ``test_redis_streams.py``，同样继承 :class:`QueueContract`，
于是下面每一条测试都会在两个后端上各跑一遍。**这就是为什么这个文件比
它的体积重要得多**：让契约测试退化成「只有 Redis 跑得通」的套件，
等于把项目的中心论点从「事实」降级成「宣传」。

### 写这里的测试时的三条纪律

1. **只用 Protocol 上的方法。** 一旦用到某个实现的私有方法，
   这份文件就变成了那个实现的测试，第二个后端继承它时只能靠跳过。
   需要看 PEL 深度之类的东西时，用 ``reclaim()`` 的返回值代替 ——
   它恰好能回答「这条消息还在不在 PEL 里」。

2. **不要为了迁就实现而放宽断言。** Redis 的近似裁剪（``MAXLEN ~``）不会
   裁到精确条数、``XADD`` 的 id 格式和这里不同 —— 这类差异应该让实现去
   适配契约，而不是让契约绕开它。真绕不过去时，把那条测试降级到
   某一端的专属文件里，**并在两边都写上为什么**。

3. **断言可观察的结果，不断言机制。** 「回收之后消息能再次被消费到」
   比「回收之后 PEL 里少了一条」更接近契约 —— 后者是 Redis 的实现细节，
   而前者是两种拓扑都欠下的语义。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from sfly_bus.base import MessageHandle, TaskQueue
from sfly_shared.contracts import (
    BootstrapMessage,
    ErrorClass,
    FilePatch,
    Finding,
    ResultStatus,
    Severity,
    TaskMessage,
    WorkerResult,
    WorkerType,
    idempotency_key_for,
)

#: 任何一次「这里应该立刻有消息」的等待上限。
#:
#: 用它是为了让**失败快速失败**而不是挂死：挂住的测试比失败的测试糟得多 ——
#: CI 会一直等到全局超时，而那时你手上一条线索都没有。
_STEP_TIMEOUT_S = 5.0

#: 「这里应该什么都没有」的观察窗口。不能是 0：消息至少要走一个事件循环周期
#: 才可能到达，0 秒等于根本没等。
_QUIET_WINDOW_S = 0.15

#: 关闭队列之后，阻塞中的消费者必须在这个时间内退出。
#: 这是给 Redis 实现的一条**硬约束**：``XREADGROUP BLOCK`` 不能设得很长
#: （建议 ≤ 1000ms），并且每一轮都要检查关闭标志。做不到的话，取消失败的表现是
#: 「进程不退出」，而那时日志里一个字都没有。
_CLOSE_TIMEOUT_S = 2.0

# --------------------------------------------------------------------------- #
# 等待辅助
# --------------------------------------------------------------------------- #


async def next_message[MsgT](
    gen: AsyncIterator[tuple[MessageHandle, MsgT]],
) -> tuple[MessageHandle, MsgT]:
    """取下一条，超过 ``_STEP_TIMEOUT_S`` 就失败。

    刻意不给超时参数：每个调用点的等待上限都该是同一个常量 ——
    一旦允许逐个调整，就会有人把某个可疑的测试调到 30 秒，
    于是它从「失败」变成「很慢但通过」。
    """
    return await asyncio.wait_for(anext(gen), _STEP_TIMEOUT_S)


async def first_message[MsgT](
    gen: AsyncIterator[tuple[MessageHandle, MsgT]],
) -> tuple[MessageHandle, MsgT]:
    """``anext(gen)``，但返回的是**协程**。

    给需要 ``asyncio.create_task`` 的场景用 —— 它只收协程。直接传
    ``anext(...)`` 在运行时其实是能跑的（异步生成器的 ``__anext__`` 返回的就是
    协程），但类型上写的是 ``Awaitable``，所以类型检查器会拦下来。
    这里包一层，让「能被调度」这件事在类型里也成立。
    """
    return await anext(gen)


async def collect[MsgT](
    gen: AsyncIterator[tuple[MessageHandle, MsgT]],
) -> list[tuple[MessageHandle, MsgT]]:
    """在 ``_QUIET_WINDOW_S`` 这个观察窗口内尽量收，然后返回收到的（可能为空）。

    用于断言「什么都不该来」。:func:`next_message` 做不到这件事 ——
    它会一直等到超时然后失败，而不是返回一个空列表。
    """
    out: list[tuple[MessageHandle, MsgT]] = []

    async def pump() -> None:
        async for item in gen:
            out.append(item)

    task = asyncio.create_task(pump())
    try:
        await asyncio.wait_for(task, _QUIET_WINDOW_S)
    except TimeoutError:
        pass
    finally:
        # 取消任务会把 CancelledError 抛进生成器正在等待的那一行，
        # 于是 `async for` 正常退栈、生成器被关闭 —— 不用手动 aclose()。
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return out


# --------------------------------------------------------------------------- #
# 构造器
# --------------------------------------------------------------------------- #


def bootstrap(**over: Any) -> BootstrapMessage:
    """一个合法的 BootstrapMessage。``idempotency_key`` 由三元组算出来，
    不留给人填 —— 填错的话契约层会直接拒绝，那不是这里想测的东西。"""
    base: dict[str, Any] = {
        "task_id": "01JTESTRUN0000000000000000",
        "repo_id": "123456",
        "repo_node_id": "R_kgDOAbcdef",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "pr_title": "给查询接口加上分页",
        "pr_author": "contributor",
        "file_patches": [
            FilePatch(path="app/db.py", language="python", patch="@@ -1 +1 @@\n-x\n+y\n", changed_lines=[1]),
        ],
    }
    base.update(over)
    base["idempotency_key"] = idempotency_key_for(base["repo_id"], base["pr_number"], base["head_sha"])
    return BootstrapMessage(**base)


def task(task_id: str, *, worker_type: WorkerType = WorkerType.SECURITY) -> TaskMessage:
    """``task_id`` 是**同一个 run 的所有 Worker 共用的**那个 id。
    多条消息各带不同 task_id，就等于这条流上同时跑着多个 run —— 这是常态。"""
    boot = bootstrap()
    return TaskMessage(
        task_id=task_id,
        worker_type=worker_type,
        idempotency_key=boot.idempotency_key,
        repo_id=boot.repo_id,
        repo_node_id=boot.repo_node_id,
        pr_number=boot.pr_number,
        head_sha=boot.head_sha,
        base_sha=boot.base_sha,
        file_patches=list(boot.file_patches),
        language="python",
    )


def finding(**over: Any) -> Finding:
    base: dict[str, Any] = {
        "file": "app/db.py",
        "line": 12,
        "severity": Severity.HIGH,
        "category": "sqli",
        "message": "用 f-string 拼接 SQL，用户输入没有参数化",
        "confidence": 0.8,
    }
    base.update(over)
    return Finding.model_validate(base)


def result(task_id: str, *, findings: list[Finding] | None = None) -> WorkerResult:
    return WorkerResult(
        task_id=task_id,
        worker_type=WorkerType.SECURITY,
        status=ResultStatus.OK,
        findings=findings if findings is not None else [finding()],
        tokens_in=1234,
        tokens_out=567,
        cached_tokens=800,
        latency_ms=4321,
        model="mock-1",
    )


# --------------------------------------------------------------------------- #
# 契约
# --------------------------------------------------------------------------- #


class QueueContract[Q: TaskQueue]:
    """两个后端都要满足的行为。

    子类**只需**实现 ``make_queue``（一个普通方法，不是 fixture）：

        class TestInMemoryQueue(QueueContract[InMemoryQueue]):
            async def make_queue(self, **kwargs: Any) -> InMemoryQueue:
                q = InMemoryQueue(**kwargs)
                await q.start()
                return q

    刻意不用 fixture：``async with self.open_queue() as q`` 让每个测试自带
    清理，断言失败时 ``finally`` 一样会跑 —— 而 fixture 的清理顺序和
    「哪个测试开了两个队列」这类问题的纠缠，不值得为省一行 import 去换。
    """

    async def make_queue(self, **kwargs: Any) -> Q:
        raise NotImplementedError("子类必须实现 make_queue")

    @asynccontextmanager
    async def open_queue(self, **kwargs: Any) -> AsyncIterator[Q]:
        q = await self.make_queue(**kwargs)
        try:
            yield q
        finally:
            await q.close()

    # -- 往返：M2 的验收标准 ----------------------------------------------- #

    async def test_full_roundtrip_bootstrap_then_task_then_result(self) -> None:
        """发布 → 消费 → ack → 发布结果 → 消费结果。

        这条就是 M2 的验收标准。它同时钉住了三件事：消息能原样往返
        （Pydantic 相等，不是「字段看着对」）、publish 返回的 id 和
        handle 上的 id 是同一个、以及 ack 之后积压归零。
        """
        async with self.open_queue() as q:
            boot = bootstrap()
            boot_id = await q.publish_bootstrap(boot)
            assert boot_id, "publish 必须返回一个可用于日志串联的消息 id"

            h1, got_boot = await next_message(q.consume_bootstrap())
            assert got_boot == boot
            assert h1.id == boot_id
            assert h1.attempt == 1
            await h1.ack()

            t = task(got_boot.task_id)
            await q.publish_task(t)
            h2, got_task = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert got_task == t
            await h2.ack()

            r = result(t.task_id)
            await q.publish_result(r)
            h3, got_result = await next_message(q.consume_results())
            assert got_result == r
            await h3.ack()

            assert await q.lag(WorkerType.SECURITY) == 0

    async def test_failed_result_roundtrips(self) -> None:
        """失败结果也是结果（约定 #2），所以它必须能正常往返。

        ``WorkerResult.failed()`` 是屏障闭合的唯一途径 —— 如果失败结果在传输层
        出问题（比如某个字段序列化不了），表现是「run 挂到超时」，
        而且只在有 Worker 失败时才出现，平时测不到。
        """
        async with self.open_queue() as q:
            r = WorkerResult.failed(
                "run-1", WorkerType.STYLE, "模型吐了 11KB 散文", ErrorClass.SCHEMA_UNRECOVERABLE
            )
            await q.publish_result(r)
            h, got = await next_message(q.consume_results())
            assert got.status is ResultStatus.FAILED
            assert got.error_class is ErrorClass.SCHEMA_UNRECOVERABLE
            assert got.error == "模型吐了 11KB 散文"
            await h.ack()

    # -- 组是独立游标，不是分工 -------------------------------------------- #

    async def test_one_group_delivers_each_message_to_exactly_one_consumer(self) -> None:
        """同组多消费者竞争消费 —— 这就是 ``--scale worker-security=3`` 的机制。

        断言的是**排序后的集合相等**：多一条（重复消费）少一条（丢消息）
        都会失败。单看条数不行 —— 一多一少也会凑出同样的数字。
        """
        n = 12
        async with self.open_queue() as q:
            for i in range(n):
                await q.publish_task(task(f"run-{i}"))

            seen: list[str] = []
            done = asyncio.Event()

            async def consume() -> None:
                async for handle, msg in q.consume_tasks(WorkerType.SECURITY):
                    seen.append(msg.task_id)
                    await handle.ack()
                    if len(seen) >= n:
                        done.set()
                        return

            consumers = [asyncio.create_task(consume()) for _ in range(3)]
            try:
                await asyncio.wait_for(done.wait(), _STEP_TIMEOUT_S)
            finally:
                for c in consumers:
                    c.cancel()
                await asyncio.gather(*consumers, return_exceptions=True)

            assert sorted(seen) == sorted(f"run-{i}" for i in range(n))

    async def test_a_foreign_worker_type_is_acked_but_not_delivered(self) -> None:
        """性能组读得到安全任务，但**不会**把它交给调用方。

        组是独立游标而不是分工，所以同一条任务会被三个组各读一遍 ——
        每个组都要按 ``worker_type`` 过滤，并且**把过滤掉的 ack 掉**。
        不 ack 的话那个组的 ``lag`` 会永远挂着一条它永远不会处理的消息，
        而这个只增不减的数字会让健康检查一直报假警。
        """
        async with self.open_queue() as q:
            await q.publish_task(task("run-sec", worker_type=WorkerType.SECURITY))

            assert await collect(q.consume_tasks(WorkerType.PERFORMANCE)) == []
            assert await q.lag(WorkerType.PERFORMANCE) == 0, "过滤掉的必须被 ack，否则 lag 永远不降"

            # 而安全组仍然拿得到它 —— 组之间互不影响
            h, got = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert got.worker_type is WorkerType.SECURITY
            await h.ack()

    # -- 回收与重投 --------------------------------------------------------- #

    async def test_unacked_message_is_redelivered_with_attempt_two(self) -> None:
        """消费者猝死 → 消息被回收 → 重投时 ``attempt`` 是 2。

        ``attempt`` 是死信判定的依据（超过 ``MAX_ATTEMPTS`` 就进死信），
        所以它必须**跨回收保留**，而不是每次投递都从 1 开始。
        """
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            h1, first = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert h1.attempt == 1
            # 刻意不 ack：模拟这个消费者在 LLM 调用中间被 docker kill 掉

            assert await q.reclaim(WorkerType.SECURITY) == 1

            h2, again = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert again.task_id == first.task_id
            assert h2.attempt == 2
            await h2.ack()

    async def test_acked_message_is_not_reclaimed(self) -> None:
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
            await h.ack()
            assert await q.reclaim(WorkerType.SECURITY) == 0

    async def test_ack_stays_idempotent_after_a_reclaim(self) -> None:
        """回收之后原消费者才 ack —— 这是**必然会发生**的，不是边界情况。

        LLM 一次调用可能跑 60 秒以上，而回收阈值是 180 秒 —— 慢一点的消费者
        就是会被回收、被重投、然后才 ack。所以 ack 必须幂等，
        而且不能把重投出去的那一条从 PEL 里误删（那会让它被无限重投）。
        """
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
            await q.reclaim(WorkerType.SECURITY)
            await h.ack()
            await h.ack()  # 重复 ack 不能报错
            assert await q.reclaim(WorkerType.SECURITY) == 0

    async def test_reclaim_without_a_type_covers_every_group(self) -> None:
        """``reclaim()`` 不带参数时要覆盖全部组。

        编排器的扫描器用的是无参形式 —— 它不关心「哪个 worker_type 掉队了」，
        只关心「有没有该重投的」。
        """
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            await q.publish_result(result("run-1"))
            await next_message(q.consume_tasks(WorkerType.SECURITY))
            await next_message(q.consume_results())
            assert await q.reclaim() == 2

    # -- 死信 --------------------------------------------------------------- #

    async def test_dead_lettering_also_acks(self) -> None:
        """死信**同时**是一次 ack。

        不 ack 的话这条消息会永远留在 PEL 里被一遍遍捞回来，而死信的全部意义
        就是「别再重试它了」—— 那样等于死信机制自己制造了无限重试。
        注意死信不是完成机制（约定 #2）：调用方**另外**还得发一条 failed 结果。
        """
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
            await h.to_dead_letter("模型连续三次返回散文", ErrorClass.SCHEMA_UNRECOVERABLE)
            assert await q.reclaim(WorkerType.SECURITY) == 0
            assert await q.lag(WorkerType.SECURITY) == 0

    # -- 生命周期 ----------------------------------------------------------- #

    async def test_close_wakes_a_blocked_consumer(self) -> None:
        """关闭必须能**叫醒**阻塞中的消费者。

        做不到的表现是「进程不退出」，而且日志里一个字都没有 —— 因为
        消费者正安静地睡在 ``await wait()`` / ``XREADGROUP BLOCK`` 上。
        """
        async with self.open_queue() as q:
            pending = asyncio.create_task(first_message(q.consume_bootstrap()))
            await asyncio.sleep(0.05)  # 让它真的进到阻塞里
            await q.close()
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(pending, _CLOSE_TIMEOUT_S)

    async def test_publish_after_close_fails_loudly(self) -> None:
        """关掉之后收下消息，会得到「投递成功但 run 永远卡在 wait」——
        最难查的一种故障。宁可在这里直接炸。"""
        async with self.open_queue() as q:
            await q.close()
            with pytest.raises(RuntimeError):
                await q.publish_task(task("run-1"))
