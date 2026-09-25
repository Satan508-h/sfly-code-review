"""聚合流水线的纯函数测试 —— **毫秒级、无 IO、无 LLM**。

这是全项目价值最高的测试套件所在（计划里那句话在 M9 会变成表驱动的
``cases/*.yaml``）。现在它测四件事：合并、置信度、阻断决策、评论渲染。

M5 的合并只做**指纹精确匹配**（同一处、同一句话、同一个类目）。相似度聚类
（两个 Worker 用不同措辞说同一件事）是 M9，所以这里不去测它 —— 测一个还没写的
行为只会把测试写成愿望清单。
"""

from __future__ import annotations

import pytest

from factories import aggregated, finding, result
from factories import report as make_report
from sfly_agent.aggregate.confidence import (
    SUPPRESS_THRESHOLD,
    adjusted_confidence,
    agreement_term,
    diversity_term,
)
from sfly_agent.aggregate.decision import (
    BLOCKING_CATEGORIES,
    HIGH_SEVERITY_BLOCK_COUNT,
    REASON_TEXT,
    decide,
)
from sfly_agent.aggregate.pipeline import (
    aggregate_run,
    finalize_run,
    merge_findings,
    split_by_confidence,
)
from sfly_agent.aggregate.render import MAX_LISTED, render_comment
from sfly_shared.contracts import (
    AggregatedFinding,
    RunRow,
    RunStatus,
    Severity,
    WorkerResult,
    WorkerType,
)

pytestmark = pytest.mark.unit


def _run(**over: object) -> RunRow:
    base: dict[str, object] = {
        "task_id": "01JTESTRUN0000000000000000",
        "idempotency_key": "123456:7:" + "a" * 40,
        "repo_id": "123456",
        "repo_node_id": "R_kgDOAbcdef",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "status": RunStatus.AGGREGATING,
        "planned_workers": list(WorkerType),
        "deadline_at": __import__("datetime").datetime.now(__import__("datetime").UTC),
        "files_total": 4,
        "files_reviewed": 4,
    }
    base.update(over)
    return RunRow.model_validate(base)


# --------------------------------------------------------------------------- #
# 合并
# --------------------------------------------------------------------------- #


def test_the_same_finding_from_two_workers_merges_into_one() -> None:
    """**跨 Worker 印证是这个设计唯一能产出的东西**，所以它必须有测试。

    两个 Worker 报出同一处、同一句话时合成一条，``sources`` 记下两边 ——
    前端要展示的「两个专家独立地说同一件事」就是这两个词。
    """
    shared = finding(message="SQL 用 f-string 拼接", category="sqli")
    merged = merge_findings(
        [
            result("t1", worker_type=WorkerType.SECURITY, findings=[shared]),
            result("t1", worker_type=WorkerType.PERFORMANCE, findings=[shared.model_copy()]),
        ]
    )

    assert len(merged) == 1
    # ``sources`` 按枚举值排序（不是按到达顺序）—— 它是稳定输出的一部分：
    # 报告要能逐字节复现，而三个 Worker 的到达顺序是不确定的。
    assert merged[0].sources == [WorkerType.PERFORMANCE, WorkerType.SECURITY]
    assert merged[0].corroboration_count == 2


def test_findings_that_differ_get_their_own_entry() -> None:
    """措辞不同就**不合并** —— M5 只做指纹精确匹配。"""
    merged = merge_findings(
        [
            result(
                "t1",
                findings=[
                    finding(message="SQL 用 f-string 拼接"),
                    finding(message="这条 SQL 没有参数化，输入可以改写语义"),
                ],
            )
        ]
    )
    assert len(merged) == 2


def test_a_single_workers_duplicate_is_not_counted_as_cross_worker() -> None:
    """同一个 Worker 报两遍不算「跨 Worker 印证」，``sources`` 里只有一个。"""
    same = finding()
    merged = merge_findings([result("t1", findings=[same, same.model_copy()])])

    assert len(merged) == 1
    assert merged[0].sources == [WorkerType.SECURITY]
    assert merged[0].corroboration_count == 2


