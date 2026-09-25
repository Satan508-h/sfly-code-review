"""依赖健康探测的单测。

**全部用「连不上的地址」而不是真实服务**，这一点是刻意的：

* 真实路径的失败分支（连接被拒绝）比成功分支更容易写错，而它平时几乎不会被走到 ——
  没人会为了测它去把数据库停掉。用 ``127.0.0.1:1`` 就是在每一次提交里都走一遍。
* 单测因此保持「无 Docker、无密钥」的分层约定，裸机上克隆下来就能跑。

端口 1 是 IANA 保留端口（TCPMUX），实际不可能有服务监听。
实测连它会在毫秒级返回 ECONNREFUSED（Windows 的 SelectorEventLoop 上约 2 秒）。
"""

from __future__ import annotations

import asyncio
import time

import pytest

from sfly_bus.factory import Dependencies, open_dependencies
from sfly_bus.health import (
    CheckResult,
    CheckStatus,
    HealthReport,
    down,
    ok,
    redact,
    skipped,
)
from sfly_bus.postgres import PostgresPool
from sfly_bus.redis_streams import RedisStreamsQueue, _safe_url, group_for
from sfly_shared.config import Settings

#: 保证连不上的地址。见模块文档。
DEAD_PG = "postgresql://nobody:nopw@127.0.0.1:1/nope"
DEAD_REDIS = "redis://127.0.0.1:1/0"


# --------------------------------------------------------------------------- #
# 结果模型
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_skipped_counts_as_healthy() -> None:
    """``skipped`` 必须算健康。

    三态而不是布尔的**全部理由**就在这一条：精简模式下没有 Redis，
    报 ``down`` 会让线上演示站在完全正常的状态下常亮红灯。
    """
    assert skipped("redis", "精简模式无需 Redis").ok is True
    assert down("redis", "boom").ok is False


@pytest.mark.unit
def test_report_ok_ignores_skipped_but_not_down() -> None:
    healthy = HealthReport([ok("postgres", "PG 16.4"), skipped("redis", "无")])
    assert healthy.ok is True

    broken = HealthReport([ok("postgres", "PG 16.4"), down("redis", "boom")])
    assert broken.ok is False
    assert [c.name for c in broken.down] == ["redis"]


@pytest.mark.unit
def test_report_as_dict_is_keyed_by_dependency_name() -> None:
    """前端按名字取，不希望自己遍历数组找。"""
    d = HealthReport([ok("postgres", "PostgreSQL 16.4", 1.234), skipped("redis", "无")]).as_dict()
    assert d["ok"] is True
    assert set(d["checks"]) == {"postgres", "redis"}
    assert d["checks"]["postgres"]["detail"] == "PostgreSQL 16.4"
    assert d["checks"]["postgres"]["latency_ms"] == 1.2  # 已四舍五入到 0.1ms


@pytest.mark.unit
def test_summary_is_a_one_liner() -> None:
    r = HealthReport([ok("postgres"), down("redis", "x")])
    assert r.summary() == "postgres=ok redis=down"


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("connection to postgresql://sfly:hunter2@db:5432 failed", "hunter2"),
        ("redis://default:s3cr3t@cache:6379 refused", "s3cr3t"),
        ("could not connect: password=topsecret host=db", "topsecret"),
        ("invalid dsn password = hunter2 port=5432", "hunter2"),
    ],
)
def test_redact_removes_credentials(raw: str, secret: str) -> None:
    """``/api/health`` 在公网演示站上是匿名可访问的。

    驱动库的报错**通常**只带 host:port，但「通常」不是保证 ——
    一条把密码漏进 HTTP 响应体的报错就是一次真实的凭据泄露。
    """
    cleaned = redact(raw)
    assert secret not in cleaned
    assert "***" in cleaned, "脱敏后应留下占位符，而不是把整段删掉"


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        "postgresql://sfly:hunter2@db:5432 failed",
        "redis://default:s3cr3t@cache:6379 refused",
        "password=topsecret host=db",
    ],
)
def test_redact_keeps_the_host_visible(raw: str) -> None:
    """脱敏**只**该删凭据。

    把整条错误换成一句「连接失败」会让排查从「看一眼健康页」退化成
    「登进容器翻日志」—— 而健康页存在的意义正是省掉这一步。
    """
    cleaned = redact(raw)
    assert "db" in cleaned or "cache" in cleaned


