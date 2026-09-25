"""``InMemoryQueue`` 的测试。

上面一大半（继承自 :class:`QueueContract`）**不是内存实现专属的** ——
M3 会用同一份契约去测 Redis。这个文件里只有三类东西：

1. 后端装配（``make_queue``）
2. 内存实现**能直接观察、而 Redis 观察不到**的行为（裁剪、死信内容）
3. 故意注入**生产路径不会产生**的输入（坏 payload）

第 2 类里有一条（裁剪）是刻意留在这里而不是放进契约的，理由见那条测试的注释。
"""

from __future__ import annotations

from typing import Any

from queue_contract import (
    QueueContract,
    bootstrap,
    collect,
    finding,
    next_message,
    result,
    task,
)

from sfly_bus.base import STREAMS
from sfly_bus.memory import InMemoryQueue
from sfly_shared.contracts import ErrorClass, WorkerType


class TestInMemoryQueue(QueueContract[InMemoryQueue]):
    async def make_queue(self, **kwargs: Any) -> InMemoryQueue:
        q = InMemoryQueue(**kwargs)
        # 必须 start()：消费者组在那一刻建出来，而建组时游标设在流尾。
        await q.start()
        return q

    # -- 裁剪（后端专属：Redis 的 MAXLEN ~ 不裁到精确条数） ------------------ #

    async def test_a_trimmed_pending_message_is_acked_never_delivered(self) -> None:
        """已被裁剪的消息：消费循环拿不到它，但它**必须**被 ack。

        两个失败方向都要防：

        * 把它交给调用方 → 空 payload 流进解析器，报一个和真因毫无关系的错
        * 不 ack → 它永远留在 PEL 里，``reclaim`` 一遍遍捞回来，每次都重新
          走一遍「payload 坏了」的分支

        Redis 那边测得同样的事，但方式不同：它的近似裁剪（``MAXLEN ~``）不保证
        裁到精确条数，所以契约里不能写「第 N 条一定被裁掉」这种断言 ——
        真要在 Redis 上跑，得用 ``XTRIM MAXLEN 0`` 精确裁。那条测试在 M3
        实现 XTRIM 之后再补，补的时候放回契约文件。
        """
        async with self.open_queue(claim_idle_ms=0, stream_maxlen_tasks=1) as q:
            await q.publish_task(task("run-1"))
            h, first = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert first.task_id == "run-1"

            # 再发一条，把上一条挤出 maxlen=1 的窗口 —— 等价于 XTRIM
            await q.publish_task(task("run-2"))

            assert await q.reclaim(WorkerType.SECURITY) == 1, "被裁掉的那条还在 PEL 里，所以会被回收"

            got = await collect(q.consume_tasks(WorkerType.SECURITY))
            assert [msg.task_id for _, msg in got] == ["run-2"], "墓碑不该被交出来，后面那条正常投递"
            for handle, _ in got:
                await handle.ack()

            assert await q.reclaim(WorkerType.SECURITY) == 0, "墓碑已经被 ack，不该再被回收"
            assert q.pending_count(WorkerType.SECURITY) == 0

            await h.ack()  # 幂等：它早就被回收并作废了，但 ack 不能报错

    async def test_a_message_trimmed_before_delivery_is_never_delivered(self) -> None:
        """还没投出去就被裁掉的消息，消费循环根本看不到它。

        maxlen 是「这个流最多记住几条」，超过的部分对**所有人**都消失了 ——
        包括还没读的消费者。这正是 CLAUDE.md 里那条「``review_results``
        在 M5 通过前不裁剪」的由来：裁剪会静默丢任务，而丢的方式是
        「这条任务从来不存在」。
        """
        async with self.open_queue(stream_maxlen_tasks=1) as q:
            for i in range(3):
                await q.publish_task(task(f"run-{i}"))
            got = await collect(q.consume_tasks(WorkerType.SECURITY))
            assert [msg.task_id for _, msg in got] == ["run-2"]

    # -- 死信内容（后端专属：契约只断言「死信同时是一次 ack」） -------------- #

    async def test_dead_letter_keeps_enough_context_to_debug(self) -> None:
        """死信里必须留下「什么失败了、为什么、第几次」。

        只留一句「失败了」等于没有死信 —— 而运维可见性正是死信**唯一**的用途
        （它不是完成机制，见约定 #2）。所以字段要够到「不用翻日志就能判断
        该不该手动重跑」。
        """
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
            await h.to_dead_letter("模型连续三次返回散文", ErrorClass.SCHEMA_UNRECOVERABLE)

            letters = q.dead_letters()
            assert len(letters) == 1
            letter = letters[0]
            assert letter["task_id"] == "run-1"
            assert letter["worker_type"] == "security"
            assert letter["error"] == "模型连续三次返回散文"
            assert letter["error_class"] == "schema_unrecoverable"
            assert letter["attempt"] == "1"
            assert letter["source_stream"] == STREAMS["tasks"]
            assert letter["failed_at"]
            # 原 payload 也要留一份：没有它，复盘时连「它当时想干什么」都不知道
            assert "run-1" in letter["payload"]

    async def test_dead_letters_are_listed_in_order(self) -> None:
        async with self.open_queue(claim_idle_ms=0) as q:
            for i in range(2):
                await q.publish_task(task(f"run-{i}"))
                h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
                await h.to_dead_letter(f"第 {i} 个失败", ErrorClass.TRANSIENT)
            assert [x["task_id"] for x in q.dead_letters()] == ["run-0", "run-1"]

    # -- 坏 payload（生产路径产生不了，只能注入） --------------------------- #

    async def test_a_poison_payload_is_dead_lettered_not_fatal(self) -> None:
        """解析不了的消息进死信并继续，而不是把消费循环带崩。

        真实的来路是**滚动发布**：新版本的 orchestrator 加了个字段，
        而还在跑的旧 Worker 的契约是 ``extra="forbid"`` —— 于是它读到的每一条
        都是「坏 payload」。

        如果这里抛异常，那个 Worker 会崩溃重启、再读到同一条、再崩 ——
        一个自造的崩溃循环，而日志里只有一条看不出因果关系的信息。

        注意这条消息的平铺字段是**合法**的：``worker_type=security`` 让过滤
        那一步放它过去，才会走到解析。这也顺带证明了平铺字段是有用的 ——
        没有它，过滤就得先解析，而解析正是这里唯一做不到的事。
        """
        async with self.open_queue() as q:
            # 故意走私有入口：正常的 publish_* 只会产出合法 payload，
            # 而这条测试要的正是「不可能由本进程产生」的输入。
            q._append(
                STREAMS["tasks"],
                {
                    "payload": '{"task_id": "run-x", "意料之外的字段": 1}',
                    "worker_type": "security",
                    "task_id": "run-x",
                },
            )
            await q.publish_task(task("run-ok"))

            got = await collect(q.consume_tasks(WorkerType.SECURITY))
            assert [msg.task_id for _, msg in got] == ["run-ok"], "坏消息之后的正常消息必须照常投递"

            letters = q.dead_letters()
            assert len(letters) == 1
            assert letters[0]["error_class"] == "schema_unrecoverable"
            assert "payload 无法解析" in letters[0]["error"]

    # -- 分组与积压口径 ----------------------------------------------------- #

    async def test_lag_counts_what_has_not_been_delivered(self) -> None:
        """``lag`` 是「还没投给这个组」，不是「还没 ack」。

        两者只在消费者卡住时才分叉，而那时前者 0、后者在涨 ——
        分清这个区别是排查「到底是没消息还是消费者死了」的唯一线索。
        """
        async with self.open_queue() as q:
            for i in range(3):
                await q.publish_task(task(f"run-{i}"))
            assert await q.lag(WorkerType.SECURITY) == 3

            h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert await q.lag(WorkerType.SECURITY) == 2, "投出去就不再算积压"
            assert q.pending_count(WorkerType.SECURITY) == 1, "而它还在 PEL 里"

            await h.ack()
            assert q.pending_count(WorkerType.SECURITY) == 0

    async def test_each_worker_type_has_its_own_lag(self) -> None:
        """三个组各自独立看待同一条流。"""
        async with self.open_queue() as q:
            await q.publish_task(task("run-1", worker_type=WorkerType.STYLE))
            assert await q.lag(WorkerType.STYLE) == 1
            assert await q.lag(WorkerType.PERFORMANCE) == 1, "风格任务对性能组同样是「未投递」"
            await collect(q.consume_tasks(WorkerType.PERFORMANCE))
            assert await q.lag(WorkerType.PERFORMANCE) == 0
            assert await q.lag(WorkerType.STYLE) == 1, "性能组读完不影响风格组"

    # -- 契约里用不到、但真实链路会用的组合 ---------------------------------- #

    async def test_bootstrap_and_result_streams_are_independent(self) -> None:
        """四条流互不串台。写错一个 stream 名的后果是「消息投进去了没人消费」，
        而它不会有任何报错。"""
        async with self.open_queue() as q:
            await q.publish_bootstrap(bootstrap())
            assert await collect(q.consume_results()) == []
            assert await collect(q.consume_tasks(WorkerType.SECURITY)) == []
            h, boot = await next_message(q.consume_bootstrap())
            assert boot.task_id == "01JTESTRUN0000000000000000"
            await h.ack()

    async def test_a_result_keeps_findings_intact(self) -> None:
        """结果里的 findings 必须原样穿过传输层。

        比对整个对象而不是挑几个字段：``line`` 会在 JSON 往返里变成数字、
        ``severity`` 是枚举、``confidence`` 走进浮点 —— 每一样都可能被
        「差不多就行」的序列化悄悄改掉，而它们全都会进评测报表。
        """
        async with self.open_queue() as q:
            f = finding(line=42, confidence=0.37)
            await q.publish_result(result("run-1", findings=[f]))
            h, got = await next_message(q.consume_results())
            assert got.findings == [f]
            assert got.findings[0].line == 42
            assert got.tokens_in == 1234
            await h.ack()
