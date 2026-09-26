"""冲突消解 —— 纯函数、无 IO、无 LLM、毫秒级。

这个文件里最重要的两条测试不是「规则对不对」，而是**顺序**：

* 冲突必须在**聚类之前**裁（否则聚类把严重度分歧抹平，一条也发现不了）
* 败方必须在**聚类之前**移除（否则一条被裁决掉的声称会变成「互相支持」）

两条都错得很安静 —— 前者让冲突面板永远是空的，后者让 ``corroboration_count``
虚高，两个都不会有任何报错。
"""

from __future__ import annotations

import pytest

from factories import finding, result, run_row
from sfly_agent.aggregate.confidence import SUPPRESS_THRESHOLD
from sfly_agent.aggregate.conflicts import (
    CONFLICT_SEVERITY_GAP,
    resolve_conflicts,
)
from sfly_agent.aggregate.pipeline import aggregate_run, merge_findings
from sfly_shared.contracts import (
    CATEGORY_OWNER,
    ConflictRecord,
    Severity,
    WorkerResult,
    WorkerType,
)

pytestmark = pytest.mark.unit

SEC = WorkerType.SECURITY
PERF = WorkerType.PERFORMANCE
STY = WorkerType.STYLE


def _results(*pairs: tuple[WorkerType, object]) -> list[WorkerResult]:
    """把 ``(worker, finding)`` 收成每个 Worker 一条上报。"""
    grouped: dict[WorkerType, list[object]] = {}
    for worker, item in pairs:
        grouped.setdefault(worker, []).append(item)
    return [
        result("t1", worker_type=worker, findings=findings)  # type: ignore[arg-type]
        for worker, findings in grouped.items()
    ]


def _outcome(*pairs: tuple[WorkerType, object]):  # type: ignore[no-untyped-def]
    return resolve_conflicts(_results(*pairs))


# --------------------------------------------------------------------------- #
# 判据
# --------------------------------------------------------------------------- #


def test_same_category_with_a_two_level_gap_is_a_conflict() -> None:
    """同类目 + 差 2 级 + 不同 Worker + 相邻行 → 一次冲突。"""
    outcome = _outcome(
        (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=12)),
        (PERF, finding(severity=Severity.MEDIUM, category="sqli", line=13)),
    )
    assert len(outcome.records) == 1
    assert outcome.records[0].resolution_rule == "category_authority"


def test_a_one_level_gap_is_not_a_conflict() -> None:
    """差 1 级是正常的判断抖动，不值得裁决。

    把 1 级差也算冲突，会把大量「一个说 high 一个说 critical」的正常分歧
    卷进冲突面板，而面板就不再是「需要人看一眼的东西」了。
    """
    outcome = _outcome(
        (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=12)),
        (PERF, finding(severity=Severity.HIGH, category="sqli", line=12)),
    )
    assert outcome.records == []


def test_a_different_category_is_never_a_conflict() -> None:
    """**同一位置的不同类目不是矛盾，是两件事。**

    安全说这里有 SQL 注入（CRITICAL），风格说这里的变量名不规范（LOW）——
    两条都成立。按「职责域优先」把它们算成冲突，结果是把风格 Worker 那条
    **完全正确的发现**丢掉，只因为它旁边有个更严重的问题。
    这条测试钉的就是这个丢发现的坑。
    """
    outcome = _outcome(
        (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=42)),
        (STY, finding(severity=Severity.LOW, category="naming", line=42)),
    )
    assert outcome.records == []
    # 两条都还在
    assert sum(len(r.findings) for r in outcome.results) == 2


def test_the_same_worker_disagreeing_with_itself_is_not_a_conflict() -> None:
    """同一个 Worker 报两条严重度不同的 —— 那是它自己的判断，不是冲突。"""
    outcome = _outcome(
        (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=12)),
        (SEC, finding(severity=Severity.LOW, category="sqli", line=12)),
    )
    assert outcome.records == []


def test_line_drift_beyond_the_tolerance_is_not_a_conflict() -> None:
    """差 4 行是两处代码，各有各的判定。"""
    outcome = _outcome(
        (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=10)),
        (PERF, finding(severity=Severity.LOW, category="sqli", line=14)),
    )
    assert outcome.records == []


# --------------------------------------------------------------------------- #
# 四条规则
# --------------------------------------------------------------------------- #


