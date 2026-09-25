"""``RedisStreamsQueue`` 的测试 —— **同一份契约在真 Redis 上再跑一遍**。

    tests/unit/bus/test_memory_queue.py       → InMemoryQueue（无 Docker）
    tests/integration/bus/test_redis_queue.py → RedisStreamsQueue（真 Redis）

这个文件里的两类东西分开看：

* 继承自 :class:`QueueContract` 的那一批 —— 与内存实现共享**同一份**代码，
  一行断言都没有为 Redis 放宽
* 下面 Redis 专属的那一批 —— 要么是内存实现**根本做不到**的事（跨副本回收），
  要么是只能用原生命令构造出来的输入（手工 ``XADD`` 一条坏 payload）

跑之前需要有 Redis：``python tasks.py up redis``，测试用 db 15。
"""

from __future__ import annotations

from typing import Any

import pytest

from contracts.queue_contract import (
    QueueContract,
    collect,
    next_message,
)
from factories import task
from redis_support import raw_client, redis_test_url
from sfly_bus.base import STREAMS
from sfly_bus.redis_streams import RedisStreamsQueue
from sfly_shared.contracts import WorkerType

#: 整个文件都是集成测试。``pyproject.toml`` 的 addopts 默认排除 integration，
#: 所以要跑它必须显式 ``python tasks.py test-int``（或 ``-m integration``）。
pytestmark = pytest.mark.integration


