"""``InMemoryLock`` 的测试。

锁只防重复劳动，不保证正确性（正确性靠数据库唯一约束）—— 但「释放了别人的锁」
这件事会**静默地**把互斥变回没有互斥。所以这里测的重点全部围绕持有者校验。
"""

from __future__ import annotations

import asyncio

import pytest

from sfly_bus.memory import InMemoryLock


async def _acquire(lock: InMemoryLock, key: str, ttl_ms: int = 60_000) -> bool:
    """在**另一个 Task** 里抢锁。持有者的身份是 Task，所以「别人来抢」这件事
    必须真的换个 Task 才能测到。"""
    return await lock.acquire(key, ttl_ms)


async def _release(lock: InMemoryLock, key: str) -> None:
    await lock.release(key)


@pytest.mark.unit
async def test_lock_is_exclusive_across_tasks() -> None:
    lock = InMemoryLock()
    assert await lock.acquire("run-1", 60_000) is True
    assert await asyncio.create_task(_acquire(lock, "run-1")) is False
    await lock.release("run-1")
    assert await asyncio.create_task(_acquire(lock, "run-1")) is True


@pytest.mark.unit
async def test_release_by_a_non_owner_is_refused() -> None:
    """**这条是这个类存在的理由。**

    A 拿了锁 → 超时 → B 拿到 → A 来释放。不做持有者校验的话，B 的锁就没了，
    于是两个执行体同时认为自己独占 —— 而症状是「偶尔重复处理同一件事」，
    既不报错也不稳定复现。
    """
    lock = InMemoryLock()
    assert await lock.acquire("run-1", 60_000) is True

    await asyncio.create_task(_release(lock, "run-1"))  # 别的 Task 来释放

    assert lock.held() == ["run-1"], "不是持有者就不能释放"
    assert await asyncio.create_task(_acquire(lock, "run-1")) is False


@pytest.mark.unit
async def test_an_expired_lock_can_be_taken_over_and_the_old_owner_cannot_release_it() -> None:
    """超时之后可以被别人接管，而**原持有者随后不能再释放它**。

    这两件事必须一起成立：只测「能接管」会漏掉「接管之后又被原持有者删掉」
    这条路径 —— 那正是把互斥悄悄变回没有互斥的那条路径。
    """
    lock = InMemoryLock()
    assert await lock.acquire("run-1", ttl_ms=0) is True
    assert lock.held() == [], "过期是惰性判定的：没人申请时不会有人去清理，但 held() 看得见"

    assert await asyncio.create_task(_acquire(lock, "run-1")) is True, "已过期的锁应该能被接管"

    await lock.release("run-1")  # 原持有者（当前 Task）来释放 —— 但持有者已经换人了
    assert lock.held() == ["run-1"]


@pytest.mark.unit
async def test_the_same_owner_cannot_acquire_twice() -> None:
    """**不可重入**，和 ``SET NX PX`` 一致。

    按 ``asyncio.Lock`` 的直觉用它会踩坑：同一个 Task 里嵌套着去拿同一把锁，
    第二次会直接失败 —— 而失败是返回值，不抛异常。
    """
    lock = InMemoryLock()
    assert await lock.acquire("k", 60_000) is True
    assert await lock.acquire("k", 60_000) is False


@pytest.mark.unit
async def test_different_keys_do_not_block_each_other() -> None:
    lock = InMemoryLock()
    assert await lock.acquire("run-1", 60_000) is True
    assert await lock.acquire("run-2", 60_000) is True
    assert lock.held() == ["run-1", "run-2"]


@pytest.mark.unit
async def test_release_of_an_unknown_key_is_a_noop() -> None:
    """释放一个从来没拿过的键不能报错 —— 关机路径上会走到这里。"""
    lock = InMemoryLock()
    await lock.release("从来没有人拿过")
    assert lock.held() == []


@pytest.mark.unit
async def test_close_clears_everything() -> None:
    lock = InMemoryLock()
    await lock.acquire("run-1", 60_000)
    await lock.close()
    assert lock.held() == []