def test_merging_is_deterministic_regardless_of_arrival_order() -> None:
    """同样的输入换个顺序，得到的报告必须**逐字节相同**。

    这是评测可复现的前提：三个 Worker 是并行的，谁先到不确定 ——
    如果报告跟着到达顺序变，那「改了聚类阈值，精确率涨了 3%」就说不清
    是改动带来的还是抖动带来的。
    """
    a = finding(message="A", category="sqli", line=10)
    b = finding(message="B", category="xss", line=20)
    c = finding(message="C", category="secrets", line=30)
    order1 = [result("t1", worker_type=WorkerType.SECURITY, findings=[a, b, c])]
    order2 = [result("t1", worker_type=WorkerType.SECURITY, findings=[c, a, b])]

    assert merge_findings(order1) == merge_findings(order2)


def test_the_representative_is_the_most_severe_and_most_confident() -> None:
    """簇代表按 ``严重度秩 × 0.5 + 置信度 × 0.5`` 选举。

    同分时的兜底必须是确定的：它决定评论里显示哪句话、推荐哪个修改建议。
    """
    low = finding(message="M", severity=Severity.LOW, confidence=0.9)
    high = finding(message="M", severity=Severity.CRITICAL, confidence=0.9)
    merged = merge_findings([result("t1", findings=[low, high])])

    assert len(merged) == 1
    assert merged[0].severity is Severity.CRITICAL


# --------------------------------------------------------------------------- #
# 置信度
# --------------------------------------------------------------------------- #


def test_agreement_saturates() -> None:
    """一致度必须是**饱和**的：第二个来源的价值远小于第一个。

    1 / 2 / 3 个来源对应 0 / 0.49 / 0.74 —— 「有一个印证」是最值钱的一步。
    """
    assert agreement_term(1) == 0.0
    assert agreement_term(2) == pytest.approx(0.486, abs=0.005)
    assert agreement_term(3) == pytest.approx(0.736, abs=0.005)
    assert agreement_term(3) - agreement_term(2) < agreement_term(2) - agreement_term(1)


def test_diversity_counts_only_distinct_workers() -> None:
    assert diversity_term(1) == 0.0
    assert diversity_term(2) == pytest.approx(0.5)
    assert diversity_term(3) == pytest.approx(1.0)
    # 四个 Worker 是将来才有的事，但公式不该在那时给出 1.5。
    assert diversity_term(9) == 1.0


def test_a_lone_finding_scores_below_the_models_own_claim() -> None:
    """没有旁证的单条声称，重算之后一定要**低于**模型自报的值。

    模型的自报置信度系统性偏高（它给的是「我有多想说这句话」）。
    这条测试是那个判断的守门人：哪天有人把权重调成「基本照抄模型」，
    它会红。
    """
    solo = finding(severity=Severity.MEDIUM, confidence=0.9)
    score = adjusted_confidence(solo, member_count=1, distinct_workers=1, grounded=False)

    assert score < solo.confidence
    assert score == pytest.approx(0.8 * 0.55 * 0.9)


def test_corroboration_and_grounding_raise_the_score() -> None:
    solo = finding(severity=Severity.HIGH, confidence=0.6)
    base = adjusted_confidence(solo, member_count=1, distinct_workers=1, grounded=False)
    boosted = adjusted_confidence(solo, member_count=3, distinct_workers=3, grounded=True)

    assert boosted > base


def test_the_score_never_leaves_zero_to_one() -> None:
    """极端输入不能产出区间外的值 —— 它会被写进 jsonb，然后在前端变成一个
    宽度为 120% 的进度条。"""
    perfect = finding(severity=Severity.CRITICAL, confidence=1.0, rule_id="sec-sqli-001")
    assert adjusted_confidence(perfect, member_count=5, distinct_workers=3, grounded=True) <= 1.0
    assert adjusted_confidence(perfect, member_count=0, distinct_workers=0, grounded=False) >= 0.0


def test_low_confidence_findings_are_suppressed_not_dropped() -> None:
    """被闸掉的发现**要留下来**：评测靠它们测量这道闸砍掉了多少召回。

    只存发布出去的话，阈值就只能盲调 —— 而盲调出来的阈值一问就穿。
    """
    weak = aggregated(adjusted_confidence=SUPPRESS_THRESHOLD - 0.01)
    strong = aggregated(adjusted_confidence=SUPPRESS_THRESHOLD + 0.01, message="另一条")
    published, suppressed = split_by_confidence([weak, strong])

    assert [f.message for f in published] == [strong.message]
    assert [f.message for f in suppressed] == [weak.message]
    assert suppressed[0].stage == "suppressed"


# --------------------------------------------------------------------------- #
# 阻断决策
# --------------------------------------------------------------------------- #


def test_no_findings_never_blocks() -> None:
    decision = decide([])
    assert decision.block_merge is False
    assert decision.reason == "below_threshold"


