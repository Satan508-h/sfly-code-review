"""WorkerRunner 与 ``reconcile_findings`` 的测试。

``reconcile_findings`` 是 Worker 里第二值得写测试的地方（第一是修复阶梯）：
它把模型的输出对齐到**真实的输入**上，而模型输出有三类不可信 ——
文件路径、行号、规则 id。每一类都有一条「静默做错」的路径：

* 路径对不上还照样发布 → M7 请求 GitHub 时 404，而那时已经离现场很远了
* 行号没校验就发布 → GitHub 422 拒绝 inline 评论，整条评论发不出去
* 编造的 rule_id 拿到 grounded 加成 → 置信度公式整体失真
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sfly_agent.diff import parse_unified_diff
from sfly_agent.llm.base import LLMProvider, LLMResponse
from sfly_shared.config import Settings
from sfly_shared.contracts import (
    ErrorClass,
    FilePatch,
    Finding,
    ResultStatus,
    Rule,
    Severity,
    WorkerType,
)
from sfly_shared.errors import LlmTimeoutError
from sfly_workers.runner import WorkerRunner, reconcile_findings
from sfly_workers.specs import spec_for

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _patch(path: str, changed: list[int]) -> FilePatch:
    return FilePatch(path=path, language="python", patch="x", changed_lines=changed)


def _finding(file: str, line: int, **over: object) -> Finding:
    base: dict[str, object] = {
        "file": file,
        "line": line,
        "severity": "high",
        "category": "sqli",
        "message": "m",
        "confidence": 0.8,
    }
    base.update(over)
    return Finding.model_validate(base)


def _rule(rule_id: str) -> Rule:
    return Rule(id=rule_id, title="t", worker_type=WorkerType.SECURITY, category="sqli", body="b")


# --------------------------------------------------------------------------- #
# 文件路径对齐
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_path_is_rewritten_to_the_repository_path() -> None:
    """模型爱写 ``b/app/db.py`` 或 ``./app/db.py``，都要被归一到真实路径。

    归一化不只是为了匹配 —— 那个字符串会一路进数据库、进 API、进前端，
    最后被拿去请求 GitHub。留在里面的 ``b/`` 前缀会在**很远的地方**炸。
    """
    report = reconcile_findings(
        [_finding("b/app/db.py", 10), _finding("./app/db.py", 11)],
        [_patch("app/db.py", [10, 11])],
        [],
    )
    assert [f.file for f in report.kept] == ["app/db.py", "app/db.py"]
    assert report.dropped == 0


@pytest.mark.unit
def test_unique_basename_is_accepted() -> None:
    """模型常把 ``src/a.py`` 写成 ``a.py``。同名文件唯一时接受。"""
    report = reconcile_findings([_finding("db.py", 3)], [_patch("app/db.py", [3])], [])
    assert [f.file for f in report.kept] == ["app/db.py"]


@pytest.mark.unit
def test_ambiguous_basename_is_dropped_rather_than_guessed() -> None:
    """**同名文件不唯一时必须丢弃。**

    猜错的后果不是「少一条意见」而是「一条挂在错误文件上的意见」——
    后者更糟：作者会照着它去改一个没有问题的文件。
    """
    report = reconcile_findings(
        [_finding("models.py", 3)],
        [_patch("app/models.py", [3]), _patch("tests/models.py", [3])],
        [],
    )
    assert report.kept == []
    assert report.file_mismatch == ["models.py"]


@pytest.mark.unit
def test_hallucinated_path_is_dropped_with_a_reason() -> None:
    """幻觉路径是结构错误里最常见的一种，而且**无法发布** ——
    GitHub 上没有这个文件，评论无处可挂。"""
    report = reconcile_findings([_finding("src/does_not_exist.py", 1)], [_patch("a.py", [1])], [])
    assert report.kept == []
    assert report.dropped == 1
    assert "does_not_exist" in report.file_mismatch[0]


# --------------------------------------------------------------------------- #
# 行号与规则 id
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_line_on_a_changed_line_is_verified() -> None:
    report = reconcile_findings([_finding("a.py", 7)], [_patch("a.py", [7, 8])], [])
    assert report.kept[0].source_line_verified is True
    assert report.unverified_lines == 0


@pytest.mark.unit
def test_line_outside_the_diff_is_kept_but_flagged() -> None:
    """**不在变更行上不是错误，但必须记下来。**

    模型有时会指向上下文行来讲清楚问题所在。丢掉它等于丢掉一条正确意见；
    但也不能当成已验证 —— 发布阶段要把这类降级成文件级评论，
    因为 GitHub 会 422 拒绝锚定在未变更行上的 inline 评论。
    """
    report = reconcile_findings([_finding("a.py", 999)], [_patch("a.py", [7])], [])
    assert len(report.kept) == 1
    assert report.kept[0].source_line_verified is False
    assert report.unverified_lines == 1


@pytest.mark.unit
def test_hallucinated_rule_id_is_nulled_so_grounding_cannot_be_faked() -> None:
    """编造的 ``rule_id`` 必须被清掉。

    ``grounded`` 是置信度公式里唯一一个「有客观依据」的加项（+0.10）。
    让编造的 id 拿到它，等于把整条置信度公式的可信度清零 ——
    而这件事完全静默。
    """
    report = reconcile_findings(
        [_finding("a.py", 1, rule_id="sec-编的-999"), _finding("a.py", 2, rule_id="sec-sqli-001")],
        [_patch("a.py", [1, 2])],
        [_rule("sec-sqli-001")],
    )
    assert report.kept[0].rule_id is None
    assert report.kept[1].rule_id == "sec-sqli-001"
    assert report.hallucinated_rules == 1


@pytest.mark.unit
def test_rule_id_is_cleared_when_no_rules_were_provided() -> None:
    """没送规则时，任何 rule_id 都是编的。"""
    report = reconcile_findings([_finding("a.py", 1, rule_id="sec-sqli-001")], [_patch("a.py", [1])], [])
    assert report.kept[0].rule_id is None


# --------------------------------------------------------------------------- #
# Runner 端到端（Mock LLM）
# --------------------------------------------------------------------------- #


class _BrokenLLM:
    """永远返回无法解析的文本 —— 模拟模型彻底抽风。"""

    name = "broken"
    model = "broken-1"

    async def complete(
        self, *, system: str, user: str, max_tokens: int | None = None, temperature: float | None = None
    ) -> LLMResponse:
        return LLMResponse(text="我觉得这段代码没什么问题。", model=self.model)


class _ExplodingLLM:
    """传输层故障 —— 这类**必须原样抛出**，不能变成一条失败结果。"""

    name = "exploding"
    model = "exploding-1"

    async def complete(
        self, *, system: str, user: str, max_tokens: int | None = None, temperature: float | None = None
    ) -> LLMResponse:
        raise LlmTimeoutError("上游超时")


def _settings() -> Settings:
    return Settings(llm_provider="mock", llm_max_repairs=0)


def _demo_patches() -> list[FilePatch]:
    return parse_unified_diff((FIXTURES / "security_demo.diff").read_text(encoding="utf-8")).patches


@pytest.mark.unit
async def test_review_returns_verified_findings() -> None:
    from sfly_agent.llm.mock import MockLLM

    spec = spec_for("security")
    runner = WorkerRunner(spec, MockLLM(worker_types=(WorkerType.SECURITY,)), _settings())
    result = await runner.review(task_id="t1", patches=_demo_patches(), rules=[])

    assert result.status is ResultStatus.OK
    assert result.worker_type is WorkerType.SECURITY
    assert result.task_id == "t1"
    assert len(result.findings) > 5
    assert all(f.source_line_verified for f in result.findings)
    assert result.tokens_in > 0
    assert result.model == "mock-1"
    assert result.dropped_findings == 0


@pytest.mark.unit
async def test_unparseable_response_becomes_a_failed_result_not_an_exception() -> None:
    """**失败也是结果**（CLAUDE.md 约定 #2）。

    解析失败如果直接抛异常而不返回结果，``wait`` 节点的屏障永远不会闭合，
    整个 run 会挂到超时。所以它必须变成一条 ``status="failed"`` 的结果 ——
    而且带着 ``raw_response``，否则没有改进 prompt 的依据。
    """
    spec = spec_for("security")
    runner = WorkerRunner(spec, _BrokenLLM(), _settings())
    result = await runner.review(task_id="t2", patches=_demo_patches(), rules=[])

    assert result.status is ResultStatus.FAILED
    assert result.error_class is ErrorClass.SCHEMA_UNRECOVERABLE
    assert result.findings == []
    assert result.raw_response == "我觉得这段代码没什么问题。"
    assert result.error


@pytest.mark.unit
async def test_transport_failure_propagates() -> None:
    """传输层故障要抛出去，由消费循环决定重试 —— 那是它才知道的事
    （attempt 计数、死信规则）。在这里吞掉会让重试逻辑永远接不到这个信号。
    """
    spec = spec_for("security")
    runner = WorkerRunner(spec, _ExplodingLLM(), _settings())
    with pytest.raises(LlmTimeoutError):
        await runner.review(task_id="t3", patches=_demo_patches(), rules=[])


@pytest.mark.unit
async def test_dropped_items_make_the_result_partial() -> None:
    """``partial`` 是一等公民：它表示「部分 finding 通过了校验」。

    混进 ``ok`` 会让评测把「部分对」算成「全对」；
    混进 ``failed`` 会让前端显示降级徽章而丢掉已经拿到的结果。
    """

    class _HalfBadLLM:
        name = "halfbad"
        model = "halfbad-1"

        async def complete(
            self,
            *,
            system: str,
            user: str,
            max_tokens: int | None = None,
            temperature: float | None = None,
        ) -> LLMResponse:
            return LLMResponse(
                text=(
                    '{"findings": ['
                    '{"file": "app/db.py", "line": 8, "severity": "high", "category": "secrets", "message": "m"},'
                    '{"file": "app/db.py", "line": "不是数字", "severity": "high", "category": "sqli", "message": "坏"}'
                    "]}"
                ),
                model=self.model,
                finish_reason="stop",
            )

    spec = spec_for("security")
    runner = WorkerRunner(spec, _HalfBadLLM(), _settings())
    result = await runner.review(task_id="t4", patches=_demo_patches(), rules=[])

    assert result.status is ResultStatus.PARTIAL
    assert len(result.findings) == 1
    assert result.dropped_findings == 1


@pytest.mark.unit
async def test_empty_findings_is_ok_not_partial() -> None:
    """「模型说没问题」是一次成功的审查，不是降级。"""
    from sfly_agent.llm.mock import MockLLM

    spec = spec_for("security")
    runner = WorkerRunner(spec, MockLLM(worker_types=(WorkerType.SECURITY,)), _settings())
    clean = parse_unified_diff((FIXTURES / "clean.diff").read_text(encoding="utf-8")).patches
    result = await runner.review(task_id="t5", patches=clean, rules=[])

    assert result.status is ResultStatus.OK
    assert result.findings == []
    assert result.dropped_findings == 0


@pytest.mark.unit
async def test_works_with_zero_patches_without_touching_the_network() -> None:
    """空输入不该崩，也不该去连任何东西 —— 它是会被真实触发的边界。"""
    from sfly_agent.llm.mock import MockLLM

    spec = spec_for("security")
    runner = WorkerRunner(spec, MockLLM(worker_types=(WorkerType.SECURITY,)), _settings())
    result = await runner.review(task_id="t6", patches=[], rules=[])
    assert result.findings == []


@pytest.mark.unit
def test_llm_protocol_is_structurally_satisfied() -> None:
    """三种 provider 都必须满足同一个 Protocol —— 这是「换 provider 不改业务代码」
    在类型层面的保证（运行时靠 Protocol 的 runtime_checkable 兜底）。"""
    from sfly_agent.llm.mock import MockLLM

    assert isinstance(MockLLM(), LLMProvider)
    assert isinstance(_BrokenLLM(), LLMProvider)


@pytest.mark.unit
def test_severity_ordering_helper_is_available_for_later_stages() -> None:
    """严重度排序在聚合阶段要用；这里只是钉住它在契约层的定义。"""
    assert Severity.CRITICAL.value == "critical"
