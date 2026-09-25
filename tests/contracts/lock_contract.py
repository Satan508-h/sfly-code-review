"""两种 ``Lock`` 实现**共用**的行为契约。

    tests/unit/bus/test_memory_lock.py          → InMemoryLock
    tests/integration/bus/test_redis_lock.py    → RedisLock

锁只防重复劳动，不保证正确性（正确性靠数据库唯一约束）—— 但「释放了别人的锁」
这件事会**静默地**把互斥变回没有互斥。所以下面每一条都围绕持有者校验。

### 两个实现必须用同一条「谁是持有者」的规则

``Lock`` 协议里 ``release(key)`` 没有 token 参数，所以实现只能自己认出持有者：
内存实现用当前 asyncio Task，Redis 实现也用当前 asyncio Task（而不是「整个进程
共享一个 token」）。两边规则一致，下面这些测试才能同时成立 —— 否则会出现
「同一个协程在内存模式下能释放、在 Redis 模式下不能」这种只在一种拓扑下复现的
差异，而它不会有任何报错。

### 这里**不测** ``close()``

内存实现的 ``close()`` 清空本地字典（等于「关掉就全放了」），Redis 实现只关连接、
远端键交给 TTL 过期（关机路径上多发一批释放命令，换来的只是少等几秒 TTL，
代价是关机可能卡在网络上）。这是一处**有意的**差异，所以它不进契约 ——
``close()`` 之后锁还在不在，是生命周期细节，不是两种拓扑都欠下的语义。
内存实现那条单独的测试留在它自己的文件里，并写明了为什么。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sfly_bus.base import Lock

#: 让 TTL 真正过去要等的时间。``ttl_ms=1`` 之后等这么久，两个实现上的
#: 「已过期」判定都不再有争议。
_EXPIRY_SLEEP_S = 0.02


async def _acquire(lock: Lock, key: str, ttl_ms: int = 60_000) -> bool:
    """在**另一个 Task** 里抢锁。

    持有者身份是 Task，所以「别人来抢 / 别人来释放」这件事必须真的换个 Task
    才能测到 —— 在当前 Task 里直接调，测的其实是「自己能不能重入」。
    """
    return await lock.acquire(key, ttl_ms)


async def _release(lock: Lock, key: str) -> None:
    await lock.release(key)


class LockContract[L: Lock]:
    """子类只需实现 ``make_lock``。"""

    async def make_lock(self, **kwargs: Any) -> L:
        raise NotImplementedError("子类必须实现 make_lock")

    @asynccontextmanager
    async def open_lock(self, **kwargs: Any) -> AsyncIterator[L]:
        lock = await self.make_lock(**kwargs)
        try:
            yield lock
        finally:
            await lock.close()

    async def test_lock_is_exclusive_across_tasks(self) -> None:
        async with self.open_lock() as lock:
            assert await lock.acquire("run-1", 60_000) is True
            assert await asyncio.create_task(_acquire(lock, "run-1")) is False
            await lock.release("run-1")
            assert await asyncio.create_task(_acquire(lock, "run-1")) is True

    async def test_release_by_a_non_owner_is_refused(self) -> None:
        """**这条是这两个类存在的理由。**

        A 拿了锁 → 超时 → B 拿到 → A 来释放。不做持有者校验的话，B 的锁就没了，
        于是两个执行体同时认为自己独占 —— 而症状是「偶尔重复处理同一件事」，
        既不报错也不稳定复现。

        断言用「另一个 Task 抢不到」而不是「内部还剩几条记录」：前者是契约
        （互斥还成立吗），后者是实现的记账方式。
        """
        async with self.open_lock() as lock:
            assert await lock.acquire("run-1", 60_000) is True

            await asyncio.create_task(_release(lock, "run-1"))  # 别的 Task 来释放

            assert await asyncio.create_task(_acquire(lock, "run-1")) is False, (
                "不是持有者就不能释放 —— 否则这里的互斥已经没了"
            )

    async def test_an_expired_lock_can_be_taken_over_and_the_old_owner_cannot_release_it(
        self,
    ) -> None:
        """超时之后可以被别人接管，而**原持有者随后不能再释放它**。

        这两件事必须一起成立：只测「能接管」会漏掉「接管之后又被原持有者删掉」
        这条路径 —— 那正是把互斥悄悄变回没有互斥的那条路径。
        """
        async with self.open_lock() as lock:
            assert await lock.acquire("run-1", ttl_ms=1) is True
            await asyncio.sleep(_EXPIRY_SLEEP_S)
            assert await asyncio.create_task(_acquire(lock, "run-1")) is True, "已过期的锁应该能被接管"

            await lock.release("run-1")  # 原持有者（当前 Task）来释放 —— 但持有者已经换人了

            assert await asyncio.create_task(_acquire(lock, "run-1")) is False, (
                "原持有者的释放把接管者的锁删掉了"
            )

    async def test_the_same_owner_cannot_acquire_twice(self) -> None:
        """**不可重入**，和 ``SET NX PX`` 一致。

        按 ``asyncio.Lock`` 的直觉用它会踩坑：同一个 Task 里嵌套着去拿同一把锁，
        第二次会直接失败 —— 而失败是返回值，不抛异常。
        """
        async with self.open_lock() as lock:
            assert await lock.acquire("k", 60_000) is True
            assert await lock.acquire("k", 60_000) is False

    async def test_different_keys_do_not_block_each_other(self) -> None:
        async with self.open_lock() as lock:
            assert await lock.acquire("run-1", 60_000) is True
            assert await lock.acquire("run-2", 60_000) is True

    async def test_release_of_an_unknown_key_is_a_noop(self) -> None:
        """释放一个从来没拿过的键不能报错 —— 关机路径上会走到这里。"""
        async with self.open_lock() as lock:
            await lock.release("从来没有人拿过")