@pytest.mark.unit
def test_down_result_is_redacted_even_if_caller_forgets() -> None:
    """脱敏放在 ``down()`` 里而不是调用方，是因为调用方迟早会忘。"""
    r = down("postgres", "failed: postgresql://u:hunter2@db/x")
    assert "hunter2" not in r.detail
    assert r.status is CheckStatus.DOWN


@pytest.mark.unit
def test_redis_safe_url_strips_password() -> None:
    """启动日志会打到容器 stdout，而 ``docker compose logs`` 经常被整段贴出去。"""
    assert _safe_url("redis://user:sekret@host:6379/0") == "redis://host:6379/0"
    assert _safe_url("redis://host:6379/0") == "redis://host:6379/0"


# --------------------------------------------------------------------------- #
# Postgres
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_postgres_ping_against_unreachable_db_reports_down() -> None:
    """连不上时报告 ``down``，而**不是抛异常**。

    健康检查自己崩掉是最没价值的一种失败：调用方正是为了知道「坏没坏」才调它。
    """
    pool = PostgresPool(DEAD_PG, min_size=1, max_size=2, connect_timeout_s=2, ping_timeout_s=6.0)
    await pool.open()
    try:
        r = await pool.ping()
    finally:
        await pool.close()

    assert r.name == "postgres"
    assert r.status is CheckStatus.DOWN
    assert r.ok is False
    assert r.detail.strip() != ""
    # 密码不能出现在健康页上（/api/health 在公网演示站是匿名可访问的）
    assert "nopw" not in r.detail
    # 这一条测的是「连接被拒绝」，报文里必须**不能**出现「挂起」——
    # 那是另一条分支（连上了但服务端不回应）。两者处置方式完全不同：
    # 前者去查数据库有没有起来，后者去查网络分区 / 服务端假死。
    assert "挂起" not in r.detail


@pytest.mark.unit
async def test_postgres_ping_distinguishes_refused_from_hung() -> None:
    """「连不上」和「连上了但不回应」必须报成两种不同的原因。

    这条是回归测试：默认配置一度是 ping 预算 5 秒、连接超时 10 秒 ——
    内层比外层还长，于是任何连接失败都会先撞上整体超时，
    报出来的永远是「服务端挂起 / 网络分区」。真实原因（端口没开、库没起来）
    被一句听起来更严重的话盖掉了。

    现在内层固定取预算的 60%，所以这里能拿到连接层的真实错误。
    """
    pool = PostgresPool(
        "postgresql://nobody:nopw@127.0.0.1:1/nope",
        min_size=1,
        max_size=2,
        connect_timeout_s=2,
        ping_timeout_s=6.0,
    )
    await pool.open()
    try:
        r = await pool.ping()
    finally:
        await pool.close()

    assert r.status is CheckStatus.DOWN
    assert "挂起" not in r.detail, f"连接被拒绝被误报成了服务端挂起：{r.detail}"
    assert r.latency_ms < 5500, f"应当在整体超时之前就失败，实测 {r.latency_ms:.0f}ms"


