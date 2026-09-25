"""迁移器 —— 纯 SQL 文件 + 一张记账表，不上 Alembic。

### 为什么不上 Alembic

整个 schema 只有六七张表、一个人维护、两种部署（本地容器 / Neon）。Alembic 带来
的是 autogenerate 和 downgrade，而这两样恰好是我们不需要的：autogenerate 要
比对活的库（于是「迁移」这件事反而依赖连得上库），downgrade 在「改坏了就重建」
更省事的场景里是纯负担。**代价是我们要自己写二十行记账逻辑，收益是它一眼能读完。**

### 三条规则

1. **一个文件一个版本，按文件名前缀升序应用。** 版本号必须从 1 连续（不跳号）——
   跳号意味着有文件在合并里丢了，而那只会在「新库上跑迁移」时才暴露：
   已经迁到 5 的库上一切正常，空库上少建一张表。所以宁可启动时直接报错。
2. **已应用的迁移不能再改。** 内容变了（sha256 不同）就直接报
   :class:`MigrationDriftError`，不尝试修复。**没有「顺便提醒一下」这个选项**：
   本地库是旧的、仓库里是新的，两边都「能跑」，于是所有测试都绿 ——
   直到某个新写的查询去读一列还不存在的字段。改结构请加 ``002_*.sql``。
3. **整段 SQL 一次性执行，永远不传参数。** 见下面那段实测记录 —— 这不是风格问题，
   是 psycopg 的协议边界。

### 并发：五个容器一起启动只有一个会真的动手

``pg_advisory_xact_lock`` 把迁移串行化。完整模式下 api / orchestrator / 三个 Worker
都会在启动时调 ``migrate()``，它们**同时**去建表是常态而不是边角情况。
没有这把锁的表现是 ``schema_version`` 主键冲突或 ``CREATE TABLE`` 竞态报错，
然后容器进入重启循环 —— 而这看起来像「数据库有问题」。

锁是 ``_xact_`` 版本：事务一结束（提交或回滚）自动释放，不会因为进程被杀而锁死。
拿不到锁的那几个会**等**，等到的结果是「已经全部应用过了，什么都做」（因为
先到的那个已经提交了）。

### 实测记录：为什么迁移里不能带参数

psycopg 3 在**不传参数**时用简单查询协议，Postgres 自己按分号切分语句，所以
一整份迁移文件可以一次 ``execute()``。一旦带上参数，psycopg 改用扩展协议
（extended query protocol），而扩展协议**一次只允许一条语句**：

    >>> conn.execute("CREATE TABLE a(x int); CREATE TABLE b(y int);")
    OK
    >>> conn.execute("SELECT %s; SELECT %s", [1, 2])
    SyntaxError: cannot insert multiple commands into a prepared statement

所以 ``run_migrations`` 里的 ``execute`` 一律不带参数，带参数的语句
（写 ``schema_version``）单独发。顺带一个推论：迁移 SQL 里可以随便用 ``%``，
不用转义成 ``%%``（那是带参数时才要操心的事）。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg.rows import dict_row

from sfly_shared.logging import get_logger

if TYPE_CHECKING:
    from psycopg import AsyncConnection

log = get_logger(__name__)

#: 迁移文件所在目录。用 ``__file__`` 而不是 ``importlib.resources``：这个包在
#: 三种安装形态下（源码树、editable、wheel）文件都在真实磁盘上，没有 zip 导入。
MIGRATIONS_DIR = Path(__file__).resolve().parent

#: 记账表。由迁移器自己建，不属于任何一个 ``NNN_*.sql``。
SCHEMA_VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    -- 内容指纹。比 version 更能说明问题：有人改了 001 却没换代，版本号一模一样。
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""

#: 迁移的串行化锁。取值只需全库唯一 —— 它只在这一次迁移里和其他迁移抢。
LOCK_KEY = 0x5F1F001

#: 等锁的上限。**不能无限等**：五个容器一起启动时，如果某个长事务（比如有人手工
#: 开着事务在改表）持着锁，无限等的结果是五个容器全部卡在启动阶段、
#: healthcheck 一直不绿，而日志里只有一行「正在迁移」。有上界就是一条能读的错误。
LOCK_TIMEOUT = "15s"

#: 单条语句的上限。给得宽松（真实的 CREATE INDEX 可能很慢），只用来挡住「永远不返回」。
STATEMENT_TIMEOUT = "300s"

_FILE_RE = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    """迁移失败。启动路径上会把它当作「这个进程不该继续跑」处理。"""


class MigrationDriftError(MigrationError):
    """已应用的迁移内容变了。

    单独一个类型是因为调用方的处置**完全不同**：连不上数据库可以继续跑
    （进程起来、``/api/health`` 说清楚），而漂移必须停下来 ——
    代码期待的结构和数据库里的不是一回事，继续跑就是往错的结构上写数据。
    """


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str

    def __str__(self) -> str:
        return f"{self.version:03d}_{self.name}"


def _checksum(sql: str) -> str:
    """内容指纹。

    先把 CRLF 归一成 LF 再算：Windows 上的编辑器、git 的 autocrlf、
    以及某些 checkout 设置都会换行符，而那和「有人改了 SQL」是两件完全不同的事 ——
    前者不该让所有人的本地库突然报漂移。（仓库里 ``.gitattributes`` 也是这个取向。）
    """
    return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def load_migrations(directory: Path | None = None) -> tuple[Migration, ...]:
    """读目录里的迁移文件，按版本升序返回。**不检查数据库。**

    这里是三条规则的落地处：文件名格式、版本不重复、版本不跳号。
    全部在**读文件的阶段**报错，而不是等到应用的时候 —— 那时候报错已经晚了
    （前面几个迁移可能已经应用，库处于中间状态）。
    """
    root = directory or MIGRATIONS_DIR
    # 忽略 __init__.py 之类的非迁移文件；也忽略 002_x.sql.bak 这种（正则不匹配）
    found: list[Migration] = []
    seen: dict[int, str] = {}
    for path in sorted(root.glob("*.sql")):
        match = _FILE_RE.match(path.name)
        if match is None:
            raise MigrationError(
                f"迁移文件名不合规：{path.name}\n"
                "  约定是 NNN_小写名.sql（三位版本号 + 下划线 + 小写字母数字下划线）"
            )
        version = int(match.group(1))
        if version in seen:
            raise MigrationError(f"版本号 {version} 被两个文件用了：{seen[version]} 和 {path.name}")
        seen[version] = path.name
        # 用默认的 universal newlines 读：CRLF 在读进来的时候就变成 LF 了，
        # 所以「工作区是 CRLF」这件事根本到不了指纹那一步。
        # （``_checksum`` 里那行 replace 是第二道保险，不是主路径 ——
        # 主路径是这里。另外 read_text 的 newline 参数要到 Python 3.13 才有，
        # 容器里是 3.12，所以这里也不能靠它。）
        sql = path.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=version,
                name=match.group(2),
                path=path,
                sql=sql,
                checksum=_checksum(sql),
            )
        )

    if not found:
        raise MigrationError(f"{root} 里一个迁移文件都没有 —— 部署包大概是缺文件了")

    versions = [m.version for m in found]
    expected = list(range(1, len(found) + 1))
    if versions != expected:
        raise MigrationError(
            f"迁移版本必须从 1 连续，实际是 {versions}\n"
            "  跳号意味着有文件在合并时丢了。这件事只在**空库**上才暴露 ——\n"
            "  已经迁到 5 的库照常跑，新克隆的库少建一张表，然后在某条查询上炸掉。"
        )
    return tuple(found)


async def run_migrations(
    conn: AsyncConnection,
    migrations: tuple[Migration, ...] | None = None,
) -> list[int]:
    """把未应用的迁移全部应用。返回本次应用的版本号（已是最新则返回空列表）。

    **全程一个事务**：要么整批生效，要么一条都没有。DDL 在 Postgres 里是事务性的，
    所以「迁移到一半失败」不会留下一半的结构。

    调用方负责开事务 —— 这里自己开（``conn.transaction()``），因为
    advisory 锁、DDL、记账三者必须在同一个事务里。
    """
    pending = migrations if migrations is not None else load_migrations()

    async with conn.transaction():
        # SET LOCAL 只作用于当前事务；锁同理，事务结束自动释放
        await conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        await conn.execute(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'")
        await conn.execute(f"SELECT pg_advisory_xact_lock({LOCK_KEY})")

        await conn.execute(SCHEMA_VERSION_DDL)
        applied = await _applied_versions(conn)

        _assert_no_drift(pending, applied)

        done: list[int] = []
        for migration in pending:
            if migration.version in applied:
                continue
            await _apply(conn, migration)
            done.append(migration.version)

    if done:
        log.info(
            "migrations.applied",
            versions=done,
            total=len(pending),
            hint="本地改了表结构？加 002_*.sql，不要改已应用的文件",
        )
    return done


async def _applied_versions(conn: AsyncConnection) -> dict[int, str]:
    """已应用的 ``{版本: 指纹}``。

    **自己开 cursor 并显式声明行工厂**，不用 ``conn.execute()``：那样拿到的是
    连接默认的行工厂，而 ``PostgresPool`` 在连接上设了 ``dict_row`` ——
    同一个函数换个调用方（或哪天池子的默认值变了）就会从 ``row["version"]``
    变成 ``row[0]``，而报错是 ``KeyError: 0`` 这种指不到真正原因的形式。
    实测踩过一次：第一次跑迁移就是在 ``_applied_versions`` 上炸的。
    """
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT version, checksum FROM schema_version")
        rows: list[dict[str, Any]] = await cur.fetchall()
    return {int(row["version"]): str(row["checksum"]) for row in rows}


def _assert_no_drift(migrations: tuple[Migration, ...], applied: dict[int, str]) -> None:
    """已应用的迁移，内容必须和仓库里的一致。"""
    drifted = [m for m in migrations if m.version in applied and applied[m.version] != m.checksum]
    if drifted:
        names = "、".join(str(m) for m in drifted)
        raise MigrationDriftError(
            f"数据库里的迁移内容和仓库里的对不上：{names}\n"
            "  已应用的迁移不能再改 —— 改了之后本地库是新结构、别人的库是旧结构，\n"
            "  而两边都能跑，直到某条查询去读一列还不存在的字段。\n"
            "  正确的做法：把改动写成一个新的 002_*.sql。\n"
            "  如果你只是在本地试验：python tasks.py clean（连数据卷一起删）后重来。"
        )


async def _apply(conn: AsyncConnection, migration: Migration) -> None:
    """应用一个迁移，并在同一个事务里记账。

    整段 SQL 一次执行、不传参数 —— 见模块开头的实测记录。
    """
    log.info("migrations.applying", migration=str(migration), chars=len(migration.sql))
    await conn.execute(migration.sql)
    # 这条带参数，所以单独发（扩展协议一次只能一条语句）
    await conn.execute(
        "INSERT INTO schema_version (version, name, checksum) VALUES (%s, %s, %s)",
        [migration.version, migration.name, migration.checksum],
    )
