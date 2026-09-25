"""**全代码库唯一允许拿 ``QUEUE_BACKEND`` / ``LOCK_BACKEND`` 做分支的文件。**

    grep -rn "queue_backend ==\\|lock_backend ==" --include=*.py packages apps
    # 只应返回本文件

这条约定是整个项目的中心论点（「一套代码、两种拓扑」）能不能成立的地方。
一旦某个节点或 Worker 开始判断「我用的是不是 Redis」，两种拓扑就跑在不同的
代码路径上，共用代码这件事从「事实」退化成「宣传」。

注意上面 grep 的是**分支**而不是名字：这两个名字必然会出现在别处，而且都不算
违规 —— ``config.py`` 定义它们，``api/main.py`` 把当前值回显到健康页，
``lite/__main__.py`` 的文档字符串里提到它们。这条约定管的是「谁拿它做判断」。
可执行的版本在 ``tests/unit/bus/test_protocols.py``（AST 扫描）。

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

from sfly_bus.base import Lock, TaskQueue
from sfly_bus.health import CheckResult, HealthReport, down, skipped
from sfly_bus.memory import InMemoryLock, InMemoryQueue
from sfly_bus.postgres import PostgresPool
from sfly_bus.redis_streams import RedisLock, RedisStreamsQueue
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

    queue: TaskQueue | None = None
    """``TaskQueue`` 实现。完整模式是 :class:`RedisStreamsQueue`，
    精简模式是 :class:`InMemoryQueue`。"""

    lock: Lock | None = None
    """``Lock`` 实现。完整模式是 :class:`RedisLock`（``SET NX PX`` + Lua 释放），
    精简模式是 :class:`InMemoryLock`。"""

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
        """释放连接。**先关传输再关数据库** —— 队列的后台回收协程可能会写库，
        反过来关会让它在最后几秒里对着一个已关闭的池子报错。

        全程吞异常：关机路径上抛错只会让进程带着非零码退出，
        而这时候 Docker 已经在拆容器了，那条错误没人看得到。
        """
        for handle in (self.queue, self.lock):
            if handle is not None:
                with contextlib.suppress(Exception):
                    await handle.close()
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

    queue, lock = await _open_transport(s, probes)

    log.info(
        "deps.opened",
        mode=s.mode,
        queue_backend=s.queue_backend,
        lock_backend=s.lock_backend,
        queue=type(queue).__name__ if queue is not None else None,
        lock=type(lock).__name__ if lock is not None else None,
        database=_safe_dsn(s.database_url),
    )
    return Dependencies(postgres=postgres, queue=queue, lock=lock, _probes=probes)


async def _open_transport(s: Settings, probes: list[Probe]) -> tuple[TaskQueue | None, Lock | None]:
    """装配队列与锁 —— **本函数是这两个后端变量的唯一分支点。**

    队列和锁是**正交**的配置项（可以队列用 Redis、锁用进程内），所以它们各自
    独立判断，最后再统一检查一次「混搭是不是配错了」。
    """
    queue: TaskQueue | None = None
    lock: Lock | None = None
    #: 「Redis 可达吗」这个探测只有一个，谁在用 Redis 就由谁回答。
    redis_probe: Probe | None = None

    if s.queue_backend == "redis":
        # client_name 会出现在 `redis-cli client list` 里 ——
        # `--scale worker-security=3` 之后能一眼数出三个副本。
        # 队列行为的参数**两种实现同名同义**，所以它们从同一处配置来：
        # 换后端不该意味着换一套调参。
        rq = RedisStreamsQueue(
            s.redis_url,
            client_name=_client_name(s),
            claim_idle_ms=s.claim_idle_ms,
            stream_maxlen_tasks=s.stream_maxlen_tasks,
            stream_maxlen_results=s.stream_maxlen_results,
        )
        await rq.start()
        queue = rq
        redis_probe = ("redis", rq.ping)
    else:
        mq = InMemoryQueue(
            claim_idle_ms=s.claim_idle_ms,
            stream_maxlen_tasks=s.stream_maxlen_tasks,
            stream_maxlen_results=s.stream_maxlen_results,
        )
        # **必须 start()**：消费者组是在这一步建的，而建组时游标设在流尾。
        # 漏了它，第一次读写会明确报错而不是安静地少消费几条消息。
        await mq.start()
        queue = mq
        # 精简模式下 Redis 不是「连不上」，而是**不存在**。报 skipped 而不是 down：
        # 这个依赖在当前拓扑下本就不需要，报故障是撒谎，报正常也是撒谎。
        redis_probe = ("redis", _redis_not_needed)

    if s.lock_backend == "redis":
        rl = RedisLock(s.redis_url, client_name=f"{_client_name(s)}-lock")
        await rl.start()
        lock = rl
        # 只在队列用的是进程内实现时才由锁来回答 —— 否则健康页上会出现两条
        # 同名的 redis 检查，而 `HealthReport.as_dict()` 按名字展平，第二条会
        # 把第一条挤掉（汇总的 ok 还是两者都得健康，所以不会误报，但页面会少一行）。
        if s.queue_backend != "redis":
            redis_probe = ("redis", rl.ping)
    else:
        lock = InMemoryLock()

    probes.append(redis_probe)

    if s.queue_backend != s.lock_backend:
        # 代码是对的（两边按各自的设置装配），但配置几乎肯定是写错了：
        # 队列在 Redis 而锁在进程内 → 跨副本互斥失效（--scale 之后形同没有锁）；
        # 队列在进程内而锁在 Redis → 精简模式里根本没有 Redis 可连。
        # 所以这里不停下来，但要喊一声 —— 这类错配的症状是「偶尔重复处理」。
        log.warning(
            "config.backend_mismatch",
            queue_backend=s.queue_backend,
            lock_backend=s.lock_backend,
            hint="队列与锁不在同一个后端上；除非你确实想这样，否则多半是配错了",
        )

    return queue, lock


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