@pytest.mark.unit
async def test_postgres_open_does_not_raise_when_db_is_down() -> None:
    """数据库不可达时**进程也要能起来**。

    崩溃退出会让 Docker 把它拖进无限重启循环，而重启日志会把真正的错误冲掉。
    起来的进程能在 ``/api/health`` 里说清楚哪里坏了 —— 这才可诊断。
    """
    pool = PostgresPool(DEAD_PG, min_size=1, max_size=2, connect_timeout_s=2, ping_timeout_s=0.3)
    await pool.open()  # 不抛
    await pool.close()  # 也不抛


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_redis_unreachable_reports_down_with_a_readable_error_and_within_bounds() -> None:
    """Redis 不可达时的三件事一起验：状态是 down、信息可读、耗时有限。

    合在一起测是因为它们共用一次真实连接尝试 —— 拆成三个测试就是把同一段
    两秒的等待付三遍（Windows 的 SelectorEventLoop 报告一个被拒绝的连接约需
    2 秒；Linux 上是毫秒级）。

    这里防的是一个已经踩到的坑：用 ``wait_for`` 超时取消时抛出来的是
    ``asyncio.TimeoutError``，而它的 ``str()`` 是**空字符串** —— 健康页会显示成
    ``TimeoutError:`` 后面什么都没有，恰好把唯一有用的信息丢掉。
    现在探测客户端不重试、连接超时留足余量（见 ``_PING_CONNECT_TIMEOUT_S``），
    所以能拿到真正的 ``ConnectionError``。

    耗时上限给到 8 秒是宽松的：它防的是「某天有人把重试加回来」或
    「连接超时被调大」，不是防边界抖动。
    """
    q = RedisStreamsQueue(DEAD_REDIS, client_name="test")
    await q.start()  # Redis 不可达时 start() 也不该抛
    try:
        t0 = time.perf_counter()
        r = await q.ping()
        elapsed = time.perf_counter() - t0
    finally:
        await q.close()

    assert r.name == "redis"
    assert r.status is CheckStatus.DOWN
    # 不能是 "TimeoutError:" 这种没有下半句的形式
    assert r.detail.strip() != ""
    assert r.detail != "TimeoutError:"
    assert "127.0.0.1" in r.detail or "refused" in r.detail.lower() or "10061" in r.detail
    assert elapsed < 8.0, f"探测耗时 {elapsed:.2f}s，健康接口会被拖垮"


@pytest.mark.unit
async def test_redis_start_does_not_retry_when_unreachable() -> None:
    """``start()`` 必须**快速失败**，不能带着重试去等 Redis。

    这条是实测出来的问题：``start()`` 原本用长驻客户端那套配置（3 次指数退避
    + 5 秒连接超时），Redis 不可达时最坏要**卡 15 秒以上**。而 ``start()`` 是在
    FastAPI 的 lifespan 里被 await 的 —— 意味着 Redis 一挂，API 就迟迟不 ready，
    Docker healthcheck 一直不绿，``tasks.py up --wait`` 一直不返回。

    该发生的是「快点起来，然后在 /api/health 里说清楚 Redis 连不上」。
    """
    q = RedisStreamsQueue(DEAD_REDIS, client_name="test")
    t0 = time.perf_counter()
    await q.start()
    elapsed = time.perf_counter() - t0
    await q.close()

    # 单次尝试 + 3 秒连接超时，再给平台本身的延迟留点余量
    assert elapsed < 8.0, f"start() 耗时 {elapsed:.2f}s，容器启动会被拖住"


@pytest.mark.unit
def test_group_name_is_derived_from_worker_type() -> None:
    """消费者组名必须能从 worker_type 推出来 —— ``--scale`` 靠它做同组竞争消费。"""
    assert group_for("security") == "security-group"
    assert group_for("performance") == "performance-group"


# --------------------------------------------------------------------------- #
# 工厂：唯一的拓扑分支点
# --------------------------------------------------------------------------- #


