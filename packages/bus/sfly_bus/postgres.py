"""Postgres 连接池。**两种拓扑都要有它**，所以这里没有「内存实现」一说。

M0 只做连接生命周期与健康探测；M4 在同一个文件里加 ``PostgresRunStore``
（``RunStore`` 协议的那一整套实现）。

### 为什么是 psycopg v3，不是 asyncpg

LangGraph 的 ``AsyncPostgresSaver`` 用 psycopg。如果应用层用 asyncpg，
一个进程里就会有两个互不相干的连接池、两套超时语义、两份「连不上怎么办」
的逻辑 —— 排查线上问题时这是最容易浪费半天的那类分裂。

### 两处不显然的设置

**``check=AsyncConnectionPool.check_connection``**
Neon 免费版空闲 5 分钟主动挂起，且**关不掉**。池子里的连接因此会变成
「TCP 上还在、服务端已经不要了」的僵尸。``check`` 回调在**每次 ``getconn()``**
（不只是归还时）被调用，失败就把这条连接丢掉换一条新的。
代价是每次取连接多一次往返；不设的话，挂起后的第一次查询直接抛错给用户。

**``connect_timeout``**
libpq 默认是 0，意思是「交给操作系统」—— 在某些网络下表现为挂起数分钟。
显式给一个值，是为了让「Postgres 不可达」表现为一条 5 秒后的错误，
而不是一个永远不返回的请求。
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import psycopg
from psycopg_pool import AsyncConnectionPool

from sfly_bus.health import CheckResult, down, ok
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 健康探测取连接的等待上限。比 ``connect_timeout`` 短是刻意的：
#: 池子连不上时应该**快速**报 DOWN，而不是让健康接口跟着一起卡住。
#:
#: 注意这里等的是**池子里的空闲连接**，不是 TCP 连接。池子连不上数据库时
#: 后台填充协程会一直失败，于是这个等待会**走满**：实测对着一台不可达的
#: 数据库，``ping()`` 的耗时正好是这个值。所以它直接决定了「数据库挂了的时候
#: 健康页有多慢」，不是个可以随手调大的数字。
DEFAULT_PING_TIMEOUT_S = 5.0


class PostgresPool:
    """``psycopg_pool.AsyncConnectionPool`` 的薄封装。

    薄是刻意的：M4 的 ``PostgresRunStore`` 需要的是「拿到一条连接执行 SQL」，
    任何在这里多加的抽象层，到了写 SQL 的时候都会变成要绕过去的东西。
    """

    def __init__(
        self,
        conninfo: str,
        *,
        min_size: int = 1,
        max_size: int = 5,
        connect_timeout_s: int = 10,
        ping_timeout_s: float = DEFAULT_PING_TIMEOUT_S,
        name: str = "sfly",
    ) -> None:
        self.conninfo = conninfo
        self.connect_timeout_s = connect_timeout_s
        self.ping_timeout_s = ping_timeout_s
        #: 探测连接的超时（整秒）。见 ``_fetch_version``：必须严格小于
        #: ``ping_timeout_s``，否则「连不上」会被误报成「服务端挂起」。
        #:
        #: 取整是因为 libpq 的 ``connect_timeout`` 就是整秒，传浮点会被
        #: psycopg 拒绝（mypy 也拦得住）。下限 1 秒。
        self._probe_connect_timeout_s = max(1, round(ping_timeout_s * 0.6))
        self._pool = AsyncConnectionPool(
            conninfo,
            min_size=min_size,
            max_size=max_size,
            name=name,
            # 显式 open=False：3.2 起构造时隐式打开会发 DeprecationWarning，
            # 而且那样就没法在打开之前挂好事件循环相关的东西。
            open=False,
            # 见模块文档：Neon 免费版会挂起空闲连接，这是唯一的应对手段
            check=AsyncConnectionPool.check_connection,
            kwargs={"connect_timeout": connect_timeout_s},
        )

    # -- 生命周期 ---------------------------------------------------------- #

    async def open(self) -> None:
        """启动池子的后台填充协程。

        ``wait=False`` 是刻意的：**Postgres 不可达时进程也要能起来**。
        崩掉退出会让 Docker 的 ``restart: unless-stopped`` 把容器拖进
        无限重启循环 —— 而那会把真正的错误信息冲掉，只留下「容器在重启」。
        起来的进程能通过 ``/api/health`` 说出哪里坏了，这才可诊断。
        """
        await self._pool.open(wait=False)

    async def close(self) -> None:
        await self._pool.close()

    # -- 取连接 ------------------------------------------------------------ #

    def connection(self, timeout: float | None = None) -> Any:
        """``async with pool.connection() as conn:`` —— 转发给底层池子。

        直接转发而不包一层，是为了不挡住 ``conn.cursor()`` / ``conn.execute()``
        这些 M4 要用到的东西。
        """
        return self._pool.connection(timeout=timeout)

    # -- 健康 -------------------------------------------------------------- #

    async def ping(self) -> CheckResult:
        """探测连通性并回报服务端版本。

        连不上**不抛异常** —— 返回 ``status=down`` 的结果。健康检查自己崩掉
        是最没有价值的一种失败：调用方本来就是为了知道「坏没坏」才调它的。

        **这里刻意不走连接池**，有两条理由，第二条是实测踩出来的：

        1. 探测问的是「PostgreSQL 现在可达吗」，而不是「我这个连接池还好吗」。
           新开一条连接测的才是前者，也正是一个新 Worker 启动时会遇到的情况。
           （和 ``RedisStreamsQueue.ping()`` 是同一个设计，那里的论证更完整。）
        2. ``docker pause``（或任何让服务端挂起、TCP 仍在的场景）下，
           ``SHOW server_version`` 会**永远阻塞**：``pool.connection(timeout=N)``
           只限制「等池子分配连接」的时间，一旦拿到连接，后面的查询没有任何超时。
           实测这一条会让 ``/api/health`` 直接挂死 —— 而 ``docker pause`` 正是
           项目文档里承诺要演示的场景之一。

        走独立连接 + ``asyncio.timeout`` 之后，硬上限是真的成立的：
        超时取消的是一条马上要销毁的连接，不影响池子里的任何东西。
        """
        t0 = time.perf_counter()
        try:
            version = await asyncio.wait_for(self._fetch_version(), timeout=self.ping_timeout_s)
        # 捕获一切是这里的**目的**，不是偷懒：探测函数抛异常等于把
        # 「依赖坏了」变成「健康接口也坏了」，调用方就再也问不出话来了。
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000
            log.warning(
                "postgres.ping_failed",
                error=str(exc),
                error_class=type(exc).__name__,
                timeout_s=self.ping_timeout_s,
            )
            return down("postgres", _describe(exc, self.ping_timeout_s), latency)

        latency = (time.perf_counter() - t0) * 1000
        return ok("postgres", f"PostgreSQL {version}", latency)

    async def _fetch_version(self) -> str:
        """开一条一次性连接，读服务端版本，关掉。

        两道防线对应两种不同的故障：

        * ``connect_timeout`` 挡「连不上」——连接被拒绝、DNS 失败、路由不可达。
        * 外面的 ``asyncio.wait_for`` 挡「连上了但服务端不吭声」——
          ``docker pause``、网络分区。

        两条防线的时间关系**必须**是「内层严格小于外层」，否则第二种故障会
        冒充第一种：连接尝试卡到整体超时，报出来的是「服务端挂起」，
        而真实原因可能只是「端口没开」。这个 bug 是测试抓出来的 ——
        默认配置曾经是 ping 5 秒 / connect 10 秒，正好反过来。

        所以内层不从 ``connect_timeout_s`` 取值（那是给连接池用的 libpq 参数），
        而是从探测自己的预算里切 60%：留 40% 给拿到连接后的那次查询往返。
        """
        conn = await psycopg.AsyncConnection.connect(
            self.conninfo, connect_timeout=self._probe_connect_timeout_s
        )
        try:
            cur = await conn.execute("SHOW server_version")
            row = await cur.fetchone()
            return str(row[0]) if row else "?"
        finally:
            # 关连接自己也可能抛（服务端已经没了），不能让它覆盖掉上面的结果
            with contextlib.suppress(Exception):
                await conn.close()


def _describe(exc: BaseException, timeout_s: float) -> str:
    """把异常拼成一句能读的说明。

    两种情况的原文都很难懂：

    * ``PoolTimeout`` —— ``couldn't get a connection after 5.00 sec``。
      看不出是「池子被占满了」还是「数据库根本连不上」，而这两种的处置方式
      完全不同（前者调 ``DB_POOL_MAX``，后者去查数据库）。
    * ``asyncio.TimeoutError`` —— ``str()`` 是**空字符串**。直接拼进健康页会得到
      ``TimeoutError:`` 后面什么都没有，看起来像 bug。它对应的场景恰恰是
      "连上了但服务端不回应"（``docker pause``），值得说清楚。
    """
    if type(exc).__name__ == "PoolTimeout":
        return f"PostgreSQL {timeout_s:g}s 内没有可用连接（数据库不可达，或 DB_POOL_MAX 太小）"
    if isinstance(exc, TimeoutError):
        return f"PostgreSQL {timeout_s:g}s 内没有响应（服务端挂起 / 网络分区，而非拒绝连接）"
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else f"{type(exc).__name__}（无附加信息）"
