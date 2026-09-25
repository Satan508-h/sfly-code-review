"""Postgres 两件事：连接池（``PostgresPool``）与仓储（``PostgresRunStore``）。

**两种拓扑都要有它**，所以这里没有「内存实现」一说 —— ``RunStore`` 是硬依赖：
``wait`` 节点的屏障查询、幂等性的唯一约束、SSE 的事件日志、恢复逻辑的超时扫描
全部落在这张表上。内存实现不是「还没写」，是**不会写**。

放在同一个文件里的理由和 ``memory.py`` / ``redis_streams.py`` 一致：
一个后端一个模块，池子和它的仓储是同一条命（池子只为仓储而存在，
而仓储的所有超时语义都来自池子的配置）。

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
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from sfly_bus.base import RunStore
from sfly_bus.health import CheckResult, down, ok
from sfly_bus.migrations import MigrationDriftError, run_migrations
from sfly_shared.contracts import (
    BootstrapMessage,
    DeliveryRow,
    DeliveryStatus,
    ErrorClass,
    Finding,
    ResultStatus,
    ReviewReport,
    RunEvent,
    RunRow,
    RunStatus,
    RunTotals,
    Severity,
    WorkerResult,
    WorkerType,
)
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

#: 启动时等连接的上限。见 ``PostgresRunStore.migrate`` —— 池子默认等 30 秒，
#: 而那是**启动路径**上的 30 秒：容器迟迟不 ready，日志里什么都没有。
MIGRATE_TIMEOUT_S = 10.0


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
            kwargs={
                "connect_timeout": connect_timeout_s,
                # 行工厂设在这里而不是每个 cursor 上：仓储里每一处查询都按列名取值，
                # ``row[7]`` 这种写法在加一列之后不会报错、只会静默取到别的字段。
                # 代价是 join 时同名列会互相覆盖 —— 所以仓储里一律不写 join。
                "row_factory": dict_row,
            },
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


# --------------------------------------------------------------------------- #
# 仓储
# --------------------------------------------------------------------------- #

#: LangGraph checkpointer 的四张表里**要按 thread 清的那三张**（M5 接入后出现）。
#: ``checkpoint_migrations`` 不在里面 —— 那是它自己的版本表，删了会让它重跑建表。
_CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


class PostgresRunStore:
    """``RunStore`` 协议的实现。**两种拓扑共用这一个**（精简模式也连 Postgres）。

    这里没有 ORM。SQL 直接写出来是刻意的：这个仓储的全部价值在于
    「约束 + 原子性」，而 ``ON CONFLICT DO NOTHING``、``COALESCE`` 保留旧值、
    部分索引这些东西，用 ORM 表达只会更难看清。表结构在 ``migrations/001_init.sql``。

    ### 三条贯穿全文件的规则

    * **每一处写入都显式开事务。** 不依赖连接池的隐式提交语义 —— 那是个
      「看起来一样、边界情况不同」的地方，而 ``save_result`` 的原子性正是靠它。
    * **不写 join。** 行工厂是 ``dict_row``（见 ``PostgresPool``），join 里的
      同名列会互相覆盖成一列，而**不会报错**。
    * **枚举进出都转。** 库里存 text（带 CHECK 约束），进出一律过
      ``RunStatus(...)`` / ``WorkerType(...)``。str 子类混着裸字符串用，
      迟早会出现「某处比较永远不相等」。
    """

    def __init__(self, pool: PostgresPool, *, run_deadline_s: int = 600) -> None:
        self._pool = pool
        self._run_deadline_s = run_deadline_s

    # -- 生命周期 ---------------------------------------------------------- #

    async def migrate(self) -> None:
        """建表（幂等）。五个容器会同时调它 —— 串行化在 ``run_migrations`` 里。

        **等连接是有上界的**（``MIGRATE_TIMEOUT_S``），因为它在启动路径上：
        数据库不可达时池子拿不到连接，而默认的等待是 30 秒 —— 那 30 秒里
        容器的 lifespan 卡在启动阶段，``tasks.py up --wait`` 看起来像卡死了，
        日志里一个字都没有。有上界的结果是 10 秒后一条能读的 error。
        """
        async with self._pool.connection(timeout=MIGRATE_TIMEOUT_S) as conn:
            await run_migrations(conn)

    async def close(self) -> None:
        """**故意什么都不做。**

        连接池由 :class:`~sfly_bus.factory.Dependencies` 持有 —— 它同时服务
        健康探测和这个仓储，关在这里会让健康页在关机路径上拿到一个已关闭的池子。
        留着这个方法是为了满足协议：调用方（``Dependencies.close``）不需要知道
        池子归谁管。
        """
        return None

    # -- run --------------------------------------------------------------- #

    async def create_run(self, msg: BootstrapMessage) -> RunRow:
        """建 run。**幂等键冲突时返回已存在的那一行。**

        ``ON CONFLICT ... DO UPDATE SET updated_at = updated_at`` 是这里唯一
        值得解释的一行：它是一次**空更新**，存在的唯一理由是让 ``RETURNING``
        拿到那一行。用 ``DO NOTHING`` + 随后的 SELECT 也能写，但那有一个真实的
        竞态 —— 两个 API 副本同时收到同一个 webhook 时，输的那个 SELECT 可能
        看不见赢的那行（对方还没提交），于是返回 None，而调用方以为建失败了。

        空更新会**锁住那一行并等**对方提交，然后连同 RETURNING 一起给出。
        ``updated_at`` 刻意不改：它表示「这一行变过」，而重复投递不是变化。
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                    INSERT INTO review_runs (
                        task_id, idempotency_key, repo_id, repo_node_id, pr_number,
                        head_sha, base_sha, deadline_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))
                    ON CONFLICT (idempotency_key)
                        DO UPDATE SET updated_at = review_runs.updated_at
                    RETURNING *
                    """,
                [
                    msg.task_id,
                    msg.idempotency_key,
                    msg.repo_id,
                    msg.repo_node_id,
                    msg.pr_number,
                    msg.head_sha,
                    msg.base_sha,
                    self._run_deadline_s,
                ],
            )
            row: dict[str, Any] | None = await cur.fetchone()

        if row is None:  # pragma: no cover —— DO UPDATE 保证一定有行返回
            raise RuntimeError(f"create_run 没有返回行：{msg.idempotency_key}")
        run = _to_run(row)
        if run.task_id != msg.task_id:
            # 同一个 PR 的同一个 head_sha 又来了一次：**返回老 run，不建新的**。
            # 这是 GitHub 超时重投时的正常路径，不是故障。
            log.info(
                "store.run_duplicate",
                idempotency_key=msg.idempotency_key,
                existing=run.task_id,
                incoming=msg.task_id,
                status=run.status.value,
            )
        return run

    async def get_run(self, task_id: str) -> RunRow | None:
        return await self._one_run("SELECT * FROM review_runs WHERE task_id = %s", [task_id])

    async def get_run_by_key(self, idempotency_key: str) -> RunRow | None:
        return await self._one_run("SELECT * FROM review_runs WHERE idempotency_key = %s", [idempotency_key])

    async def _one_run(self, sql: str, params: list[Any]) -> RunRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(sql, params)
            row: dict[str, Any] | None = await cur.fetchone()
        return _to_run(row) if row is not None else None

    async def set_status(
        self,
        task_id: str,
        status: RunStatus,
        *,
        degraded: bool | None = None,
        missing_workers: list[WorkerType] | None = None,
    ) -> None:
        """改状态。``None`` 的参数表示「不动这一列」，不是「置为空」。

        ``COALESCE(%s::boolean, degraded)`` 就是这件事的写法：参数为 NULL 时
        取旧值。两个 ``::`` 强制转换不是装饰 —— 不写的话 Postgres 拿不到
        NULL 的类型（它是 unknown），会报「无法确定参数类型」。
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                    UPDATE review_runs
                       SET status = %s,
                           degraded = COALESCE(%s::boolean, degraded),
                           missing_workers = COALESCE(%s::text[], missing_workers),
                           updated_at = now()
                     WHERE task_id = %s
                    """,
                [
                    status.value,
                    degraded,
                    [w.value for w in missing_workers] if missing_workers is not None else None,
                    task_id,
                ],
            )
            if cur.rowcount == 0:
                # 不改状态、不抛错：调用方可能正在为一条已被清理的 run 收尾。
                # 但一条都影响不到通常意味着 task_id 拼错了，所以要说一声。
                log.warning("store.set_status_missing_run", task_id=task_id, status=status.value)

    async def set_plan(
        self,
        task_id: str,
        planned_workers: list[WorkerType],
        *,
        files_total: int,
        files_reviewed: int,
        diff_truncated: bool,
        deadline_at: datetime,
    ) -> None:
        """``plan`` 节点写回计划与 deadline。

        状态的条件更新（``CASE WHEN status = 'queued'``）是给**重放**用的：
        LangGraph 恢复时会重新执行节点，而 plan 在恢复时很可能是在一个已经
        ``waiting`` 的 run 上跑的 —— 无条件写 ``dispatched`` 会把 run 倒退回去，
        症状是扫描器开始盯着一个其实已经在等屏障的 run。

        ``dispatched_at`` 用 COALESCE 只写第一次，理由同上。
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                    UPDATE review_runs
                       SET planned_workers = %s,
                           files_total = %s,
                           files_reviewed = %s,
                           diff_truncated = %s,
                           deadline_at = %s,
                           dispatched_at = COALESCE(dispatched_at, now()),
                           status = CASE WHEN status = 'queued' THEN 'dispatched' ELSE status END,
                           updated_at = now()
                     WHERE task_id = %s
                    """,
                [
                    [w.value for w in planned_workers],
                    files_total,
                    files_reviewed,
                    diff_truncated,
                    deadline_at,
                    task_id,
                ],
            )
            if cur.rowcount == 0:
                log.warning("store.set_plan_missing_run", task_id=task_id)

    async def set_decision(self, task_id: str, *, block_merge: bool, totals: RunTotals) -> None:
        """写回最终决定与成本汇总。

        这两列在 ``review_runs`` 上（而不是只存在 ``review_reports`` 的 jsonb 里），
        是因为**运行列表要显示它们**：列表页不该为了显示一次成本去解每一行的
        jsonb。列注释里写着「``block_merge`` 可空 = 还没做决定，与 ``false``
        （决定了不阻断）是两件不同的事」—— 这个方法就是让那句话成立的地方。

        幂等（重放安全）：同样的入参重复写得到同样的结果。
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                    UPDATE review_runs
                       SET block_merge = %s, totals = %s, updated_at = now()
                     WHERE task_id = %s
                    """,
                [block_merge, Jsonb(totals.model_dump(mode="json")), task_id],
            )
            if cur.rowcount == 0:
                log.warning("store.set_decision_missing_run", task_id=task_id)

    async def due_runs(self, now: datetime) -> list[RunRow]:
        """过期的、还在等屏障的 run。走 ``review_runs_due_idx`` 那个部分索引。

        **``queued`` 不在这里**，这是刻意的：一个还没被编排器消费的 bootstrap，
        它的状态活在**队列里**（Redis 的 PEL / 内存队列），所以它由队列层的
        ``reclaim()`` 负责恢复。把 ``queued`` 也交给扫描器的话，两个机制会同时
        去救同一个 run —— 而扫描器那一侧没有「bootstrap 消息是否还在」的信息，
        它只能盲目唤醒，唤醒一个没有 checkpoint 的图。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM review_runs
                 WHERE status IN ('dispatched', 'waiting')
                   AND deadline_at <= %s
                 ORDER BY deadline_at
                """,
                [now],
            )
            rows: list[dict[str, Any]] = await cur.fetchall()
        return [_to_run(r) for r in rows]

    # -- 结果 -------------------------------------------------------------- #

    async def save_result(self, result: WorkerResult) -> None:
        """落一条结果（连同它的 findings），**幂等**。

        先写的那条赢：``ON CONFLICT DO NOTHING`` 加上 ``RETURNING``，
        返回空就说明这条 (task_id, worker_type) 已经有人写过了，直接返回。

        于是「已经被回收过一次的任务被重跑出结果」这件事在数据库层就没了 ——
        这才是幂等性的保证（CLAUDE.md 里那条「不要用 Redis SETNX 当正确性机制」）。
        重复上报不是异常路径，它会走 here 并且只留下一行 ``store.result_duplicate``。

        findings 只在**这一行是新的**时候写。不做「先删后插」是因为没必要：
        结果行和它的 findings 在同一个事务里写入，不存在「有结果行但没有 findings」
        的中间状态。
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                    INSERT INTO worker_results (
                        task_id, worker_type, status, error, error_class,
                        tokens_in, tokens_out, cached_tokens, latency_ms, model,
                        raw_response, dropped_findings, attempt, finished_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (task_id, worker_type) DO NOTHING
                    RETURNING task_id
                    """,
                [
                    result.task_id,
                    result.worker_type.value,
                    result.status.value,
                    result.error,
                    result.error_class.value if result.error_class else None,
                    result.tokens_in,
                    result.tokens_out,
                    result.cached_tokens,
                    result.latency_ms,
                    result.model,
                    result.raw_response,
                    result.dropped_findings,
                    result.attempt,
                    result.finished_at,
                ],
            )
            if await cur.fetchone() is None:
                log.info(
                    "store.result_duplicate",
                    task_id=result.task_id,
                    worker_type=result.worker_type.value,
                    status=result.status.value,
                    hint="这条结果已经写过了，本次丢弃（幂等）",
                )
                return
            if result.findings:
                # executemany 在**游标**上，不在连接上 —— AsyncConnection 没有这个方法
                # （同步连接有，所以这里是那种「照着 psycopg2 的印象写会踩」的地方）。
                # 一个 Worker 报十几条 finding，逐条 INSERT 就是十几次往返。
                async with conn.cursor() as cur:
                    await cur.executemany(
                        """
                        INSERT INTO findings (
                            task_id, worker_type, file, line, end_line, severity, category,
                            message, evidence, confidence, suggestion, rule_id,
                            source_line_verified, fingerprint
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        [_finding_params(result.task_id, result.worker_type, f) for f in result.findings],
                    )

    async def get_results(self, task_id: str) -> list[WorkerResult]:
        """一个 run 的全部上报，含各自的 findings。

        两次查询而不是 join：join 会给出 findings × results 的笛卡尔积，
        然后在 Python 里再拆一次 —— 而这里两次查询各自都是走索引的点查。
        """
        async with self._pool.connection() as conn:
            results_cur = await conn.execute(
                "SELECT * FROM worker_results WHERE task_id = %s ORDER BY worker_type", [task_id]
            )
            result_rows: list[dict[str, Any]] = await results_cur.fetchall()

            findings_cur = await conn.execute(
                "SELECT * FROM findings WHERE task_id = %s ORDER BY id", [task_id]
            )
            finding_rows: list[dict[str, Any]] = await findings_cur.fetchall()

        by_worker: dict[str, list[Finding]] = {}
        for row in finding_rows:
            by_worker.setdefault(str(row["worker_type"]), []).append(_to_finding(row))
        return [_to_result(r, by_worker.get(str(r["worker_type"]), [])) for r in result_rows]

    async def completed_workers(self, task_id: str) -> list[WorkerType]:
        """屏障查询。**没有 status 过滤** —— 失败也是一条结果（约定 #2）。

        这也是为什么它不该写成「哪些 Worker 成功了」：写成那样的话，
        一个 Worker 失败之后屏障永远闭合不了，整个 run 会挂到超时。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT worker_type FROM worker_results WHERE task_id = %s ORDER BY worker_type",
                [task_id],
            )
            rows: list[dict[str, Any]] = await cur.fetchall()
        return [WorkerType(str(r["worker_type"])) for r in rows]

    async def exists_result(self, task_id: str, worker_type: WorkerType | str) -> bool:
        """Worker 端的幂等快路径：做过就不再烧一遍 token。"""
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT 1 FROM worker_results
                 WHERE task_id = %s AND worker_type = %s
                 LIMIT 1
                """,
                [task_id, str(worker_type)],
            )
            return await cur.fetchone() is not None

    # -- webhook 投递 ------------------------------------------------------ #

    async def record_delivery(
        self,
        delivery_id: str,
        *,
        event: str,
        repo_id: str = "",
        pr_number: int | None = None,
    ) -> bool:
        """认领一次投递。**首次 True，已经见过 False。**

        为什么是 ``DO NOTHING`` 而 ``create_run`` 用的是 ``DO UPDATE``（空更新）：
        两者的需求正好相反。``create_run`` 要的是**那一行**（老 run 的 task_id），
        所以愿意为它等对方提交；这里要的只是**「我是不是第一个」**这一个布尔值，
        而 ``RETURNING`` 在冲突时不返回行，正好就是那个布尔值。
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                """
                    INSERT INTO webhook_deliveries (delivery_id, event, repo_id, pr_number)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (delivery_id) DO NOTHING
                    RETURNING delivery_id
                    """,
                [delivery_id, event, repo_id, pr_number],
            )
            row: dict[str, Any] | None = await cur.fetchone()
        return row is not None

    async def list_deliveries(self, limit: int = 50) -> list[DeliveryRow]:
        """最近的投递，新的在前。不需要分页 —— 这是排查用的视图，
        真正的问题（「我这次投递怎么了」）永远在最近几条里。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM webhook_deliveries ORDER BY received_at DESC LIMIT %s",
                [limit],
            )
            rows: list[dict[str, Any]] = await cur.fetchall()
        return [_to_delivery(r) for r in rows]

    async def get_delivery(self, delivery_id: str) -> DeliveryRow | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT * FROM webhook_deliveries WHERE delivery_id = %s", [delivery_id])
            row: dict[str, Any] | None = await cur.fetchone()
        return _to_delivery(row) if row is not None else None

    async def release_delivery(self, delivery_id: str) -> None:
        """撤销一次**还没结算**的认领（删掉那一行），让它能被重新处理。

        只删仍然停在 ``received`` 的那一行。带这个条件是必需的：
        接管窗口之外可能有另一个请求已经把这次投递接管并结算了，
        无条件删会把**它的**记录抹掉 —— 而那时它已经把 bootstrap 投出去了。
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "DELETE FROM webhook_deliveries WHERE delivery_id = %s AND status = 'received'",
                [delivery_id],
            )
            if cur.rowcount == 0:
                log.info("store.delivery_release_noop", delivery_id=delivery_id)

    async def finish_delivery(
        self,
        delivery_id: str,
        status: DeliveryStatus,
        *,
        task_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """了结一次投递。

        ``COALESCE`` 让 ``None`` 表示「不动这一列」而不是「清空」——
        与 ``set_status`` 同一个约定。这里尤其要紧：``accepted`` 之后
        再来一次同 delivery 的投递，走的路径是「读出来、发现是终态、报告重复」，
        不该顺手把原来的 task_id 抹掉。
        """
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                """
                    UPDATE webhook_deliveries
                       SET status = %s,
                           task_id = COALESCE(%s, task_id),
                           reason = COALESCE(%s, reason),
                           finished_at = now()
                     WHERE delivery_id = %s
                    """,
                [str(status), task_id, reason, delivery_id],
            )

    # -- 报告 -------------------------------------------------------------- #

    async def save_report(self, report: ReviewReport) -> None:
        """存最终报告（覆盖写：重新聚合应该替换上一次的结果）。

        ``report`` 整份存 jsonb，另外几列是**故意冗余**的：运行列表要显示成本和
        条数、评测要做全库聚合、publish 失败后要能直接重发 ``comment_body`` ——
        这三件事都不该为了拿一个数字去解 jsonb。

        ``model_dump(mode="json")`` 而不是 ``model_dump()``：里面全是
        ``datetime`` / 枚举，psycopg 的 Jsonb 走的是 ``json.dumps``，
        不做这层转换会在写库那一刻抛 ``TypeError``。
        """
        totals = report.totals
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                """
                    INSERT INTO review_reports (
                        task_id, report, findings_count, suppressed_count, conflicts_count,
                        block_merge, degraded, comment_body,
                        tokens_in, tokens_out, cached_tokens, cost_usd, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (task_id) DO UPDATE
                       SET report = EXCLUDED.report,
                           findings_count = EXCLUDED.findings_count,
                           suppressed_count = EXCLUDED.suppressed_count,
                           conflicts_count = EXCLUDED.conflicts_count,
                           block_merge = EXCLUDED.block_merge,
                           degraded = EXCLUDED.degraded,
                           comment_body = EXCLUDED.comment_body,
                           tokens_in = EXCLUDED.tokens_in,
                           tokens_out = EXCLUDED.tokens_out,
                           cached_tokens = EXCLUDED.cached_tokens,
                           cost_usd = EXCLUDED.cost_usd,
                           updated_at = now()
                    """,
                [
                    report.task_id,
                    Jsonb(report.model_dump(mode="json")),
                    len(report.findings),
                    len(report.suppressed),
                    len(report.conflicts),
                    report.block_merge,
                    report.degraded,
                    report.comment_body,
                    totals.tokens_in,
                    totals.tokens_out,
                    totals.cached_tokens,
                    totals.cost_usd,
                ],
            )

    async def get_report(self, task_id: str) -> ReviewReport | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute("SELECT report FROM review_reports WHERE task_id = %s", [task_id])
            row: dict[str, Any] | None = await cur.fetchone()
        if row is None:
            return None
        return ReviewReport.model_validate(row["report"])

    async def mark_published(self, task_id: str, comment_id: int) -> None:
        """记下评论 id。**不碰 status** —— 那是 ``set_status`` 的地盘。

        两个方法都写 status 的话，最终状态取决于调用顺序，而这就是那种
        「本地跑得好好的、线上偶尔不对」的 bug。publish 节点按自己的语义
        调 ``set_status(PUBLISHED)``，这里只负责「发过了、id 是多少」。
        """
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                """
                    UPDATE review_runs
                       SET github_comment_id = %s, published_at = now(), updated_at = now()
                     WHERE task_id = %s
                    """,
                [comment_id, task_id],
            )

    # -- 事件 -------------------------------------------------------------- #

    async def append_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> int:
        """追加一条事件，返回它的 ``seq``（全局自增，见 001_init.sql 里的说明）。"""
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "INSERT INTO run_events (task_id, kind, payload) VALUES (%s, %s, %s) RETURNING seq",
                [task_id, kind, Jsonb(payload)],
            )
            row: dict[str, Any] | None = await cur.fetchone()
        if row is None:  # pragma: no cover —— INSERT ... RETURNING 一定有行
            raise RuntimeError("append_event 没有返回 seq")
        return int(row["seq"])

    async def events_since(self, task_id: str, after_seq: int) -> list[RunEvent]:
        """``seq > after_seq`` 的事件 —— SSE 带 ``Last-Event-ID`` 重连时的补齐路径。

        轮询也用同一个查询（``after_seq=0`` 就是全量），所以断线降级成轮询
        不需要另写一段代码。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT * FROM run_events
                 WHERE task_id = %s AND seq > %s
                 ORDER BY seq
                """,
                [task_id, after_seq],
            )
            rows: list[dict[str, Any]] = await cur.fetchall()
        return [_to_event(r) for r in rows]

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[RunRow]:
        """按 ``task_id`` 倒序 —— 也就是按时间倒序。

        ULID 前 48 位是毫秒时间戳（``sfly_shared/ids.py``），所以主键索引就是
        时间索引，不需要额外的 ``created_at`` 列和索引。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT * FROM review_runs ORDER BY task_id DESC LIMIT %s OFFSET %s",
                [limit, offset],
            )
            rows: list[dict[str, Any]] = await cur.fetchall()
        return [_to_run(r) for r in rows]

    # -- 成本 -------------------------------------------------------------- #

    async def record_llm_call(
        self,
        *,
        task_id: str | None,
        agent: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cached_tokens: int,
        cost_usd: float,
        latency_ms: int,
        ok: bool,
        error_class: str | None = None,
    ) -> None:
        """记一次调用。**失败的调用也要记** —— 失败也花了 token。

        ``task_id`` 可以是 ``None``（独立 CLI 跑单次审查时没有 run）。
        这张表刻意没有外键，见 001_init.sql 里的说明：成本记录要比 run 活得久。
        """
        async with self._pool.connection() as conn, conn.transaction():
            await conn.execute(
                """
                    INSERT INTO llm_calls (
                        task_id, agent, model, tokens_in, tokens_out, cached_tokens,
                        cost_usd, latency_ms, ok, error_class
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                [
                    task_id,
                    agent,
                    model,
                    tokens_in,
                    tokens_out,
                    cached_tokens,
                    cost_usd,
                    latency_ms,
                    ok,
                    error_class,
                ],
            )

    async def sum_costs(self, task_id: str) -> dict[str, float]:
        """单 run 的 token 与成本汇总。评测报告里的每个数字都来自这一条查询。

        ``sum()`` 在 ``numeric`` 上返回 ``Decimal``，而调用方要的是 float ——
        在 SQL 里 ``::float8`` 提前转掉，比让每个调用点自己转一遍可靠。
        """
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                """
                SELECT coalesce(sum(tokens_in), 0)::float8     AS tokens_in,
                       coalesce(sum(tokens_out), 0)::float8    AS tokens_out,
                       coalesce(sum(cached_tokens), 0)::float8 AS cached_tokens,
                       coalesce(sum(cost_usd), 0)::float8      AS cost_usd,
                       count(*)::float8                        AS llm_calls
                  FROM llm_calls
                 WHERE task_id = %s
                """,
                [task_id],
            )
            row: dict[str, Any] | None = await cur.fetchone()
        if row is None:  # pragma: no cover —— 聚合查询一定有一行
            return {}
        return {k: float(v) for k, v in row.items()}

    # -- 清理 -------------------------------------------------------------- #

    async def purge_older_than(self, days: int = 14) -> dict[str, int]:
        """清理旧的 run、事件与 checkpoint。返回每张表删了多少行。

        Neon 免费版只有 0.5GB，而 LangGraph 的 checkpoint **按 thread 无界增长** ——
        线上演示跑一段时间就会写满，所以这不是「有空再做的优化」。

        顺序是有讲究的：**先记下要删的 run id，再删**。checkpoint 的清理要按
        ``thread_id = task_id`` 来，而 run 一删，那个 id 就再也查不出来了。
        （``run_events`` 同理 —— 虽然它有 ON DELETE CASCADE，但显式按 id 删
        能让返回值里那个数字是真的「删了几条事件」而不是 0。）
        """
        async with self._pool.connection() as conn, conn.transaction():
            cur = await conn.execute(
                "SELECT task_id FROM review_runs WHERE created_at < now() - make_interval(days => %s)",
                [days],
            )
            old_ids = [str(r["task_id"]) for r in await cur.fetchall()]
            counts: dict[str, int] = {}

            if not old_ids:
                # 没有旧 run，但 llm_calls 仍然要按时间清（它没有外键，
                # 而且 task_id 可以是 NULL 的记录也必须能被清掉）
                counts["llm_calls"] = await _delete_llm_calls(conn, days)
                return counts

            for table in _CHECKPOINT_TABLES:
                counts[table] = await _delete_checkpoints(conn, table, old_ids)
            counts["run_events"] = await _delete_by_task_ids(conn, "run_events", old_ids)
            # 结果、发现、报告都靠 ON DELETE CASCADE 跟着走
            counts["review_runs"] = await _delete_by_task_ids(conn, "review_runs", old_ids)
            counts["llm_calls"] = await _delete_llm_calls(conn, days)
        log.info("store.purged", days=days, **counts)
        return counts


async def _delete_by_task_ids(conn: Any, table: str, task_ids: list[str]) -> int:
    # 表名来自本模块的常量，不含用户输入
    cur = await conn.execute(f"DELETE FROM {table} WHERE task_id = ANY(%s)", [task_ids])  # noqa: S608
    return int(cur.rowcount)


async def _delete_llm_calls(conn: Any, days: int) -> int:
    """按时间清成本记录。**不按 task_id** —— 那张表的意义就是比 run 活得久。"""
    cur = await conn.execute(
        "DELETE FROM llm_calls WHERE created_at < now() - make_interval(days => %s)", [days]
    )
    return int(cur.rowcount)


async def _delete_checkpoints(conn: Any, table: str, task_ids: list[str]) -> int:
    """清 LangGraph 的 checkpoint。**表还不存在时返回 0。**

    M5 接入 ``AsyncPostgresSaver`` 之后这些表才会出现（它自己建，不走我们的
    迁移器）。所以这里必须先问一句 ``to_regclass``：M4 阶段直接调
    ``DELETE FROM checkpoints`` 会报「关系不存在」，而清理任务是每日自动跑的 ——
    一条必然失败的定时任务比没有定时任务更糟。
    """
    exists_cur = await conn.execute("SELECT to_regclass(%s) IS NOT NULL AS present", [f"public.{table}"])
    exists_row: dict[str, Any] | None = await exists_cur.fetchone()
    if not exists_row or not exists_row["present"]:
        return 0
    cur = await conn.execute(
        f"DELETE FROM {table} WHERE thread_id = ANY(%s)",  # noqa: S608
        [task_ids],
    )
    return int(cur.rowcount)


# --------------------------------------------------------------------------- #
# 启动时的迁移
# --------------------------------------------------------------------------- #


async def migrate_on_startup(store: RunStore) -> None:
    """启动时建表。**漂移是致命的，连不上不是。**

    两种失败的处置必须不同，合成一个 ``except Exception`` 就必然做错一边：

    * **连不上数据库** → 记一条 error 然后继续。M0 定的规矩是「Postgres 不可达
      时进程也要能起来」（``PostgresPool.open`` 的文档里有完整论证）：
      崩掉退出会让 Docker 把它拖进重启循环，而重启日志会把真正的错误冲掉。
      起来的进程能在 ``/api/health`` 里说清楚哪里坏了。
    * **迁移漂移**（库里已应用的迁移和仓库里的内容不一致）→ **让它抛出去**。
      这不是环境问题而是部署问题：代码期待的结构和数据库里的不是一回事，
      继续跑就是往错的结构上写数据，而且不会有任何报错。
    """
    try:
        await store.migrate()
    except MigrationDriftError:
        raise
    # 捕获一切是这里的**目的**：启动路径上的失败必须变成一条能读的日志，
    # 而不是一个把容器拖进重启循环的堆栈。
    except Exception as exc:
        log.error(
            "db.migrate_failed",
            error=str(exc),
            error_class=type(exc).__name__,
            hint="表还没建好。Postgres 恢复后重启本容器即可：python tasks.py restart",
        )


# --------------------------------------------------------------------------- #
# 行 → 契约
# --------------------------------------------------------------------------- #
#
# 手写映射而不是 ``RunRow(**row)``：那样会在加一列时静默失败（pydantic 的
# extra="forbid" 会抛，但抛在启动之后、在某个请求里），而显式的字段列表
# 让「库里有什么、契约期待什么」的差异在**读代码时**就看得见。


def _to_run(row: dict[str, Any]) -> RunRow:
    totals = row["totals"]
    return RunRow(
        task_id=str(row["task_id"]),
        idempotency_key=str(row["idempotency_key"]),
        repo_id=str(row["repo_id"]),
        repo_node_id=str(row["repo_node_id"]),
        pr_number=int(row["pr_number"]),
        head_sha=str(row["head_sha"]),
        base_sha=str(row["base_sha"]),
        status=RunStatus(str(row["status"])),
        attempt=int(row["attempt"]),
        files_total=int(row["files_total"]),
        files_reviewed=int(row["files_reviewed"]),
        diff_truncated=bool(row["diff_truncated"]),
        planned_workers=[WorkerType(str(w)) for w in row["planned_workers"]],
        missing_workers=[WorkerType(str(w)) for w in row["missing_workers"]],
        deadline_at=row["deadline_at"],
        dispatched_at=row["dispatched_at"],
        published_at=row["published_at"],
        github_comment_id=int(row["github_comment_id"]) if row["github_comment_id"] is not None else None,
        block_merge=row["block_merge"],
        degraded=bool(row["degraded"]),
        # jsonb 读回来是 dict（psycopg 装了加载器），直接喂给 pydantic
        totals=RunTotals.model_validate(totals) if totals is not None else None,
        created_at=row["created_at"],
    )


def _to_delivery(row: dict[str, Any]) -> DeliveryRow:
    return DeliveryRow(
        delivery_id=str(row["delivery_id"]),
        event=str(row["event"]),
        repo_id=str(row["repo_id"]),
        pr_number=int(row["pr_number"]) if row["pr_number"] is not None else None,
        status=DeliveryStatus(str(row["status"])),
        task_id=row["task_id"],
        reason=row["reason"],
        received_at=row["received_at"],
        finished_at=row["finished_at"],
    )


def _to_result(row: dict[str, Any], findings: list[Finding]) -> WorkerResult:
    error_class = row["error_class"]
    return WorkerResult(
        task_id=str(row["task_id"]),
        worker_type=WorkerType(str(row["worker_type"])),
        status=ResultStatus(str(row["status"])),
        findings=findings,
        error=row["error"],
        # 契约里是 ErrorClass 枚举，库里存 text
        error_class=ErrorClass(str(error_class)) if error_class else None,
        tokens_in=int(row["tokens_in"]),
        tokens_out=int(row["tokens_out"]),
        cached_tokens=int(row["cached_tokens"]),
        latency_ms=int(row["latency_ms"]),
        model=row["model"],
        raw_response=row["raw_response"],
        dropped_findings=int(row["dropped_findings"]),
        attempt=int(row["attempt"]),
        finished_at=row["finished_at"],
    )


def _to_finding(row: dict[str, Any]) -> Finding:
    return Finding(
        file=str(row["file"]),
        line=int(row["line"]),
        end_line=int(row["end_line"]) if row["end_line"] is not None else None,
        severity=Severity(str(row["severity"])),
        category=str(row["category"]),
        message=str(row["message"]),
        evidence=row["evidence"],
        confidence=float(row["confidence"]),
        suggestion=row["suggestion"],
        rule_id=row["rule_id"],
        source_line_verified=bool(row["source_line_verified"]),
        fingerprint=row["fingerprint"],
    )


def _to_event(row: dict[str, Any]) -> RunEvent:
    # kind 在契约里是 Literal（那 11 个事件名），库里是 text —— 而且刻意没有
    # CHECK 约束（见 001_init.sql：写错只影响时间线展示，每加一个事件类型都要
    # 改一次约束的代价更高）。pydantic 会在这里校验，不认识的值会抛 ValidationError，
    # 所以「库里存着旧代码写的事件类型」这件事会在读取时暴露，而不是被静默吞掉。
    return RunEvent(
        seq=int(row["seq"]),
        task_id=str(row["task_id"]),
        kind=row["kind"],
        payload=row["payload"] or {},
        created_at=row["created_at"],
    )


def _finding_params(task_id: str, worker_type: WorkerType, f: Finding) -> list[Any]:
    return [
        task_id,
        worker_type.value,
        f.file,
        f.line,
        f.end_line,
        f.severity.value,
        f.category,
        f.message,
        f.evidence,
        f.confidence,
        f.suggestion,
        f.rule_id,
        f.source_line_verified,
        f.fingerprint,
    ]


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