@pytest.mark.parametrize("severity", list(Severity))
def test_secrets_block_regardless_of_severity_or_confidence(severity: Severity) -> None:
    """凭据泄露**不看严重度也不看置信度**，因为代价不对称：

    误报的代价是作者多看一眼，漏报的代价是密钥进了 git 历史（且要轮换）。
    """
    weak = aggregated(category="secrets", severity=severity, adjusted_confidence=0.01)
    assert decide([weak]).reason == "secrets_found"


#: ``secrets`` 排除在外：它走的是**第一条**规则（不看严重度也不看置信度），
#: 单独有一条测试。放进这个参数化里会让人以为它在测置信度门槛。
_CRITICAL_ONLY_CATEGORIES = sorted(BLOCKING_CATEGORIES - {"secrets"})


@pytest.mark.parametrize("category", _CRITICAL_ONLY_CATEGORIES)
def test_a_confident_critical_in_a_blocking_category_blocks(category: str) -> None:
    finding_ = aggregated(category=category, severity=Severity.CRITICAL, adjusted_confidence=0.61)
    assert decide([finding_]).reason == "blocking_critical"


def test_a_confident_critical_outside_the_policy_list_does_not_block_alone() -> None:
    """类目不在策略白名单里时，单条 critical 不阻断。

    白名单的意义就在这里：没有它，「模型说 critical 就拦」等于把合并门禁
    交给了一个会系统性偏高的自报数字。
    """
    finding_ = aggregated(category="magic_number", severity=Severity.CRITICAL, adjusted_confidence=0.99)
    assert decide([finding_]).block_merge is False


def test_many_high_severity_findings_block_together() -> None:
    """单看每一条都不足以阻断，但它们同时出现时「这个 PR 要认真看一遍」
    这件事本身是确定的。"""
    few = [
        aggregated(category="n_plus_one", severity=Severity.HIGH, adjusted_confidence=0.8, message=f"m{i}")
        for i in range(HIGH_SEVERITY_BLOCK_COUNT - 1)
    ]
    assert decide(few).block_merge is False

    one_more = [*few, aggregated(category="quadratic", severity=Severity.HIGH, adjusted_confidence=0.8)]
    assert decide(one_more).reason == "too_many_high"


def test_low_confidence_highs_do_not_accumulate_into_a_block() -> None:
    """置信度不够的高危不参与累积 —— 否则这个规则会变成「多报几条就能拦住」。"""
    many = [
        aggregated(category="n_plus_one", severity=Severity.HIGH, adjusted_confidence=0.3, message=f"m{i}")
        for i in range(10)
    ]
    assert decide(many).block_merge is False


def test_every_reason_has_a_human_readable_text() -> None:
    """新增一条规则却忘了写说明，症状是评论里冒出一个英文 slug。"""
    for reason in ("secrets_found", "blocking_critical", "too_many_high", "below_threshold"):
        assert REASON_TEXT[reason] and REASON_TEXT[reason] != reason


# --------------------------------------------------------------------------- #
# 渲染
# --------------------------------------------------------------------------- #


def test_the_comment_starts_with_the_hidden_marker() -> None:
    """隐藏标记是防重复评论的第二道闸（第一道是 ``github_comment_id``）：
    两种失效方式各挡一种，只用一道的话两种情况各会产出一次重复评论。"""
    report = make_report()
    assert render_comment(report).startswith(f"<!-- sfly:run:{report.task_id} -->")


def test_the_comment_never_says_approved() -> None:
    """**永不发 APPROVE。**机器人审批人类 PR 是策略漏洞：一个被绕过的模型
    会拿到一个和人工审批长得一模一样的绿色标记。"""
    body = render_comment(make_report(block_merge=False))
    assert "通过" not in body.replace("未通过", "") or "供参考" in body
    assert "APPROVE" not in body


def test_the_degraded_notice_is_at_the_top() -> None:
    """降级改变的是**整份报告**的可信度，所以它必须在最上面，
    而不是附在最后一行小字里。"""
    report = make_report(degraded=True, missing_workers=[WorkerType.STYLE])
    body = render_comment(report)
    notice_at = body.index("不完整")
    first_finding_at = body.index("```") if "```" in body else len(body)
    assert notice_at < first_finding_at