class TestRedisStreamsQueue(QueueContract[RedisStreamsQueue]):
    async def make_queue(self, **kwargs: Any) -> RedisStreamsQueue:
        q = RedisStreamsQueue(redis_test_url(), **kwargs)
        # 必须 start()：消费者组在那一刻建出来（XGROUP CREATE ... $），
        # 而建组时游标设在流尾。漏了它，第一次读会报 NOGROUP。
        await q.start()
        return q

    # -- 跨副本回收：内存实现做不到的那件事 -------------------------------- #

    async def test_a_peer_replica_reclaims_a_dead_consumers_message(self) -> None:
        """**这条是「docker kill 一个 Worker，run 照样跑完」那个演示的机制本身。**

        两个队列对象 = 两条连接 = 两个 client_name，等价于两个容器。一个拿走了
        消息、在 LLM 调用中间被杀掉（不 ack、直接消失）；另一个 ``XAUTOCLAIM``
        把它捞回来重跑，重跑时 ``attempt`` 是 2。

        内存实现**做不出这条测试**：它的队列活在进程里，进程死了队列就没了。
        「Worker 猝死」在精简模式下根本不存在（单进程重启 = 全部重来），
        只有 Redis 这一侧才需要证明它可恢复 —— 而它必须真的可恢复，
        否则 ``--scale`` 就只是个摆设。
        """
        async with (
            self.open_queue(claim_idle_ms=0, client_name="sfly-replica-a") as dead,
            self.open_queue(claim_idle_ms=0, client_name="sfly-replica-b") as alive,
        ):
            await dead.publish_task(task("run-1"))
            h, first = await next_message(dead.consume_tasks(WorkerType.SECURITY))
            assert h.attempt == 1
            # 这里「杀掉」这个消费者：不 ack，也不 close —— 消息留在它的 PEL 里

            assert await alive.reclaim(WorkerType.SECURITY) == 1

            consumers = await alive.client.xinfo_consumers(STREAMS["tasks"], "security-group")
            names = {c["name"] for c in consumers}
            # 两边都还挂在 PEL 上，所以两个消费者名都该看得见：
            # 一个是死掉的副本（消息正是从它手里被抢回来的），一个是接手的新副本。
            # 这一对名字就是 `XINFO CONSUMERS` 里那份「谁在干活」的现场。
            assert any("sfly-replica-a" in n for n in names), names
            assert any("sfly-replica-b" in n for n in names), names

            h2, again = await next_message(alive.consume_tasks(WorkerType.SECURITY))
            assert again.task_id == first.task_id
            assert h2.attempt == 2, "重跑必须能看出这是第二次，否则死信判定没有依据"
            await h2.ack()

    async def test_each_replica_gets_its_own_consumer_name(self) -> None:
        """两个副本必须产生两个消费者名。

        名字撞了的后果不是「出错」，而是 ``--scale worker-security=3`` 那个演示
        变成一行输出 —— 而那正是要证明的东西。容器里 pid 恒等于 1，
        所以副本之间的区分只能靠主机名（Docker 拿容器 id 的前 12 位当 hostname）。
        """
        async with (
            self.open_queue(client_name="sfly-replica-x") as a,
            self.open_queue(client_name="sfly-replica-y") as b,
        ):
            await a.publish_task(task("run-1"))
            ha, _ = await next_message(a.consume_tasks(WorkerType.SECURITY))
            await ha.ack()
            await b.publish_task(task("run-2"))
            hb, _ = await next_message(b.consume_tasks(WorkerType.SECURITY))
            await hb.ack()

            consumers = await a.client.xinfo_consumers(STREAMS["tasks"], "security-group")
            names = {c["name"] for c in consumers}
            assert len(names) == 2, f"两个副本应该有两个消费者名：{names}"

    # -- 只能用原生命令构造的输入 ------------------------------------------ #

    async def test_a_poison_payload_is_dead_lettered_not_fatal(self) -> None:
        """坏 payload 进死信、消费循环继续 —— 与内存实现逐字段同义。

        真实的来路是**滚动发布**：新版本的 orchestrator 加了个字段，而还在跑的
        旧 Worker 的契约是 ``extra="forbid"``。

        这里伸手进原生客户端，因为 ``publish_task`` 只会产出合法 payload，
        而这条测试要的正是「不可能由本进程产生」的输入。
        """
        async with self.open_queue() as q:
            raw = raw_client()
            try:
                raw.xadd(
                    STREAMS["tasks"],
                    {
                        "payload": '{"task_id": "run-x", "意料之外的字段": 1}',
                        "worker_type": "security",
                        "task_id": "run-x",
                    },
                )
            finally:
                raw.close()

            await q.publish_task(task("run-ok"))

            got = await collect(q.consume_tasks(WorkerType.SECURITY))
            assert [msg.task_id for _, msg in got] == ["run-ok"], "坏消息之后的正常消息必须照常投递"

            letters = await q.dead_letters()
            assert len(letters) == 1
            assert letters[0]["error_class"] == "schema_unrecoverable"
            assert "payload 无法解析" in letters[0]["error"]

    async def test_an_entry_without_a_payload_is_acked_never_delivered(self) -> None:
        """空 payload 的条目：ack 掉、继续，**绝不交给调用方**。

        真实来路是「消息被裁掉或删除，而它已经被投给了某个消费者」—— 这时
        PEL 里剩下一个孤立的 id，读出来就是一个没有 payload 的条目。

        它和坏 payload 的处理**刻意不同**：坏 payload 进死信（它有可能是别的东西
        发错了），空 payload 只留一条 warning —— 它没有任何可复盘的内容。
        """
        async with self.open_queue() as q:
            raw = raw_client()
            try:
                raw.xadd(STREAMS["tasks"], {"worker_type": "security", "task_id": "run-x"})
            finally:
                raw.close()

            await q.publish_task(task("run-ok"))

            got = await collect(q.consume_tasks(WorkerType.SECURITY))
            assert [msg.task_id for _, msg in got] == ["run-ok"]
            assert await q.dead_letters() == [], "空 payload 不该进死信"
            assert await q.pending_count(WorkerType.SECURITY) == 1, "只剩正常那条还在 PEL 里"

    # -- Redis 侧的运维细节 ------------------------------------------------ #

    async def test_a_trimmed_entry_hangs_in_the_pel_until_a_reclaim_purges_it(self) -> None:
        """被裁掉的条目会**悬在 PEL 上**，直到下一次 ``reclaim()`` 把它清掉。

        这条把 Redis 的真实机制钉住（实测 7.4）：``XTRIM`` **不动 PEL** ——
        裁完之后 ``XPENDING`` 里那条还在，只是它的条目已经不在流里了。真正把
        它摘掉的是 ``XAUTOCLAIM``：悬空的 id 被从 PEL 里删除，并在返回值的
        第三段里报出来（``min-idle-time`` 多大都不影响这件事，它不会因为它们
        「不够空闲」而留下它们）。

        所以这一刻 ``reclaim()`` 清理的是**垃圾**而不是重投消息，返回值
        （可重投条数）是 0；内存实现在裁剪那一刻就把 PEL 清了，中间状态不同、
        结果相同。**留这条测试的直接原因值得写下来**：我最初把「XTRIM 之后
        XPENDING 少了一条」当成了 XTRIM 干的，照着这个理解去写了内存实现 ——
        而那个读数是同一段脚本里紧跟着的 XAUTOCLAIM 造成的。这类错误没有报错、
        没有异常，只有一条会红的测试能挡住它。它现在就在这儿。
        """
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert await q.pending_count(WorkerType.SECURITY) == 1

            assert await q.trim(STREAMS["tasks"], 0) >= 1

            assert await q.pending_count(WorkerType.SECURITY) == 1, "XTRIM 不碰 PEL"
            assert await q.reclaim(WorkerType.SECURITY) == 0, "悬空的 id 不是「可重投的消息」"
            assert await q.pending_count(WorkerType.SECURITY) == 0, "回收的时候才清掉"
            await h.ack()  # 幂等：它的条目早就没了

    async def test_the_attempt_counter_always_carries_a_ttl(self) -> None:
        """``sfly:attempts:*`` 必须有过期时间。

        一条消息一个键，而消息是**无界**的（每天多少个 PR 就有多少条）。没有 TTL
        的话 Redis 的内存会被一个只增不减的键空间慢慢吃掉，而症状要几个月后
        才出现 —— 到那时没人会想到是这里。
        """
        async with self.open_queue(claim_idle_ms=0) as q:
            await q.publish_task(task("run-1"))
            h, _ = await next_message(q.consume_tasks(WorkerType.SECURITY))
            assert h.attempt == 1

            keys = await q.client.keys("sfly:attempts:*")
            assert len(keys) == 1, f"应该只有一条消息的计数器：{keys}"
            assert await q.client.ttl(keys[0]) > 0, "计数器必须带 TTL"
            await h.ack()

    async def test_a_restarted_replica_does_not_replay_the_previous_run(self) -> None:
        """建组时游标在**流尾**（``XGROUP CREATE ... $``）：历史消息不回放。

        这就是 ``id="$"`` 而不是 ``id="0"`` 的全部意义。写错的后果是「每次重启
        Worker 都把之前跑过的任务重跑一遍」—— 它不报错、不丢数据，只是慢慢烧钱，
        而且因为结果是幂等的，连数据都看不出异常。

        要构造这个场景，得先有一个「流里已经有数据、而组还没建」的时刻，这在正常
        API 路径下不可能发生（``start()`` 总是先于任何发布）。所以这里刻意伸手
        进原生客户端把组删掉，把那个时刻造出来。
        """
        async with self.open_queue() as q:
            await q.publish_task(task("run-old"))
            topic = STREAMS["tasks"]

            raw = raw_client()
            try:
                raw.xgroup_destroy(topic, "security-group")
            finally:
                raw.close()

            # 新副本起来，会把组建在流尾 —— 于是那条「上一个 run 的任务」不会被回放
            async with self.open_queue() as fresh:
                assert await collect(fresh.consume_tasks(WorkerType.SECURITY)) == []
