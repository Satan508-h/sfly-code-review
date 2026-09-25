"""``InMemoryLock`` 的测试。

上面大半（继承自 :class:`LockContract`）**不是内存实现专属的** —— 同一份契约
在 ``tests/integration/bus/test_redis_lock.py`` 里对着真 Redis 再跑一遍。

这里只留两样东西：后端装配，以及内存实现**能直接观察、而 Redis 观察不到**的
记账细节（``held()`` 与 ``close()`` 的语义）。后者见契约文件里那段说明。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from contracts.lock_contract import LockContract
from sfly_bus.memory import InMemoryLock


class TestInMemoryLock(LockContract[InMemoryLock]):
    async def make_lock(self, **kwargs: Any) -> InMemoryLock:
        return InMemoryLock()


@pytest.mark.unit
async def test_held_lists_what_this_process_holds() -> None:
    """``held()`` 是给测试和排查用的自省接口（不在 Protocol 里）。

    Redis 实现有一个同名同义的方法，但它只能看到**本进程**拿过的键 ——
    这也是内存实现的天然口径（内存实现里不存在「别的进程」）。
    """
    lock = InMemoryLock()
    assert await lock.acquire("run-1", 60_000) is True
    assert await lock.acquire("run-2", 60_000) is True
    assert lock.held() == ["run-1", "run-2"]

    await lock.release("run-1")
    assert lock.held() == ["run-2"]


@pytest.mark.unit
async def test_an_expired_lock_is_invisible_to_held() -> None:
    """过期是**惰性**判定的：没有后台定时器，没人申请就没人清理。

    但 ``held()`` 必须看得见这件事 —— 否则排查时看到的是一份「还在持有」的
    假账，而它和真实状态已经分叉了。
    """
    lock = InMemoryLock()
    assert await lock.acquire("run-1", ttl_ms=0) is True
    assert lock.held() == []


@pytest.mark.unit
async def test_close_clears_everything() -> None:
    """内存实现的 ``close()`` 清空本地记账 —— 进程内的锁没有别的地方可活。

    **这处是两种实现有意的差异**，所以它不在契约里：Redis 实现的 ``close()``
    只关连接，远端键交给 TTL 过期（关机路径上多发一批释放命令，换来的只是
    少等几秒 TTL，代价是关机可能卡在网络上）。对调用方的实际影响是一样的：
    ``release()`` 在 ``close()`` 之后都只是什么都不做。
    """
    lock = InMemoryLock()
    await lock.acquire("run-1", 60_000)
    await lock.close()
    assert lock.held() == []
    await lock.release("run-1")  # 关掉之后释放不能报错


@pytest.mark.unit
async def test_a_child_task_cannot_release_its_parents_lock() -> None:
    """内存实现的持有者是 **Task**，所以子协程释放不了父协程拿的锁。

    这不是 bug 而是规则（Redis 实现用同一条规则，见契约文件）。写下来是因为
    它会让人意外：`create_task` 一下再释放，看起来完全正常，而锁纹丝不动。
    """
    lock = InMemoryLock()
    assert await lock.acquire("run-1", 60_000) is True

    await asyncio.create_task(_release(lock, "run-1"))

    assert lock.held() == ["run-1"]


async def _release(lock: InMemoryLock, key: str) -> None:
    await lock.release(key)
