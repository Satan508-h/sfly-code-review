"""迁移器的纯逻辑：文件名、版本、指纹，以及 **SQL 里的约束和契约枚举是否一致**。

全部不需要数据库（连不上也能跑，这是单测层的分层约定）。真实的建表、并发、
漂移检测在 ``tests/integration/bus/test_migrations.py`` 里对着真库跑。

这个文件里最有价值的是最后两条：它们在 **SQL 和 contracts.py 之间**架了一道检查。
``CHECK (status IN (...))`` 是那套枚举在数据库里的第二份拷贝，而第二份拷贝
一定会漂移 —— 除非有东西盯着它。约定 #5 说「契约变更先改 contracts.py」，
这两条测试让那句话在数据库这一层也有牙齿。
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

import pytest

from sfly_bus.migrations import (
    MIGRATIONS_DIR,
    MigrationDriftError,
    MigrationError,
    load_migrations,
)
from sfly_bus.postgres import migrate_on_startup
from sfly_shared.contracts import (
    ErrorClass,
    ResultStatus,
    RunStatus,
    Severity,
    WorkerType,
)

pytestmark = pytest.mark.unit

#: 建表用 ``DROP TABLE `` 之外的另一半：这条 SQL 里**不该**出现记账表的定义。
SCHEMA_VERSION_TABLE = "schema_version"


def _write(directory: Path, name: str, sql: str = "SELECT 1;") -> None:
    """``newline=""`` 是关键：默认的写模式会把 ``\\n`` 翻译成 ``os.linesep``，
    于是在 Windows 上「写一个 LF 文件」和「写一个 CRLF 文件」会得到
    ``\\n`` 与 ``\\r\\r\\n``（后者被翻译了两次）—— 而那条换行符测试正是要
    区分这两者，写的时候一翻译它就测不出任何东西了。
    """
    (directory / name).write_text(sql, encoding="utf-8", newline="")


# --------------------------------------------------------------------------- #
# 读取与校验
# --------------------------------------------------------------------------- #


def test_the_shipped_migrations_load_in_order() -> None:
    migrations = load_migrations()

    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "init"
    assert len(migrations[0].sql) > 500, "迁移文件不该是个空壳"
    assert re.fullmatch(r"[0-9a-f]{64}", migrations[0].checksum)


def test_a_bad_filename_is_refused(tmp_path: Path) -> None:
    """``1_init.sql`` 这种「少写一位」的文件如果被接受，
    版本顺序就会静默地按字符串排序而不是数字 —— 002 会排在 1 前面。"""
    _write(tmp_path, "1_init.sql")

    with pytest.raises(MigrationError, match="文件名不合规"):
        load_migrations(tmp_path)


def test_two_files_with_the_same_version_are_refused(tmp_path: Path) -> None:
    """合并两个分支时最容易出现的情况：两边各加了一个 002。"""
    _write(tmp_path, "001_init.sql")
    _write(tmp_path, "002_a.sql")
    _write(tmp_path, "002_b.sql")

    with pytest.raises(MigrationError, match="版本号 2 被两个文件用了"):
        load_migrations(tmp_path)


def test_a_gap_in_versions_is_refused(tmp_path: Path) -> None:
    """**跳号意味着有文件在合并时丢了。**

    这件事的可怕之处在于它只在**空库**上才暴露：已经迁到 3 的库照常跑，
    新克隆下来的库少建一张表，然后在某条查询上炸掉 —— 而那时报的是
    ``relation does not exist``，和「少了一个文件」隔着好几层。
    """
    _write(tmp_path, "001_init.sql")
    _write(tmp_path, "003_later.sql")

    with pytest.raises(MigrationError, match="必须从 1 连续"):
        load_migrations(tmp_path)


def test_an_empty_migrations_directory_is_refused(tmp_path: Path) -> None:
    """没有迁移文件 = 部署包里没带上那个目录。报错比建不出表之后再报错早得多。"""
    with pytest.raises(MigrationError, match="一个迁移文件都没有"):
        load_migrations(tmp_path)


def test_checksum_ignores_line_endings_but_not_content(tmp_path: Path) -> None:
    """指纹看的是内容，不是换行符。

    仓库里一直是 LF（``.gitattributes``），但工作区可能因为编辑器、
    ``autocrlf`` 或者某些 checkout 设置变成 CRLF —— 那和「有人改了 SQL」
    是两件完全不同的事，前者不该让所有人的本地库突然报漂移。
    """
    _write(tmp_path, "001_init.sql", "SELECT 1;\nSELECT 2;\n")
    unix = load_migrations(tmp_path)[0].checksum

    _write(tmp_path, "001_init.sql", "SELECT 1;\r\nSELECT 2;\r\n")
    windows = load_migrations(tmp_path)[0].checksum

    assert unix == windows

    _write(tmp_path, "001_init.sql", "SELECT 1;\r\nSELECT 3;\r\n")
    assert load_migrations(tmp_path)[0].checksum != unix, "内容变了指纹必须跟着变"


# --------------------------------------------------------------------------- #
# SQL 与契约的一致性
# --------------------------------------------------------------------------- #

_TABLE_RE = re.compile(r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)", re.IGNORECASE)
_CHECK_RE = re.compile(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL)
_VALUE_RE = re.compile(r"'([^']*)'")


def _check_constraints(sql: str) -> dict[tuple[str, str], set[str]]:
    """``{(表, 列): {允许取值}}`` —— 把 SQL 里的 CHECK 约束读出来。

    用正则而不是 SQL 解析器：这些约束全是同一个形状（我们写 SQL 时统一了写法），
    而一个真正的解析器会带来一个需要维护的依赖。形状变了的话，
    ``test_every_check_constraint_in_the_sql_is_covered`` 会因为「找到的约束变少」
    而报错，不会静默漏检。
    """
    found: dict[tuple[str, str], set[str]] = {}
    parts = _TABLE_RE.split(sql)
    # split 带一个捕获组 → [前置, 表名1, 区块1, 表名2, 区块2, ...]
    for table, block in zip(parts[1::2], parts[2::2], strict=True):
        for column, values in _CHECK_RE.findall(block):
            found[(table.lower(), column.lower())] = set(_VALUE_RE.findall(values))
    return found


#: SQL 里的 CHECK 约束 ↔ contracts.py 里的枚举。**唯一的真相来源是右边。**
_CHECKED_ENUMS: dict[tuple[str, str], type[StrEnum]] = {
    ("review_runs", "status"): RunStatus,
    ("worker_results", "status"): ResultStatus,
    ("worker_results", "worker_type"): WorkerType,
    ("worker_results", "error_class"): ErrorClass,
    ("findings", "worker_type"): WorkerType,
    ("findings", "severity"): Severity,
}


def _enum_values(enum: type[StrEnum]) -> set[str]:
    return {member.value for member in enum}


def test_every_check_constraint_in_the_sql_is_covered() -> None:
    """SQL 里每一条枚举约束都必须在这张表里被对照过。

    没有这条的话，新加一个 ``CHECK (severity IN (...))`` 却忘了登记，
    就等于加了一个**没有任何东西在看着的第二份拷贝** —— 它会在某次契约变更
    之后安静地拒绝一条本该合法的写入（而错误信息是
    ``violates check constraint``，不会告诉你是哪个枚举漏了）。
    """
    found = set(_check_constraints((MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")))

    assert found == set(_CHECKED_ENUMS), (
        "SQL 里的 CHECK 约束和 _CHECKED_ENUMS 对不上。\n"
        f"  SQL 里有：{sorted(found)}\n"
        f"  登记了：{sorted(_CHECKED_ENUMS)}\n"
        "  新增约束要在 _CHECKED_ENUMS 里登记，删掉的要从那里去掉。"
    )


@pytest.mark.parametrize(("table", "column", "enum"), [(t, c, e) for (t, c), e in _CHECKED_ENUMS.items()])
def test_a_check_constraint_matches_its_enum(table: str, column: str, enum: type[StrEnum]) -> None:
    """约束里的字面量集合必须**逐个**等于枚举。

    两个方向都会出错：

    * 少了一个值（契约加了 ``WorkerType.SECURITY_AUDIT``，SQL 没加）→
      新 Worker 的结果**写不进库**，而报错是一句 check constraint violation
    * 多了一个值（SQL 里留着已删掉的状态）→ 库接受一个代码不认识的状态，
      而 ``RunStatus(...)`` 在**读**的时候才会炸，离写入点很远
    """
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
    constraints = _check_constraints(sql)

    assert (table, column) in constraints
    assert constraints[(table, column)] == _enum_values(enum)


def test_the_migration_sql_does_not_create_the_bookkeeping_table() -> None:
    """``schema_version`` 由迁移器自己建（见 ``migrations/__init__.py`` 的 ``SCHEMA_VERSION_DDL``）。

    它要是也出现在迁移文件里，这个表就有两处定义了 —— 而两处定义不会打架
    （都用 ``IF NOT EXISTS``），只会慢慢分叉：列多一个少一个、类型不同，
    直到某天有人重建库才发现。
    """
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")

    assert re.search(rf"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?{SCHEMA_VERSION_TABLE}", sql, re.I) is None


# --------------------------------------------------------------------------- #
# 启动时的处置：漂移要炸，连不上不能炸
# --------------------------------------------------------------------------- #


class _FailingStore:
    """只实现 ``migrate()`` 的桩 —— ``migrate_on_startup`` 也只调这一个方法。"""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.calls = 0

    async def migrate(self) -> None:
        self.calls += 1
        raise self._exc


async def test_startup_tolerates_an_unreachable_database() -> None:
    """连不上数据库时**不抛** —— M0 定下的规矩：进程要能起来，
    然后在 ``/api/health`` 里说清楚哪里坏了。

    崩掉退出会让 Docker 的 ``restart: unless-stopped`` 把容器拖进无限重启循环，
    而那会把真正的错误信息冲掉，只剩下一屏「容器在重启」。

    用桩而不是真的去连 ``127.0.0.1:1``：这里要验的是**处置策略**
    （哪一类失败吞掉、哪一类往上抛），而真的连一次只会让这条测试慢两秒，
    顺带把 psycopg 的连接行为也拖进来当被测对象。
    """
    store = _FailingStore(OSError("connection refused"))

    await migrate_on_startup(store)  # type: ignore[arg-type]

    assert store.calls == 1


async def test_startup_does_not_tolerate_drift() -> None:
    """漂移必须往上抛：代码期待的结构和数据库里的不是一回事。

    继续跑就是往错的结构上写数据，而且不会有任何报错 —— 这类错误
    「安静地写坏数据」的性质，决定了它必须是启动失败而不是一条警告。
    """
    store = _FailingStore(MigrationDriftError("001_init 的内容变了"))

    with pytest.raises(MigrationDriftError):
        await migrate_on_startup(store)  # type: ignore[arg-type]
