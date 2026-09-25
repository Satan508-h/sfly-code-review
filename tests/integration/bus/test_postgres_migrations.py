"""迁移器对着**真实的** Postgres 跑。

``tests/unit/bus/test_migrations.py`` 测的是「文件读得对不对」（版本连续、
指纹稳定、CHECK 约束和枚举一致），全都不需要数据库。这个文件测的是那些
**只有真库才能回答**的问题：

* 空库上建得起来吗（本地容器和 Neon 都是这么开始的）
* 同时起五个容器会发生什么（完整模式下的常态，不是边角情况）
* 有人改了已应用的迁移会怎样
* 一个迁移失败之后，库是干净的还是一半一半

最后一条是这里最有价值的一条：**「全程一个事务」是个声明，声明要被验证。**
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import psycopg
import pytest

from postgres_support import drop_all_tables, postgres_test_dsn, table_names
from sfly_bus.migrations import (
    MIGRATIONS_DIR,
    Migration,
    MigrationDriftError,
    load_migrations,
    run_migrations,
)
from sfly_bus.postgres import PostgresPool, PostgresRunStore, migrate_on_startup

pytestmark = pytest.mark.integration

#: 建完表之后应该有的表名。与 ``postgres_support.BUSINESS_TABLES`` 分开写：
#: 那一份是「测试前要清空的表」，这一份是「迁移应该建出来的表」——
#: 两份清单的差异（比如记账表）正是要看得见的东西。
EXPECTED_TABLES = {
    "review_runs",
    "worker_results",
    "findings",
    "review_reports",
    "run_events",
    "llm_calls",
    "webhook_deliveries",
    "schema_version",
}


async def test_an_empty_database_gets_the_full_schema(store: PostgresRunStore) -> None:
    """从**一张表都没有**的库开始，migrate() 应该建出全部结构。

    这条测试之所以不嫌麻烦地先把表删干净：本地 Docker 的库和 Neon 的库都是
    从空开始的（``init.sql`` 只建扩展，不建表 —— 见 infra/postgres/init.sql）。
    一个只在「已经建好的库」上验证过的迁移器，恰好没有验证过唯一要紧的那条路径。
    """
    drop_all_tables()
    assert table_names() == set()

    await store.migrate()

    assert table_names() == EXPECTED_TABLES
    assert len(load_migrations()) >= 1


async def test_migrate_is_idempotent(store: PostgresRunStore, raw_pg: psycopg.Connection[Any]) -> None:
    """跑三次和跑一次的结果一样 —— 五个容器每次启动都会调它。"""
    await store.migrate()
    await store.migrate()
    await store.migrate()

    rows = raw_pg.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
    assert [r[0] for r in rows] == [m.version for m in load_migrations()]
    # 条数 == 迁移文件数。写死成 1 的话，加第二个迁移文件时这条断言会红，
    # 而那正是它该做的事 —— 但红的理由要说清楚是「文件数对不上」，
    # 所以这里直接和 load_migrations() 比。
    count = raw_pg.execute("SELECT count(*) FROM schema_version").fetchone()
    assert count == (len(load_migrations()),)


async def test_five_processes_migrating_at_once_create_one_schema() -> None:
    """**完整模式的常态**：api / orchestrator / 三个 Worker 同时启动，
    五个进程一起进 ``migrate()``。

    没有 ``pg_advisory_xact_lock`` 的话，这里会看到 ``schema_version`` 的主键冲突
    或者 ``CREATE TABLE`` 的竞态报错 —— 而症状是容器进入重启循环，
    看起来像「数据库有问题」。

    用五个独立的池子（而不是一个池子发五个协程）是为了真的走五条连接：
    同一条连接上的五个协程是串行的，那样测不到任何东西。
    """
    drop_all_tables()
    pools = [PostgresPool(postgres_test_dsn(), min_size=1, max_size=1, connect_timeout_s=5) for _ in range(5)]
    try:
        for pool in pools:
            await pool.open()
        stores = [PostgresRunStore(pool) for pool in pools]

        results = await asyncio.gather(*(s.migrate() for s in stores), return_exceptions=True)

        failures = [r for r in results if isinstance(r, BaseException)]
        assert failures == [], f"并发迁移里有失败：{failures}"
    finally:
        for pool in pools:
            await pool.close()

    assert table_names() == EXPECTED_TABLES
    with psycopg.connect(postgres_test_dsn()) as conn:
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone() == (len(load_migrations()),)


async def test_a_changed_migration_file_is_refused(
    store: PostgresRunStore, raw_pg: psycopg.Connection[Any]
) -> None:
    """已应用的迁移被改了 → 启动直接失败，而不是「顺便提醒一下」。

    这是整个迁移器最重要的一条规则：库里是旧结构、仓库里是新结构，
    **两边都能跑**，于是所有测试都绿 —— 直到某条新写的查询去读一列还不存在的字段。
    那个 bug 的现场和原因隔得很远，所以宁可在这里拦住。

    ``migrate_on_startup`` 对这一类的处置是**继续往上抛**（和连不上数据库相反），
    所以这里一并断言它不会把漂移吞掉。

    这条测试会破坏记账表，所以必须**自己收拾干净**：每个测试开始前的清空动作
    刻意不碰 ``schema_version``（清了它，下一次 ``migrate()`` 会去建一堆已存在的表）。
    留着被篡改的指纹的话，**下一条测试**会在它的 fixture 里撞上漂移 ——
    而它挂的原因和它要测的东西毫无关系。
    """
    await store.migrate()
    original = raw_pg.execute("SELECT checksum FROM schema_version WHERE version = 1").fetchone()
    assert original is not None  # 上一步刚迁移完，这一行必然在

    try:
        raw_pg.execute("UPDATE schema_version SET checksum = 'tampered' WHERE version = 1")

        with pytest.raises(MigrationDriftError) as excinfo:
            await store.migrate()

        message = str(excinfo.value)
        assert "002" in message, "报错里要给出正确的做法（加一个新的迁移文件）"
        assert "clean" in message, "以及本地试验时的出路"

        # 启动路径同样不能把它吞掉
        with pytest.raises(MigrationDriftError):
            await migrate_on_startup(store)
    finally:
        raw_pg.execute("UPDATE schema_version SET checksum = %s WHERE version = 1", [original[0]])


async def test_a_failing_migration_leaves_no_trace(store: PostgresRunStore) -> None:
    """一个迁移失败 → 库回到迁移前的样子。**「全程一个事务」是个声明，这条验证它。**

    故意让第二条语句失败（插入一张不存在的表）：第一条 ``CREATE TABLE`` 是**成功执行过**的，
    所以只有事务回滚能解释它为什么不见了。整批语句在解析阶段就失败的话，
    这条测试什么也没证明 —— 那种情况下 Postgres 根本不会执行任何一条。

    迁移里出错的性质决定了这条为什么重要：结构改一半的库既不是旧结构也不是新结构，
    而代码对它的判断是随机的。
    """
    await store.migrate()
    before = table_names()

    # 版本号取「现有迁移数 + 1」而不是写死 2：**写死的话，加一个真实的迁移文件
    # 就会让这条测试变成「版本号重复」，而它要测的东西（事务回滚）根本没跑到。**
    # 路径同理 —— 它必须是一个不存在的文件名，不然会和真实迁移撞上。
    next_version = len(load_migrations()) + 1
    half_applied = Migration(
        version=next_version,
        name="deliberately_broken",
        path=MIGRATIONS_DIR / f"{next_version:03d}_deliberately_broken.sql",
        sql="CREATE TABLE should_not_survive (x int);\nINSERT INTO no_such_table VALUES (1);",
        checksum="not-compared-because-it-was-never-applied",
    )

    async with await psycopg.AsyncConnection.connect(postgres_test_dsn()) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable):
            await run_migrations(conn, migrations=(*load_migrations(), half_applied))

    assert table_names() == before, "失败的那条语句之前的 CREATE TABLE 也必须回滚掉"
    with psycopg.connect(postgres_test_dsn()) as conn:
        assert conn.execute("SELECT count(*) FROM schema_version").fetchone() == (len(load_migrations()),)


def test_the_migrations_directory_is_reachable_from_the_installed_package() -> None:
    """迁移 SQL 是**数据文件**，不是 Python 模块 —— 打包时最容易被漏掉的东西。

    ``Dockerfile`` 里是 ``COPY packages ./packages`` + editable 安装，所以文件
    在镜像里也在；但这条断言是那种「一旦不成立，症状会出现在部署之后」的检查，
    而那时候要排查的是「容器里为什么没有这个文件」。它顺便钉住了一件事：
    ``__file__`` 解析出来的目录在源码树里是对的（不用 importlib.resources 的理由
    写在这个常量旁边）。
    """
    assert (MIGRATIONS_DIR / "001_init.sql").is_file()
    assert Path(MIGRATIONS_DIR).name == "migrations"
