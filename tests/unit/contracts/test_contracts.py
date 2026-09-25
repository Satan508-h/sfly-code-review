"""领域契约的单元测试。

**这是改动 contracts.py 之后必须立刻跑的一套。** CLAUDE.md 约定的第 5 条：
契约变更永远从改 contracts.py 开始，然后跑这个文件，再改消费方。

测试的重点是**容错**而不是正确输入 —— 契约层存在的意义就是把 LLM 实际会吐的
各种脏数据挡在业务逻辑之外。断言的都是「输入是这个鬼样子，出来必须是什么」。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from pydantic import BaseModel

from sfly_shared.contracts import (
    BootstrapMessage,
    ErrorClass,
    FilePatch,
    Finding,
    ResultStatus,
    RunTotals,
    Severity,
    TaskMessage,
    WorkerResult,
    WorkerType,
    assert_idempotency_key,
    idempotency_key_for,
    normalize_path,
    stable_hash,
)
from sfly_workers.specs import CATEGORY_OWNER


def _finding(**over: object) -> Finding:
    base: dict[str, object] = {
        "file": "src/a.py",
        "line": 10,
        "severity": "high",
        "category": "sqli",
        "message": "m",
        "confidence": 0.8,
    }
    base.update(over)
    return Finding.model_validate(base)


# --------------------------------------------------------------------------- #
# 置信度强转
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (0.85, 0.85),
        (1, 1.0),
        (0, 0.0),
        # 枚举词 —— LLM 最常干的事
        ("high", 0.85),
        ("HIGH", 0.85),
        ("very low", 0.20),
        ("certain", 0.98),
        # 字符串数字
        ("0.42", 0.42),
        # 百分数
        (85, 0.85),
        ("85%", 0.85),
        # 越界必须被夹住，不能抛错（抛错会让整条 finding 丢掉）
        (1.7, 1.0),
        (-3, 0.0),
        # 完全无法解析时给中性默认值，而不是丢弃这条发现
        (None, 0.5),
        ("莫名其妙", 0.5),
    ],
)
def test_confidence_coercion(raw: object, expected: float) -> None:
    assert _finding(confidence=raw).confidence == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# 行号强转
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (42, 42),
        ("42", 42),
        ("L42", 42),
        ("l42", 42),
        ("42-45", 42),  # 区间取起始行
        ("db.py:42", 42),  # 带文件名
        ("  42  ", 42),  # str_strip_whitespace 之外还要自己 strip
    ],
)
def test_line_coercion(raw: object, expected: int) -> None:
    assert _finding(line=raw).line == expected


@pytest.mark.unit
def test_line_unparseable_raises() -> None:
    # 行号无法解析时**必须抛错**，不能静默变成 0 或 1 ——
    # 那样会发出一条锚在错误位置的 inline 评论，比不发更糟
    with pytest.raises(ValueError):
        _finding(line="不知道")


# --------------------------------------------------------------------------- #
# 枚举与别名
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CRITICAL", Severity.CRITICAL),
        ("Critical", Severity.CRITICAL),
        ("error", Severity.HIGH),  # LLM 常把 error/warning 当严重度
        ("warning", Severity.MEDIUM),
        ("warn", Severity.MEDIUM),
        ("note", Severity.INFO),
    ],
)
def test_severity_aliases(raw: str, expected: Severity) -> None:
    assert _finding(severity=raw).severity is expected


@pytest.mark.unit
def test_end_line_before_start_is_clamped() -> None:
    f = _finding(line=50, end_line=10)
    assert f.end_line == 50


@pytest.mark.unit
def test_blank_strings_become_none() -> None:
    # LLM 经常把「没有」写成空串而非 null。空串会让下游的 `is not None`
    # 判断全部走错分支，所以必须在契约层收敛。
    f = _finding(evidence="   ", suggestion="", rule_id="")
    assert f.evidence is None
    assert f.suggestion is None
    assert f.rule_id is None


# --------------------------------------------------------------------------- #
# 类目别名 —— 这是最重要的一个不变量
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "canonical"),
    [
        ("SQL Injection", "sqli"),
        ("sql-injection", "sqli"),
        ("Cross-Site Scripting", "xss"),
        ("hardcoded credentials", "secrets"),
        ("Authorization", "auth"),
        ("directory traversal", "path_traversal"),
        ("O(n^2)", "quadratic"),
        ("N+1", "n_plus_one"),
        ("query in loop", "n_plus_one"),
        ("synchronous IO", "blocking_io"),
        ("unused variable", "dead_code"),
        ("missing docs", "docs"),
        ("readability", "complexity_readability"),
    ],
)
def test_category_alias_normalization(raw: str, canonical: str) -> None:
    assert _finding(category=raw).category == canonical


@pytest.mark.unit
def test_every_canonical_category_has_an_owner() -> None:
    """契约层归一后的每个类目名，都必须能被某个 Worker 认领。

    这条断言保护的是一个**静默失效**：归一化产出的名字如果不在
    ``CATEGORY_OWNER`` 里，冲突消解的 ``category_authority`` 规则会查不到归属，
    于是安全 Worker 报的 CRITICAL 会被别的 Worker 的 LOW 拉平 ——
    而日志里没有任何异常。跑到线上才会发现「安全发现被降级了」。
    """
    from sfly_shared.contracts import _CATEGORY_ALIASES

    orphans = {k: v for k, v in _CATEGORY_ALIASES.items() if v not in CATEGORY_OWNER}
    assert not orphans, f"别名表指向不存在的类目: {orphans}"


@pytest.mark.unit
def test_category_owner_has_no_duplicates_across_workers() -> None:
    """同一个类目不能同时属于两个 Worker，否则职责域规则没有确定答案。"""
    seen: dict[str, WorkerType] = {}
    for wt, spec in __import__("sfly_workers.specs", fromlist=["SPECS"]).SPECS.items():
        for cat in spec.categories:
            assert cat not in seen, f"类目 {cat!r} 同时属于 {seen[cat]} 和 {wt}"
            seen[cat] = wt


# --------------------------------------------------------------------------- #
# 幂等键
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_idempotency_key_format() -> None:
    assert idempotency_key_for("owner/repo", 7, "abc123") == "owner/repo:7:abc123"


@pytest.mark.unit
def test_assert_idempotency_key_rejects_mismatch() -> None:
    assert_idempotency_key("o/r", 1, "sha", "o/r:1:sha")
    with pytest.raises(ValueError, match="不一致"):
        assert_idempotency_key("o/r", 1, "sha", "o/r:2:sha")


@pytest.mark.unit
def test_task_message_rejects_wrong_key() -> None:
    with pytest.raises(ValueError, match="不一致"):
        TaskMessage(
            task_id="t",
            worker_type="security",
            idempotency_key="错的",
            repo_id="o/r",
            repo_node_id="1",
            pr_number=1,
            head_sha="sha",
            base_sha="base",
            file_patches=[],
        )


@pytest.mark.unit
def test_bootstrap_message_rejects_wrong_key() -> None:
    with pytest.raises(ValueError, match="不一致"):
        BootstrapMessage(
            task_id="t",
            idempotency_key="错的",
            repo_id="o/r",
            repo_node_id="1",
            pr_number=1,
            head_sha="sha",
            base_sha="base",
        )


# --------------------------------------------------------------------------- #
# WorkerResult 状态归一
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_failed_factory_closes_the_barrier() -> None:
    """失败也是结果 —— 这个工厂方法产出的是能让屏障闭合的对象。"""
    r = WorkerResult.failed("t1", WorkerType.SECURITY, "LLM 超时", ErrorClass.LLM_TIMEOUT, attempt=3)
    assert r.status is ResultStatus.FAILED
    assert r.error_class is ErrorClass.LLM_TIMEOUT
    assert r.attempt == 3
    assert r.findings == []


@pytest.mark.unit
def test_failed_error_is_truncated() -> None:
    # 一个巨大的 traceback 塞进 Postgres 的 TEXT 会拖慢查询，
    # 而诊断只需要开头那一段
    r = WorkerResult.failed("t1", WorkerType.SECURITY, "x" * 10_000)
    assert r.error is not None
    assert len(r.error) == 2000


@pytest.mark.unit
def test_findings_with_failed_status_is_normalized_to_partial() -> None:
    """有产出却报 failed 会误导前端显示成「完全失败」，抹掉已经拿到的结果。"""
    r = WorkerResult(
        task_id="t1",
        worker_type="security",
        status=ResultStatus.FAILED,
        findings=[_finding()],
    )
    assert r.status is ResultStatus.PARTIAL


@pytest.mark.unit
def test_error_with_ok_status_is_normalized_to_partial() -> None:
    r = WorkerResult(task_id="t1", worker_type="security", status=ResultStatus.OK, error="部分失败")
    assert r.status is ResultStatus.PARTIAL


@pytest.mark.unit
def test_clean_result_stays_ok() -> None:
    r = WorkerResult(task_id="t1", worker_type="security", findings=[_finding()])
    assert r.status is ResultStatus.OK


# --------------------------------------------------------------------------- #
# Streams 平铺编解码
# --------------------------------------------------------------------------- #


def _bootstrap() -> BootstrapMessage:
    return BootstrapMessage(
        task_id="t1",
        idempotency_key=idempotency_key_for("o/r", 3, "sha"),
        repo_id="o/r",
        repo_node_id="1",
        pr_number=3,
        head_sha="sha",
        base_sha="base",
        file_patches=[FilePatch(path="app/db.py", patch="@@ -1 +1 @@\n-x\n+y\n", changed_lines=[1])],
    )


def _task_message() -> TaskMessage:
    return TaskMessage(
        task_id="t1",
        worker_type="performance",
        idempotency_key=idempotency_key_for("o/r", 3, "sha"),
        repo_id="o/r",
        repo_node_id="1",
        pr_number=3,
        head_sha="sha",
        base_sha="base",
        file_patches=[],
        language="python",
    )


def _worker_result() -> WorkerResult:
    return WorkerResult(
        task_id="t1",
        worker_type="security",
        findings=[_finding()],
        tokens_in=1234,
        tokens_out=567,
        cached_tokens=1000,
    )


#: 三条流上流转的全部消息类型。**每加一种跨进程消息，就加到这里。**
_STREAM_MESSAGES = [
    pytest.param(BootstrapMessage, _bootstrap, id="bootstrap"),
    pytest.param(TaskMessage, _task_message, id="task"),
    pytest.param(WorkerResult, _worker_result, id="result"),
]


@pytest.mark.unit
@pytest.mark.parametrize(("cls", "build"), _STREAM_MESSAGES)
def test_stream_fields_are_all_strings(cls: type[BaseModel], build: Callable[[], BaseModel]) -> None:
    """Redis Streams 只能存字符串。只要有非 str 值，XADD 就会失败。"""
    fields = build().to_stream_fields()  # type: ignore[attr-defined]
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in fields.items())


@pytest.mark.unit
@pytest.mark.parametrize(("cls", "build"), _STREAM_MESSAGES)
def test_every_cross_process_message_round_trips(
    cls: type[BaseModel], build: Callable[[], BaseModel]
) -> None:
    """每种跨进程消息都必须能原样往返。

    参数化而不是一条一条写：``BootstrapMessage`` 曾经是唯一漏掉平铺编解码的
    那个类型 —— 因为没有任何地方要求它有，也就没有任何地方会报错，
    直到有人真的要往 ``review_bootstrap`` 上写消息。
    """
    msg = build()
    assert cls.from_stream_fields(msg.to_stream_fields()) == msg  # type: ignore[attr-defined]


@pytest.mark.unit
def test_stream_fields_expose_indexable_columns() -> None:
    """payload 之外还要平铺几个字段，否则消费者必须反序列化整包才能筛选。

    这一条对 worker 侧尤其要紧：分发到 ``review_tasks`` 的消息里混着三种
    ``worker_type``，消费者得靠平铺的 ``worker_type`` 把它们挑出来 ——
    没有它就只能先解析（而解析正是「坏 payload 进死信」那条路径要避免的事）。
    """
    assert {"payload", "task_id", "worker_type", "status", "attempt"} <= set(
        _worker_result().to_stream_fields()
    )
    assert {"payload", "task_id", "worker_type", "attempt"} <= set(_task_message().to_stream_fields())
    assert {"payload", "task_id", "repo_id", "pr_number"} <= set(_bootstrap().to_stream_fields())


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("src/a.py", "src/a.py"),
        ("./src/a.py", "src/a.py"),
        ("a/src/a.py", "src/a.py"),  # diff 的 a/ 前缀
        ("b/src/a.py", "src/a.py"),  # diff 的 b/ 前缀
        ("src\\a.py", "src/a.py"),  # Windows 反斜杠
        ("Src/A.PY", "src/a.py"),  # 大小写
        ("  src/a.py  ", "src/a.py"),
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert normalize_path(raw) == expected


@pytest.mark.unit
def test_stable_hash_is_deterministic_across_processes() -> None:
    """必须用 sha1 这类稳定哈希，不能用内置 hash()。

    ``hash()`` 对字符串加了随机盐，每个进程结果都不同 —— 指纹在 Worker 和
    orchestrator 之间会对不上，去重静默失效。
    """
    assert stable_hash("a", "b") == stable_hash("a", "b")
    assert stable_hash("a", "b") != stable_hash("b", "a")


@pytest.mark.unit
def test_stable_hash_separator_prevents_collision() -> None:
    # 没有分隔符的话 "ab"+"c" 和 "a"+"bc" 会碰撞
    assert stable_hash("ab", "c") != stable_hash("a", "bc")


@pytest.mark.unit
def test_contracts_reject_unknown_fields() -> None:
    """extra=forbid 是刻意的：让字段名拼写错误在写入时就炸，
    而不是变成一条静默丢失的信息。"""
    with pytest.raises(ValueError):
        Finding.model_validate(
            {
                "file": "a.py",
                "line": 1,
                "severity": "low",
                "category": "docs",
                "message": "m",
                "confidence": 0.5,
                "sevrity": "critical",  # 拼错了
            }
        )


@pytest.mark.unit
def test_cache_hit_rate() -> None:
    t = RunTotals(tokens_in=10_000, cached_tokens=6_000)
    assert t.cache_hit_rate == pytest.approx(0.6)
    assert RunTotals().cache_hit_rate == 0.0  # 不能除零
