"""LangGraph 的 Postgres checkpointer —— **它必须有自己的连接池**。

### 为什么不能共用 ``sfly_bus.PostgresPool``

两个理由，第二个是硬的：

**1. 事务语义不同。** 我们的池是非 autocommit 的，仓储里每一处写入都显式
``async with conn.transaction()``。而 checkpointer 的写入路径是一串裸
``conn.execute()``（``_cursor()`` 里没有 transaction 块）—— 它**依赖连接的
autocommit**。把非 autocommit 的连接给它，那些写入会停在一个永不提交的事务里：
图能跑、日志正常、状态读出来也是对的（同一个连接看得见自己的未提交数据），
然后进程一重启，checkpoint 全没了。

**2. ``setup()`` 里有 ``CREATE INDEX CONCURRENTLY``。**
Postgres **禁止**它出现在事务块里 —— 而 psycopg 在非 autocommit 连接上执行的
每一条语句都在一个隐式事务里。所以就算解决了第 1 条，建表那一步也会直接报
``CREATE INDEX CONCURRENTLY cannot run inside a transaction block``。

这也解释了 LangGraph 自己的 ``from_conn_string`` 为什么写死了
``autocommit=True, prepare_threshold=0, row_factory=dict_row``。我们不用它
（它只给一条裸连接，活不过 Neon 的 5 分钟空闲挂起），但把那三个参数照搬过来。

### ``prepare_threshold=0``

关掉服务端预编译语句。Neon 的**连接池端点**（pgbouncer，transaction 模式）
不支持 prepared statements，而 Node 侧报出来的错是
``prepared statement "s0" already exists`` —— 看起来像并发 bug，其实是端点类型不对。
本地直连时这个设置只是让每条语句多一次解析，代价可以忽略。

### 独立池的代价

多一个池就多几条连接。所以 ``max_size`` 给得很小（``CHECKPOINTER_POOL_MAX``）：
checkpointer 自己有一把 ``asyncio.Lock`` 串行化所有 ``_cursor()``，
它天然不需要高并发。Neon 免费版总共 10 条连接，这里多占 2 条是能接受的，
再多就要动 ``DB_POOL_MAX`` 了。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: checkpointer 自己的池子上限。见模块文档最后一段。
CHECKPOINTER_POOL_MAX = 2

#: 建表（``saver.setup()``）等连接的上限。和 ``MIGRATE_TIMEOUT_S`` 同一个理由：
#: 这是启动路径，数据库不可达时必须**有上界地**失败，而不是让容器在
#: lifespan 里默默卡 30 秒。
SETUP_TIMEOUT_S = 10.0


@asynccontextmanager
async def open_checkpointer(dsn: str, *, connect_timeout_s: int = 10) -> AsyncIterator[AsyncPostgresSaver]:
    """开一个属于 checkpointer 的连接池，包成 ``AsyncPostgresSaver``。

    池子用 ``open(wait=False)``：**数据库不可达时进程也要能起来** ——
    这是 M0 定下的规矩（见 ``PostgresPool.open``），checkpointer 不该破例。
    """
    # 显式写出类型参数：行工厂是在 ``kwargs`` 里给的，mypy 推不出连接的行类型，
    # 于是 ``AsyncPostgresSaver`` 会因为「池子的行类型是 tuple」而拒绝它。
    pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]] = AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=CHECKPOINTER_POOL_MAX,
        name="sfly-checkpointer",
        open=False,
        # Neon 会挂起空闲连接，而 checkpointer 的连接**偏偏是最空闲的** ——
        # 一个 run 只在几个瞬间写 checkpoint，中间可能空几分钟。
        check=AsyncConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "connect_timeout": connect_timeout_s,
        },
    )
    await pool.open(wait=False)
    try:
        yield AsyncPostgresSaver(pool)
    finally:
        # 关在 finally 里：停机、异常、取消三条路径都要走到。
        # 吞异常的理由和 ``Dependencies.close`` 一样 —— 关机路径上抛错
        # 只会让进程带着非零码退出，而那时 Docker 已经在拆容器了。
        with contextlib.suppress(Exception):
            await pool.close()


async def setup_on_startup(saver: AsyncPostgresSaver) -> None:
    """建 checkpoint 的表（幂等）。**和 ``migrate_on_startup`` 同一套处置策略。**

    * 连不上 → 记一条 error 继续（进程要能起来，在 ``/api/health`` 里说话）
    * 其它错误 → 也继续，但**要说清楚后果**：checkpointer 不可用意味着
      ``wait`` 节点的 ``interrupt()`` 无法落盘，图会在第一次挂起时失败。
      这比一个起不来的容器容易诊断得多。

    checkpoint 的表**不归我们的迁移器管**（LangGraph 自己维护
    ``checkpoint_migrations`` 那张版本表）。所以这里建不出来时，
    ``python tasks.py tables`` 是看不出问题的 —— 那句话得由这条日志来说。
    """
    try:
        await asyncio.wait_for(saver.setup(), timeout=SETUP_TIMEOUT_S)
        log.info("checkpointer.ready")
    except Exception as exc:
        log.error(
            "checkpointer.setup_failed",
            error=str(exc),
            error_class=type(exc).__name__,
            hint="checkpoint 表没建好 —— 图的 interrupt() 会失败。依赖恢复后重启本容器。",
        )
