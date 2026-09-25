"""集成测试用的 Postgres 铺路石。

**测试跑在单独的 ``sfly_test`` 库，不是开发用的 ``sfly`` 库。**

Redis 那边用「库号 15」做隔离（Redis 一个实例有 16 个库），Postgres 没有这回事 ——
一个实例里的库是各自独立的数据库。所以这里的做法是：从 ``DATABASE_URL``
**派生**一个 ``<原名>_test``，并在每个测试会话开始时**删掉重建**。

每会话重建带来两件事，都是想要的：

* 测试之间不可能通过数据库互相串联，也不会因为上一次跑残留的结构而失败；
* ``migrate()`` 每次都是在一张**白纸**上跑的 —— 而「新库上能不能建起来」正是
  这个迁移器唯一真正需要被证明的事。本地库和 Neon 都是从空库开始的。

代价是每次跑集成测试会丢掉那个库里的数据。所以下面每一条会动数据的函数都先过一遍
:func:`assert_test_db`：库名不以 ``_test`` 结尾就拒绝动手。一条写错的配置不该能删掉
真实的数据 —— 这类保护的成本是几行代码，收益是「不可能发生」。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import psycopg

from sfly_shared.config import get_settings

#: 测试库名的后缀。**也是保护条件本身** —— 见 :func:`assert_test_db`。
TEST_DB_SUFFIX = "_test"

#: 建库/删库要连的库。``postgres`` 在所有安装里都存在。
ADMIN_DB = "postgres"

#: 探测与建库用的短超时。连不上就立刻报错，不要让人干等。
_CONNECT_TIMEOUT_S = 5

#: 会被每次测试前清空的业务表。
#:
#: **加了表就要同步这里。** 漏一张的症状是「单跑通过、全跑失败」——
#: 上一条测试的数据留在表里，而下一条测试以为它是干净的。
#: :func:`truncate_all` 会拿这张清单和数据库里的实际表核对，所以漏掉会报错而不是静默。
BUSINESS_TABLES: tuple[str, ...] = (
    "review_runs",
    "worker_results",
    "findings",
    "review_reports",
    "run_events",
    "llm_calls",
)

#: 迁移器的记账表。**不参与清空** —— 清了就等于假装这个库没建过表，
#: 而表还在，于是下一次 migrate() 会去 CREATE 一堆已存在的表。
SCHEMA_VERSION_TABLE = "schema_version"

#: LangGraph checkpointer 的表（M5 起出现）。它们不归我们的迁移器管，
#: 也不参与清空 —— 见 tests/integration/bus/test_migrations.py 里的核对逻辑。
CHECKPOINT_PREFIX = "checkpoint"


def _dsn_with_db(db: str) -> str:
    """``DATABASE_URL``，库名换成 ``db``。

    从 ``Settings`` 取而不是直接读环境变量：开发机的 ``.env`` 把宿主机端口改成了
    55432（5432 被另一个项目占着），而 CI 里没有 ``.env``、用默认的 5432。
    两条路径都由 ``Settings`` 负责，这里不重复一遍。
    """
    parts = urlsplit(get_settings().database_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{db}", parts.query, parts.fragment))


def test_db_name() -> str:
    """测试库名 = 开发库名 + ``_test``。"""
    base = urlsplit(get_settings().database_url).path.lstrip("/") or "sfly"
    return f"{base}{TEST_DB_SUFFIX}"


def postgres_test_dsn() -> str:
    """测试库的连接串。集成测试里所有连接都用它。

    **名字刻意不以 ``test_`` 开头。** 它会被各个测试文件 import 进自己的模块命名空间，
    而 pytest 会把模块里任何叫 ``test_*`` 的可调用对象当成一条测试收集 ——
    它原本叫 ``test_dsn``，于是每个 import 它的文件都多出一条「测试」：
    什么都不检查、只是返回一个字符串，但**收集到的用例数是正常的**，
    所以没有人会注意到。换名字比在每个文件里写 alias 可靠。
    """
    return _dsn_with_db(test_db_name())


def admin_dsn() -> str:
    """连到 ``postgres`` 库 —— 建库/删库必须在别的库里执行。"""
    return _dsn_with_db(ADMIN_DB)


def assert_test_db(dsn: str) -> None:
    """拒绝在名字不以 ``_test`` 结尾的库上做破坏性动作。"""
    name = urlsplit(dsn).path.lstrip("/")
    if not name.endswith(TEST_DB_SUFFIX):
        raise RuntimeError(
            f"拒绝对 {name!r} 做集成测试的清理动作 —— 库名必须以 {TEST_DB_SUFFIX!r} 结尾。（dsn={dsn}）"
        )


def probe() -> tuple[bool, str]:
    """Postgres 可达吗？返回 ``(可达, 说明)``。

    不抛异常：调用它的是「整个会话该不该跑」的判断，而失败时最需要的是
    **一句能读的原因**，不是一串回溯。
    """
    try:
        # 同步连接：这是「测试开始前扫一下地」的活，不参与事件循环。
        with psycopg.connect(admin_dsn(), connect_timeout=_CONNECT_TIMEOUT_S) as conn:
            row = conn.execute("SHOW server_version").fetchone()
            version = row[0] if row else "?"
        return True, f"PostgreSQL {version} @ {postgres_test_dsn()}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def recreate_test_db() -> str:
    """删掉再重建测试库，返回它的 dsn。**每次会话开始时调一次。**

    两个不显然的地方：

    * ``autocommit=True`` 是必须的。``CREATE DATABASE`` / ``DROP DATABASE``
      **不能在事务块里执行**，而 psycopg 默认会替你开一个事务 ——
      报错是 ``CREATE DATABASE cannot run inside a transaction block``，
      读起来像 SQL 写错了，其实是连接模式的问题。
    * ``WITH (FORCE)``（PG 13+）踢掉仍然连着的会话。上一次跑测试时崩掉的进程
      可能把连接留在那儿，而那时 ``DROP DATABASE`` 会失败并提示「还有 N 个连接」——
      对着一个测试库说这种话，除了让人去手工 kill 之外没有任何指导意义。
    """
    dsn = postgres_test_dsn()
    assert_test_db(dsn)
    name = test_db_name()
    with psycopg.connect(admin_dsn(), autocommit=True, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{name}"')
    return dsn


def drop_all_tables() -> None:
    """把库清成**空库**（连表都没有）。迁移测试用它模拟「新库」。

    比 ``DROP DATABASE`` 温和：会话级的连接池还开着，删库会把它一起踢掉，
    而接下来那条测试要用的正是那个池子。
    """
    dsn = postgres_test_dsn()
    assert_test_db(dsn)
    with psycopg.connect(dsn, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


def table_names() -> set[str]:
    """库里现有的表名（public 模式）。"""
    dsn = postgres_test_dsn()
    assert_test_db(dsn)
    with psycopg.connect(dsn, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
        rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    return {str(r[0]) for r in rows}


def truncate_all() -> None:
    """清空全部业务表（结构留着）。**每个测试开始前调一次。**

    ``RESTART IDENTITY`` 让 ``run_events.seq`` / ``findings.id`` 从 1 重新开始 ——
    于是断言可以写 ``events[0].seq == 1`` 这种确定的形式，而不用先记一个基准值。

    ``CASCADE`` 处理外键：清单里的表互相有引用，不写它得按依赖顺序删。

    最后一遍核对表清单：数据库里有而清单里没有的表，会让测试之间通过残留数据串联，
    而那是「单跑通过、全跑失败」这种最难查的形态。宁可在这里报错。

    ### 表还不存在时**静默返回**

    这是全文件唯一一处「什么都不做还不吭声」，所以要说清楚为什么。

    会话开始时库是**空的**（``recreate_test_db`` 刚建的），而建表由第一个用到
    ``store`` 的测试触发（它的 fixture 会调 ``migrate()``）。也就是说
    「库是空的」在本会话里是一个**合法状态**，而不是异常 —— 迁移测试还会主动
    把表全删掉来模拟新库。在这里报错的话，第一个测试必挂，而它挂的原因和
    它要测的东西毫无关系。
    """
    assert_test_db(postgres_test_dsn())
    actual = table_names()
    if not set(BUSINESS_TABLES) <= actual:
        return

    known = set(BUSINESS_TABLES) | {SCHEMA_VERSION_TABLE}
    # LangGraph 的 checkpoint 表不归我们管，留着
    unexpected = {t for t in actual if t not in known and not t.startswith(CHECKPOINT_PREFIX)}
    if unexpected:
        raise RuntimeError(
            f"数据库里有 {sorted(unexpected)}，但 tests/postgres_support.py 的 "
            "BUSINESS_TABLES 里没有 —— 新加的表必须一起清，否则测试之间会通过残留数据串联。"
        )

    with psycopg.connect(postgres_test_dsn(), autocommit=True, connect_timeout=_CONNECT_TIMEOUT_S) as conn:
        conn.execute(f"TRUNCATE {', '.join(BUSINESS_TABLES)} RESTART IDENTITY CASCADE")


def raw_conn(*, autocommit: bool = True) -> psycopg.Connection[tuple[object, ...]]:
    """裸连接。**只在原生命令才能构造出输入的地方用。**

    正常路径一律走 ``PostgresRunStore`` —— 伸手进原生 SQL 的测试必须说清楚为什么，
    否则它慢慢就变成了「绕过实现测实现」。目前的两个正当理由：
    构造「14 天前的 run」这种时间上够不着的状态（``UPDATE created_at``），
    以及破坏 ``schema_version`` 来验证漂移检测真的会报错。
    """
    return psycopg.connect(postgres_test_dsn(), autocommit=autocommit, connect_timeout=_CONNECT_TIMEOUT_S)