def test_the_comment_caps_the_number_of_listed_findings() -> None:
    """一个改了 200 个文件的大 PR 报出 60 条发现时，没有人会读完那条评论 ——
    而「读不完」的下一步是「不看」。超出部分要说清楚还剩多少，不能静默截断。"""
    many = [
        aggregated(line=i + 1, message=f"问题 {i}", adjusted_confidence=0.9) for i in range(MAX_LISTED + 5)
    ]
    body = render_comment(make_report(findings=many))

    assert f"还有 {len(many) - MAX_LISTED} 条未在此列出" in body


def test_every_finding_line_carries_its_sources() -> None:
    """跨 Worker 印证要在正文里**显式说出来** ——「两个不同的 Worker 各自
    独立地发现了同一处」比「有两个来源」对读者的说服力完全不同。"""
    body = render_comment(make_report(findings=[aggregated(sources=[WorkerType.SECURITY, WorkerType.STYLE])]))
    assert "安全" in body and "风格" in body


# --------------------------------------------------------------------------- #
# 端到端（纯内存）
# --------------------------------------------------------------------------- #


def test_aggregate_then_finalize_produces_a_publishable_report() -> None:
    results: list[WorkerResult] = [
        result("t1", worker_type=WorkerType.SECURITY, findings=[finding(category="secrets")]),
        result(
            "t1", worker_type=WorkerType.PERFORMANCE, findings=[finding(message="N+1", category="n_plus_one")]
        ),
        result("t1", worker_type=WorkerType.STYLE, findings=[]),
    ]
    report = aggregate_run(_run(), results, cost_usd=0.0021)
    assert report.degraded is False
    assert report.missing_workers == []
    assert report.totals.tokens_in == 1234 * 3
    assert report.totals.cost_usd == pytest.approx(0.0021)
    # per_worker_ms 是**每个** Worker 自己的耗时，不求和 —— 三个 Worker 并行。
    assert set(report.totals.per_worker_ms) == {"security", "performance", "style"}

    finalize_run(report)
    assert report.block_merge is True  # secrets
    assert report.comment_body


def test_a_failed_worker_makes_the_report_degraded_even_though_the_barrier_closed() -> None:
    """**这条容易漏**：三条结果都按时上报了，屏障闭合得非常干净，
    但其中一条是失败结果 —— 报告仍然是不完整的。

    M5 实测踩到过：一开始 ``missing_workers`` 是按「谁没在数据库里」算的，
    而 ``wait`` 会给超时的 Worker 补一条 failed 结果，于是报告显示
    ``degraded=True`` 而 ``missing_workers=[]`` —— 前端那个降级徽章
    找不到任何一个可以显示的名字。
    """
    results = [
        result("t1", worker_type=WorkerType.SECURITY),
        result("t1", worker_type=WorkerType.PERFORMANCE),
        WorkerResult.failed("t1", WorkerType.STYLE, "模型返回了没法解析的东西"),
    ]
    report = aggregate_run(_run(), results)

    assert report.degraded is True
    assert report.missing_workers == [WorkerType.STYLE]


def test_a_worker_that_never_reported_is_missing() -> None:
    report = aggregate_run(_run(), [result("t1", worker_type=WorkerType.SECURITY)])

    assert report.degraded is True
    assert set(report.missing_workers) == {WorkerType.PERFORMANCE, WorkerType.STYLE}


def test_the_report_is_json_round_trippable() -> None:
    """报告要进 jsonb 列。``model_dump(mode="json")`` 是那条路径的入口 ——
    它必须能吃下全部字段（包括枚举和 datetime）。"""
    report = finalize_run(aggregate_run(_run(), [result("t1")]))
    payload = report.model_dump(mode="json")
    restored = type(report).model_validate(payload)

    # 比 JSON 而不是比对象：datetime 的 tzinfo 在 ``!=`` 上是出了名的坑
    # （``timezone.utc`` 与 ``timezone(timedelta(0))`` 相等，但
    # ``datetime`` 的相等判定会走到 tzinfo 的比较上）。而这里真正要验证的
    # 恰恰是「走一圈 jsonb 之后还是同一个形状」—— 那就直接比那一圈的结果。
    assert restored.model_dump(mode="json") == payload


def test_aggregated_finding_is_the_contract_type() -> None:
    """``merge_findings`` 返回的必须真的是 ``AggregatedFinding``——
    多出来的那几个字段（sources / adjusted_confidence）是聚合的产物，
    用 ``Finding`` 装不下。"""
    merged = merge_findings([result("t1")])
    assert all(isinstance(f, AggregatedFinding) for f in merged)
