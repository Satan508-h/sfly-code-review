"""**全代码库唯一允许读 ``QUEUE_BACKEND`` / ``LOCK_BACKEND`` 的文件。**

    grep -rn "QUEUE_BACKEND" --include=*.py .    # 必须只返回这一个文件

这条约定是整个项目的中心论点（「一套代码、两种拓扑」）能不能成立的地方。
一旦某个节点或 Worker 开始判断「我用的是不是 Redis」，两种拓扑就跑在不同的
代码路径上，共用代码这件事从「事实」退化成「宣传」。

### 靠结构而不是靠自觉

``Dependencies.probe()`` **不知道**自己手里是哪一种队列 —— 它只拿到一串
由本文件装配好的探测函数，挨个调用。调用方拿不到任何可以拿来分支的信息，
所以想违反约定也无从下手。

这比「写条注释提醒大家别判断」可靠得多。约定靠注释维持，就一定会漂移。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from sfly_bus.health import CheckResult, HealthReport, down, skipped
from sfly_bus.postgres import PostgresPool
from sfly_bus.redis_streams import RedisStreamsQueue
from sfly_shared.config import Settings, get_settings
from sfly_shared.logging import get_logger

log = get_logger(__name__)

Probe = tuple[str, Callable[[], Awaitable[CheckResult]]]
"""``(名字, 探测函数)``。名字显式给出，不去从函数 ``__name__`` 里猜 ——
探测函数崩掉时正是最需要准确名字的时候，而猜名字的代码在那种时刻最不该出错。"""


@dataclass
class Dependencies:
    """一次服务生命周期内持有的依赖句柄。

    调用方只该用两个接口：``probe()`` 和 ``close()``。
    ``postgres`` / ``queue`` 是给业务代码用的，**类型是 Protocol** ——
    ``wait`` 节点不知道也不需要知道 ``queue`` 背后是 Redis 还是 asyncio 队列。
    """

    postgres: PostgresPool
    """两种拓扑都有。``RunStore`` 的实现挂在这上面（M4）。"""

    queue: object | None = None
    """``TaskQueue`` 实现。完整模式是 :class:`RedisStreamsQueue`，
    精简模式是 ``InMemoryQueue``（M2 交付）。

    这里标成 ``object`` 而不是 ``TaskQueue`` 是暂时的：M0 的 ``RedisStreamsQueue``
    还没实现协议里的方法，标成 ``TaskQueue`` 会让 mypy 正确但**没有意义地**报错。
    M3 补齐后改回 ``TaskQueue | None``。 —— 见 TODO(M3)
    """

    _probes: list[Probe] = field(default_factory=list, repr=False)

    async def probe(self) -> HealthReport:
        """并发探测全部依赖。

        并发而不是串行：一个依赖挂掉时，串行版本会让健康接口的响应时间
        变成一个依赖的超时乘以依赖个数。并发时最坏就是单个超时。

        ``return_exceptions=True`` 是必需的 —— ``asyncio.gather`` 默认遇到第一个
        异常就往上抛，那会让「Redis 挂了」表现为健康接口本身 500，
        而不是一条 ``redis: down`` 的结果。探测函数内部已经各自吞掉异常了，
        这里是第二道保险，防的是将来有人新写的探测函数忘了吞。
        """
        if not self._probes:
            return HealthReport([])
        results = await asyncio.gather(*(p() for _, p in self._probes), return_exceptions=True)
        checks: list[CheckResult] = []
        for (name, _), r in zip(self._probes, results, strict=True):
            if isinstance(r, BaseException):
                log.exception("health.probe_crashed", dep=name, error=str(r))
                checks.append(down(name, f"{type(r).__name__}: {r}"))
            else:
                checks.append(r)
        return HealthReport(checks)

    async def close(self) -> None:
        """释放连接。**先关队列再关数据库** —— 队列的后台回收协程可能会写库，
        反过来关会让它在最后几秒里对着一个已关闭的池子报错。

        全程吞异常：关机路径上抛错只会让进程带着非零码退出，
        而这时候 Docker 已经在拆容器了，那条错误没人看得到。
        """
        if self.queue is not None:
            with contextlib.suppress(Exception):
                await self.queue.close()  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            await self.postgres.close()


async def open_dependencies(settings: Settings | None = None) -> Dependencies:
    """按当前拓扑打开依赖。**这是两种模式唯一的汇合点。**

    完整模式（``QUEUE_BACKEND=redis``）：Postgres + Redis。
    精简模式（``QUEUE_BACKEND=memory``）：只有 Postgres，没有 Redis 实例可开。

    两种模式下 Postgres 都是硬依赖 —— ``wait`` 节点的屏障查询、幂等性的
    唯一约束、SSE 的事件日志、恢复逻辑的超时扫描都落在上面。所以这里没有
    「Postgres 可选」的分支。
    """
    s = settings or get_settings()

    postgres = PostgresPool(
        s.database_url,
        min_size=s.db_pool_min,
        max_size=s.db_pool_max,
        connect_timeout_s=s.db_connect_timeout_s,
        ping_timeout_s=s.db_ping_timeout_s,
    )
    await postgres.open()
    probes: list[Probe] = [("postgres", postgres.ping)]

    queue: object | None = None

    if s.queue_backend == "redis":
        # client_name 会出现在 `redis-cli client list` 里 ——
        # `--scale worker-security=3` 之后能一眼数出三个副本。
        rq = RedisStreamsQueue(s.redis_url, client_name=_client_name(s))
        await rq.start()
        queue = rq
        probes.append(("redis", rq.ping))

        if s.lock_backend != "redis":
            # 只有队列在 Redis 上、锁却在进程内，意味着跨副本互斥失效。
            # 代码是对的（工厂按 lock_backend 装配），但配置几乎肯定是错的。
            log.warning(
                "config.lock_backend_mismatch",
                queue_backend=s.queue_backend,
                lock_backend=s.lock_backend,
                hint="队列用 Redis 而锁用进程内实现，跨副本互斥会失效",
            )
    else:
        # M2 会在这里装配 InMemoryQueue。现在先明确地报出来，
        # 而不是返回一个 None 让调用方在很远的地方崩掉。
        log.warning(
            "config.memory_backend_pending",
            queue_backend=s.queue_backend,
            hint="内存队列在 M2 交付；当前该进程没有可用的 TaskQueue",
        )
        probes.append(("redis", _redis_not_needed))

    log.info(
        "deps.opened",
        mode=s.mode,
        queue_backend=s.queue_backend,
        lock_backend=s.lock_backend,
        database=_safe_dsn(s.database_url),
    )
    return Dependencies(postgres=postgres, queue=queue, _probes=probes)


async def _redis_not_needed() -> CheckResult:
    return skipped("redis", "精简模式用进程内队列，本就不需要 Redis")


def _client_name(s: Settings) -> str:
    """``sfly-api`` / ``sfly-orchestrator`` / ``sfly-worker`` —— 从 MODE 之外的地方拿不到
    进程名，所以用 queue/lock 后端 + 模式组合一个够用的标识。"""
    return f"sfly-{s.mode}"


def _safe_dsn(dsn: str) -> str:
    """``postgresql://user:pw@host/db`` → ``postgresql://user@host/db``。

    启动日志会打到容器 stdout，而 ``docker compose logs`` 经常被贴进 issue。
    """
    if "@" not in dsn:
        return dsn
    head, _, tail = dsn.rpartition("@")
    scheme, sep, creds = head.partition("://")
    if not sep:
        return f"***@{tail}"
    user = creds.split(":", 1)[0]
    return f"{scheme}{sep}{user}@{tail}"