def test_category_authority_the_in_lane_claim_wins() -> None:
    """``sqli`` 是安全 Worker 的职责域，严重度以它为准。"""
    outcome = _outcome(
        (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=12)),
        (PERF, finding(severity=Severity.MEDIUM, category="sqli", line=12)),
    )
    record = outcome.records[0]

    assert record.resolution_rule == "category_authority"
    assert record.winner_worker is SEC
    assert record.loser_worker is PERF
    assert record.winner_severity is Severity.CRITICAL
    assert record.loser_severity is Severity.MEDIUM
    # 败方真的不在结果里了
    remaining = [f for r in outcome.results for f in r.findings]
    assert len(remaining) == 1
    assert remaining[0].severity is Severity.CRITICAL


def test_out_of_lane_downgrade_the_in_lane_claim_wins_even_though_it_is_less_severe() -> None:
    """越界的高危判不过职责域内的低危 —— 而且理由里要说出降了几级。"""
    outcome = _outcome(
        (PERF, finding(severity=Severity.CRITICAL, category="sqli", line=12)),
        (SEC, finding(severity=Severity.LOW, category="sqli", line=12)),
    )
    record = outcome.records[0]

    assert record.resolution_rule == "out_of_lane_downgrade"
    assert record.winner_worker is SEC
    assert record.loser_worker is PERF
    assert record.winner_severity is Severity.LOW
    # 降一级的事实必须出现在理由里 —— 它是这条规则的**全部内容**，
    # 只写在代码里而理由不解释，读报告的人就只看到「低危赢了高危」
    assert "降一级" in record.rationale
    assert "high" in record.rationale


def test_evidence_adjudication_when_the_category_has_no_owner() -> None:
    """类目不在职责域表里（LLM 编的）→ 改由「行号是否落在 diff 变更行上」裁决。"""
    outcome = _outcome(
        (
            SEC,
            finding(
                severity=Severity.CRITICAL,
                category="made_up_category",
                line=12,
                source_line_verified=True,
            ),
        ),
        (
            PERF,
            finding(
                severity=Severity.LOW,
                category="made_up_category",
                line=12,
                source_line_verified=False,
            ),
        ),
    )
    record = outcome.records[0]

    assert record.resolution_rule == "evidence_adjudication"
    assert record.winner_worker is SEC


def test_unresolved_keeps_the_more_severe_one_and_flags_it_for_a_human() -> None:
    """两条都无法归属、也都拿不出证据 → 保留严重的那条，但**明确说裁不出来**。"""
    outcome = _outcome(
        (SEC, finding(severity=Severity.CRITICAL, category="made_up_category", line=12, confidence=0.9)),
        (PERF, finding(severity=Severity.LOW, category="made_up_category", line=12, confidence=0.5)),
    )
    record = outcome.records[0]

    assert record.resolution_rule == "unresolved"
    assert record.winner_worker is SEC
    assert len(outcome.needs_human_review) == 1
    # 置信度被压到 min(0.9, 0.5) × 0.85 = 0.425
    kept = [f for r in outcome.results for f in r.findings]
    assert kept[0].confidence == pytest.approx(0.5 * 0.85)


def test_an_unknown_category_falls_through_to_the_last_rule() -> None:
    """显式传一张空表 = 「没有职责域信息」，四条规则要能一路落到最后一条。

    这条同时说明规则的**顺序**是对的：前两条查不到归属就往下走，
    而不是当成「无冲突」直接放过。
    """
    outcome = resolve_conflicts(
        _results(
            (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=12)),
            (PERF, finding(severity=Severity.LOW, category="sqli", line=12)),
        ),
        authority={},
    )
    assert outcome.records[0].resolution_rule == "unresolved"


def test_the_authority_table_comes_from_the_contract_layer() -> None:
    """默认的职责域表就是契约层那张 —— 没有第二份定义。"""
    assert CATEGORY_OWNER["sqli"] is SEC
    assert CATEGORY_OWNER["n_plus_one"] is PERF
    assert CATEGORY_OWNER["naming"] is STY
    assert CONFLICT_SEVERITY_GAP == 2


# --------------------------------------------------------------------------- #
# 与聚类的顺序 —— 这两条是这个文件里最值钱的
# --------------------------------------------------------------------------- #


