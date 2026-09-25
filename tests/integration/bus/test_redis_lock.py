"""``RedisLock`` 的测试 —— 同样一份契约，对着真 Redis 再跑一遍。

    tests/unit/bus/test_memory_lock.py       → InMemoryLock
    tests/integration/bus/test_redis_lock.py → RedisLock

上面大多继承自 :class:`LockContract`。下面 Redis 专属的那几条测的是
**内存实现原理上做不到**的事：两个 ``RedisLock`` 对象是两条连接、两个进程，
而进程内的锁根本不存在「另一个进程」这个概念。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from contracts.lock_contract import LockContract
from redis_support import raw_client, redis_test_url
from sfly_bus.redis_streams import RedisLock

pytestmark = pytest.mark.integration


class TestRedisLock(LockContract[RedisLock]):
    async def make_lock(self, **kwargs: Any) -> RedisLock:
        lock = RedisLock(redis_test_url(), **kwargs)
        # 必须 start()：建连接、注册那段释放用的 Lua（EVALSHA 需要先有脚本）。
        await lock.start()
        return lock

    # -- 跨进程互斥：内存实现做不到的那件事 -------------------------------- #

    async def test_two_instances_are_mutually_exclusive(self) -> None:
        """两个 ``RedisLock`` 对象等价于两个进程 —— 这才是锁真正要解决的问题。

        内存实现的互斥只在单进程内成立（它甚至不能跨进程用，见 ``InMemoryQueue``
        模块文档）；而 webhook 去重、唤醒选举这两处**必须**跨容器互斥，
        否则 ``--scale api=2`` 之后同一份 webhook 会被两个副本各处理一遍。

        注意这条测试不依赖「谁先谁后」的时序运气：B 的失败是 Redis 的
        ``SET NX`` 判的，不是本地状态判的。
        """
        async with (
            self.open_lock(client_name="sfly-lock-a") as a,
            self.open_lock(client_name="sfly-lock-b") as b,
        ):
            assert await a.acquire("run-1", 60_000) is True
            assert await b.acquire("run-1", 60_000) is False, "另一个进程不该拿到同一个键"

            await a.release("run-1")
            assert await b.acquire("run-1", 60_000) is True
            await b.release("run-1")

    async def test_a_foreign_holders_lock_survives_our_release(self) -> None:
        """键是别人拿的，我们去 ``release`` —— 必须什么都不做。

        契约里那条（换一个 Task 释放）测的是「同进程内的持有者校验」，
        这条测的是**跨进程**那一层：本地根本没有这个键的记录，于是
        ``_held`` 查不到、直接返回。要是实现写成「查不到就 DEL 一下试试」，
        这里就会把 B 的锁删掉 —— 而 B 完全不知道，两个进程同时认为自己独占。
        """
        async with (
            self.open_lock(client_name="sfly-lock-a") as a,
            self.open_lock(client_name="sfly-lock-b") as b,
        ):
            assert await a.acquire("run-1", 60_000) is True

            await b.release("run-1")  # B 从没拿过这个键

            assert await b.acquire("run-1", 60_000) is False, "A 的锁被 B 释放掉了"
            await a.release("run-1")

    async def test_an_expired_lock_actually_leaves_redis(self) -> None:
        """TTL 是 Redis 自己执行的 —— 进程崩了锁也会自己走。

        这是锁和数据库唯一约束的关键区别，也是为什么**锁不能当正确性机制**：
        它会过期、会随 Redis 重启消失（见 ``Lock`` 协议的说明）。
        这里把它测出来，是因为「以为 SETNX 就是幂等保证」是个很常见的误解。

        用另一个客户端去读键（而不是伸手进锁的内部）：要证明的正是
        「**在 Redis 里**它没了」，而不是「这个对象觉得它没了」。

        时间余量给得很宽（TTL 150ms、等 350ms），并且先 ping 一次预热连接：
        这条测试第一版用的是 30ms/100ms，单跑必过、整层跑起来偶尔会红 ——
        因为那次读键要现建一条 TCP 连接，而这一点开销就够让 30ms 的 TTL 到期。
        「单跑绿、全跑红」的测试比没有测试更耗人。
        """
        raw = raw_client()
        try:
            raw.ping()  # 先把连接建好，别把建连接的时间算进 TTL 窗口里
            async with self.open_lock() as lock:
                assert await lock.acquire("run-1", ttl_ms=150) is True
                assert raw.get("run-1") is not None

                await asyncio.sleep(0.35)  # 让 TTL 真的过去

                assert raw.get("run-1") is None, "键应该已经自己过期了"
                assert lock.held() == [], "本地记账也要认这件事，否则排查时看到的是一份假账"
        finally:
            raw.close()
