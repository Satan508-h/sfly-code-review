"""集成测试的公共设施：确认 Redis 与 Postgres 可达，然后每个测试前扫一次地。

### 依赖不可达时是**失败**，不是 skip

``pytest.skip`` 在这里是错的：跑 ``python tasks.py test-int`` 的人明确要求了
「需要外部依赖」的那一层测试。把依赖缺失报成 skip，结果是
``30 skipped`` 配一个绿色的退出码 —— 而这是**假装有保护**最典型的样子，
CLAUDE.md 里那句「声明了却在别处不执行的门槛比没有门槛更糟」说的就是这个。

所以这里用 ``pytest.exit`` 直接停下整个会话，给一条能照做的提示。它比
「每个测试各失败一次」好：后者会在屏幕上刷 30 遍同一句话，真正的原因反而被埋掉。

### 两个依赖分开报

先探 Redis 再探 Postgres，各自给各自的提示。合成一句「依赖不可达」的话，
只有一个依赖没起来的人要去猜是哪一个 —— 而这恰好是本地最常见的形态
（``docker compose up redis`` 起了，忘了 Postgres）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import psycopg
import pytest

from postgres_support import postgres_test_dsn, raw_conn, recreate_test_db, truncate_all

# 空行不是随手加的：``postgres_support`` / ``redis_support`` 住在 ``tests/`` 下，
# 而 ``tests`` 是 ruff 配置里的一个 src 根，所以它们被算作**本仓库的模块**、
# 要和第三方分开。
from postgres_support import probe as probe_postgres
from redis_support import flush_test_db
from redis_support import probe as probe_redis
from sfly_bus.postgres import PostgresPool, PostgresRunStore

#: 测试用的 run deadline。断言里算区间时引用它，避免测试里散落魔法数字。
DEADLINE_S = 600


@pytest.fixture(scope="session", autouse=True)
def _require_redis() -> None:
    ok, detail = probe_redis()
    if not ok:
        pytest.exit(
            "Redis 不可达，集成测试无法运行：\n"
            f"  {detail}\n"
            "  先起依赖：python tasks.py up redis\n"
            "  （测试跑在 db 15，与开发用的 db 0 分开）",
            returncode=1,
        )


@pytest.fixture(scope="session", autouse=True)
def _require_postgres() -> None:
    ok, detail = probe_postgres()
    if not ok:
        pytest.exit(
            "Postgres 不可达，集成测试无法运行：\n"
            f"  {detail}\n"
            "  先起依赖：python tasks.py up postgres\n"
            "  （测试用独立的 <库名>_test 库，每次会话开始时重建）",
            returncode=1,
        )


@pytest.fixture(scope="session", autouse=True)
def _fresh_test_db(_require_postgres: None) -> None:
    """会话开始时把测试库删掉重建。

    见 ``postgres_support.recreate_test_db`` 的说明。这里只强调一件事：
    **每次会话都是从空库开始，所以每一次 ``migrate()`` 都在跑「新库上能不能建起来」
    这条真实路径** —— 本地 Docker 的库和 Neon 的库都是从空开始的，
    而一个只在「已经建好的库」上验证过的迁移器什么也没证明。
    """
    recreate_test_db()


@pytest.fixture(autouse=True)
def _clean_redis() -> None:
    """每个测试开始前清空测试库（Redis 的 db 15）。

    每个测试自带干净状态，测试之间就不会通过数据库互相串联 —— 那种耦合的表现是
    「单跑通过、全跑失败」，而定位它要花的时间远超过这里每次多一次 FLUSHDB。
    """
    flush_test_db()


@pytest.fixture(autouse=True)
def _clean_postgres(_fresh_test_db: None) -> None:
    """每个测试开始前清空全部业务表（结构留着）。"""
    truncate_all()


@pytest.fixture
async def store() -> AsyncIterator[PostgresRunStore]:
    """连到测试库的仓储。**函数级**，不是会话级。

    会话级的异步 fixture 会被绑定到**会话**的事件循环上，而 pytest-asyncio 的
    默认 ``asyncio_default_fixture_loop_scope`` 是 ``function`` —— 于是测试跑在
    另一个循环里，用的是上一个循环里创建的连接。表现是随机的
    ``attached to a different loop`` 而不是稳定失败。函数级没有这个问题，
    代价是每个测试重建一次池子（实测几十毫秒）。
    """
    pool = PostgresPool(postgres_test_dsn(), min_size=1, max_size=2, connect_timeout_s=5, ping_timeout_s=3.0)
    await pool.open()
    store = PostgresRunStore(pool, run_deadline_s=DEADLINE_S)
    await store.migrate()
    try:
        yield store
    finally:
        await pool.close()


@pytest.fixture
def raw_pg() -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """裸连接。只在原生命令才能构造出输入的地方用 —— 见 ``postgres_support.raw_conn``。"""
    conn = raw_conn()
    try:
        yield conn
    finally:
        conn.close()