def test_the_loser_does_not_become_corroboration() -> None:
    """**败方不是印证。**

    两条一模一样的措辞（聚类本来一定会把它们合成一条、``corroboration_count``
    变成 2），但其中一条严重度低 2 级 —— 那是一次分歧，不是一次互相支持。
    数字虚高的后果是评报告里「跨 Worker 印证」的比例无端变好看，
    而它是去重环节唯一被展示的指标。
    """
    message = "SQL 用 f-string 拼接，用户输入没有参数化"
    merged = merge_findings(
        resolve_conflicts(
            _results(
                (SEC, finding(message=message, severity=Severity.CRITICAL, category="sqli", line=12)),
                (PERF, finding(message=message, severity=Severity.LOW, category="sqli", line=12)),
            )
        ).results
    )

    assert len(merged) == 1
    assert merged[0].corroboration_count == 1
    assert merged[0].sources == [SEC]


def test_a_conflict_is_still_found_when_the_wording_is_identical() -> None:
    """措辞**完全一样**时冲突照样要能被发现。

    这是「必须先裁冲突再聚类」的证据：聚类会把这两条合成一条
    （同路径、同类目、行号相同、相似度 1.0），而合成之后代表选举取严重度更高的那条 ——
    分歧被抹平，且报告里看不出任何异样。
    """
    message = "循环里逐个查询数据库"
    # 风格 Worker 越界报了个 CRITICAL 的 N+1，而 N+1 是性能 Worker 的职责域 ——
    # 越界所以走第二条规则，而不是第一条。
    outcome = resolve_conflicts(
        _results(
            (STY, finding(message=message, severity=Severity.CRITICAL, category="n_plus_one", line=10)),
            (PERF, finding(message=message, severity=Severity.LOW, category="n_plus_one", line=10)),
        )
    )

    assert len(outcome.records) == 1
    assert outcome.records[0].resolution_rule == "out_of_lane_downgrade"
    assert outcome.records[0].winner_worker is PERF


def test_the_winner_carries_its_conflict_record() -> None:
    """胜者的发现身上带着裁决记录 —— 前端在条目上挂冲突徽章靠的就是它。"""
    report = aggregate_run(
        run_row(),
        _results(
            (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=12)),
            (PERF, finding(severity=Severity.MEDIUM, category="sqli", line=12)),
        ),
    )

    assert len(report.conflicts) == 1
    assert len(report.findings) == 1
    assert isinstance(report.findings[0].conflict, ConflictRecord)
    assert report.findings[0].conflict.resolution_rule == "category_authority"


def test_an_unresolved_conflict_is_published_even_below_the_confidence_gate() -> None:
    """**「我们不确定」不能被沉默掉。**

    ``unresolved`` 会把置信度压到阈值以下。如果这里被 ``split_by_confidence``
    照常砍掉，那句「两个专家吵起来了，我们裁不出来」就变成了**什么都不显示** ——
    而 ``needs_human_review`` 本来就是为「交给人看一眼」设的。
    """
    report = aggregate_run(
        run_row(),
        _results(
            (
                SEC,
                finding(severity=Severity.CRITICAL, category="made_up", line=12, confidence=0.3),
            ),
            (PERF, finding(severity=Severity.LOW, category="made_up", line=12, confidence=0.3)),
        ),
    )

    assert len(report.conflicts) == 1
    assert report.conflicts[0].resolution_rule == "unresolved"
    assert len(report.findings) == 1
    finding_out = report.findings[0]
    assert finding_out.needs_human_review is True
    # 置信度确实被压到了阈值以下 —— 否则这条测试证明不了任何东西
    assert finding_out.adjusted_confidence < SUPPRESS_THRESHOLD
    assert report.suppressed == []


# --------------------------------------------------------------------------- #
# 确定性
# --------------------------------------------------------------------------- #


def test_resolution_does_not_depend_on_arrival_order() -> None:
    """三条 Worker 上报的到达顺序不固定，而冲突记录必须与顺序无关。

    记录里的理由是一句人读的中文 —— 它跟着顺序变的话，同一份 diff 在
    不同机器上会给出不同的裁决说明，而「可复现」正是这一层全部的意义。
    """
    a = (SEC, finding(severity=Severity.CRITICAL, category="sqli", line=12))
    b = (PERF, finding(severity=Severity.MEDIUM, category="sqli", line=12))
    c = (STY, finding(severity=Severity.LOW, category="naming", line=40))

    forward = _outcome(a, b, c)
    backward = _outcome(c, b, a)

    assert forward.records == backward.records
    assert forward.winner_conflicts == backward.winner_conflicts