def _dead_settings(**over: object) -> Settings:
    """指向死地址、并且把所有超时都拧到最小的配置。

    ``db_connect_timeout_s`` 尤其重要：池子 ``close()`` 时会等待正在进行的那次
    连接尝试结束，所以它直接决定每个测试的收尾耗时。
    """
    base: dict[str, object] = {
        "database_url": DEAD_PG,
        "redis_url": DEAD_REDIS,
        "db_connect_timeout_s": 2,
        # 必须明显大于「连接被拒绝」的耗时（Windows 上约 2 秒），
        # 否则测到的是整体超时而不是连接错误 —— 那是另一条分支。
        "db_ping_timeout_s": 6.0,
        "db_pool_min": 1,
        "db_pool_max": 2,
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.mark.unit
async def test_memory_backend_opens_no_redis_connection() -> None:
    """精简模式下**根本不该去连 Redis**。

    报 ``skipped`` 而不是 ``down``：这个依赖在当前拓扑下不存在，
    报故障是撒谎，报正常也是撒谎。
    """
    deps = await open_dependencies(_dead_settings(queue_backend="memory", lock_backend="memory"))
    try:
        assert deps.queue is None
        report = await deps.probe()
    finally:
        await deps.close()

    redis = report.as_dict()["checks"]["redis"]
    assert redis["status"] == "skipped"
    assert redis["ok"] is True
    assert "精简模式" in redis["detail"]


@pytest.mark.unit
async def test_redis_backend_actually_probes_redis() -> None:
    """完整模式下 Redis 是**真实探测**的，不是照抄配置值。"""
    deps = await open_dependencies(_dead_settings(queue_backend="redis", lock_backend="redis"))
    try:
        assert deps.queue is not None
        report = await deps.probe()
    finally:
        await deps.close()

    checks = report.as_dict()["checks"]
    assert set(checks) == {"postgres", "redis"}
    # 两个依赖都指向死地址，所以两个都必须报 down ——
    # 一个「照抄配置」的实现会在这里报 ok
    assert checks["redis"]["status"] == "down"
    assert report.ok is False


@pytest.mark.unit
async def test_probe_survives_a_probe_that_raises() -> None:
    """单个探测函数崩了，不能让整个健康接口 500。

    探测函数内部已经各自吞异常了，这里是第二道保险：防的是将来有人
    新写一个探测函数时忘了吞。那时表现应该是「那一个依赖报 down」，
    而不是「健康接口整体不可用」。
    """

    async def boom() -> CheckResult:
        raise RuntimeError("探测函数自己崩了")

    async def fine() -> CheckResult:
        return ok("postgres", "PostgreSQL 16.4")

    deps = Dependencies(postgres=None, queue=None, _probes=[("postgres", fine), ("redis", boom)])  # type: ignore[arg-type]
    report = await deps.probe()

    checks = report.as_dict()["checks"]
    assert checks["postgres"]["status"] == "ok"
    assert checks["redis"]["status"] == "down"
    assert "探测函数自己崩了" in checks["redis"]["detail"]


@pytest.mark.unit
async def test_probe_runs_dependencies_concurrently() -> None:
    """并发探测：三个各睡 0.2s 的依赖应该在约 0.2s 内全部返回，而不是 0.6s。

    串行版本的响应时间等于各依赖超时之和 —— 依赖越多，健康接口越慢，
    而它恰恰是最不该慢的那个接口（负载均衡和人都靠它判断要不要继续打流量）。
    """

    async def slow(name: str) -> CheckResult:
        await asyncio.sleep(0.2)
        return ok(name, "ok")

    deps = Dependencies(
        postgres=None,  # type: ignore[arg-type]
        queue=None,
        _probes=[("a", lambda: slow("a")), ("b", lambda: slow("b")), ("c", lambda: slow("c"))],
    )
    t0 = time.perf_counter()
    report = await deps.probe()
    elapsed = time.perf_counter() - t0

    assert len(report.checks) == 3
    assert elapsed < 0.45, f"耗时 {elapsed:.2f}s，看起来是串行执行的"


@pytest.mark.unit
async def test_close_is_safe_when_queue_was_never_opened() -> None:
    """关机路径上不能抛：这时候 Docker 已经在拆容器了，没人会看到那条错误。"""
    deps = Dependencies(postgres=None, queue=None, _probes=[])  # type: ignore[arg-type]
    await deps.close()
    await deps.close()  # 重复调用也要安全
