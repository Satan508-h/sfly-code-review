"""主 Agent 的聚合流水线 —— 项目真正的 IP，**全确定性、零 LLM 调用**。

    results → 合并 → 置信度重算 → 分档 → ReviewReport

为什么整条链路上一个 LLM 都不调（除了 Worker 本身）：评测要可复现。
同一批 ``WorkerResult`` 跑一百遍必须得到一模一样的报告，否则
「改了聚类阈值，精确率涨了 3%」这句话就没有意义 —— 涨幅可能只是模型抖动。

### M5 做到哪一步

M5 完成的是：**按指纹精确合并 + 置信度重算 + 分档 + 汇总**。

``fingerprint`` 是 ``sha1(路径 | 行号//3 | 类目 | 归一的 message)`` ——
它只合并「同一个 Worker 用同一句话描述同一处」这种情况，也就是**完全相同的声称**。

还差的一步是**相似度聚类**（M9）：两个 Worker 用不同措辞说同一处问题、
或者模型这次报第 10 行下次报第 12 行时，指纹不同而它们其实是同一件事。
那一步是 O(n²) 的并查集 + rapidfuzz 阈值（同 Worker 0.75 / 跨 Worker 0.55），
在这里插进来 —— :func:`merge_findings` 就是它的扩展点。

之所以敢分成两步：**指纹那一步永远不会被替换掉**，它是并查集的快速路径
（指纹相同直接合并，不必做字符串相似度）。M9 加的是它后面的兜底，
不是把它推倒重来。

### 冲突消解不在这个文件里

同路径 + 邻近行号 + 不同 Worker + 严重度差 ≥ 2 才是冲突（``conflicts.py``，M9）。
M5 的 ``report.conflicts`` 因此恒为空 —— 前端（M8）要在空列表上正常工作，
这比先塞一堆假数据进去安全。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sfly_agent.aggregate.confidence import SUPPRESS_THRESHOLD, adjusted_confidence
from sfly_agent.aggregate.decision import decide
from sfly_agent.aggregate.fingerprint import fingerprint
from sfly_agent.aggregate.render import render_comment
from sfly_shared.contracts import (
    SEVERITY_RANK,
    AggregatedFinding,
    Finding,
    ResultStatus,
    ReviewReport,
    RunRow,
    RunTotals,
    WorkerResult,
    WorkerType,
)


def merge_findings(results: Sequence[WorkerResult]) -> list[AggregatedFinding]:
    """把全部 Worker 的发现按指纹合并，返回每个簇的代表。

    **只有代表进入结果**，簇里的其它成员不单独出现 —— 但 ``sources`` 和
    ``corroboration_count`` 把它们记了下来。跨 Worker 印证是去重最值钱的产物：
    两个专家独立地说同一件事，比一个专家说两遍可信得多。
    """
    # **``(worker_type, Finding)`` 配对，不是裸的 Finding** ——
    # ``Finding`` 上没有 ``worker_type``（它属于 ``WorkerResult``），
    # 而 ``AggregatedFinding.sources`` 恰恰要回答「哪几个 Worker 报了它」。
    # 第一次写这里时按直觉用了 ``list[Finding]``，mypy 当场指出来。
    groups: dict[str, list[tuple[WorkerType, Finding]]] = {}
    for result in results:
        # 失败的结果没有 findings（``WorkerResult.failed`` 不产出），
        # 但别假设它 —— 契约允许 partial 带 findings 同时带 error。
        for finding in result.findings:
            groups.setdefault(fingerprint(finding), []).append((result.worker_type, finding))

    merged = [_representative(members) for members in groups.values()]
    # 排序必须在**分档之前**：suppressed 也要按同样的顺序存，
    # 否则评测拿到的 suppressed 列表顺序是哈希序 —— 而它是用来人工抽查的。
    merged.sort(key=_sort_key)
    return merged


def _representative(members: list[tuple[WorkerType, Finding]]) -> AggregatedFinding:
    """从簇里选一个代表，然后把簇的统计写在它身上。

    选举规则：``严重度秩 × 0.5 + 置信度 × 0.5`` 最大者胜。
    用秩而不是严重度本身，是因为五个档位之间的距离并不相等（``critical`` 到
    ``high`` 的跨度远大于 ``low`` 到 ``info``），而线性映射会假装它们相等。

    同分时的兜底顺序是 ``(message, file, line)`` —— 必须是**确定的**：
    同分的两个成员谁当选，会决定评论里显示哪句话、推荐哪个修改建议。
    让它依赖 dict 的插入顺序（也就是 Redis 的投递顺序）意味着同一份 diff
    在不同机器上得到不同措辞的报告。
    """
    ranked = sorted(members, key=lambda pair: _election_key(pair[1]))
    representative = ranked[0][1]
    # ``worker_type`` 排序去重 → 稳定的 ``sources``（不随投递顺序变）
    workers = sorted({w for w, _ in members}, key=lambda w: w.value)

    out = AggregatedFinding(**representative.model_dump(), sources=workers)
    out.corroboration_count = len(members)
    out.adjusted_confidence = adjusted_confidence(
        representative,
        member_count=len(members),
        distinct_workers=len(workers),
        grounded=any(f.rule_id for _, f in members),
    )
    out.stage = "clustered"
    return out


def _election_key(f: Finding) -> tuple[float, str, str, int]:
    """簇代表选举的排序键（升序取第一个 = 最该当选的那个）。

    ``-(严重度秩 × 0.5 + 置信度 × 0.5)`` 是降序的写法。后三项是**确定性的兜底**：
    同分的两个成员谁当选，决定评论里显示哪句话、推荐哪个修改建议 ——
    让它依赖 dict 的插入顺序（也就是消息的投递顺序），同一份 diff 会在
    不同机器上得到不同措辞的报告。
    """
    return (-(SEVERITY_RANK[f.severity] * 0.5 + f.confidence * 0.5), f.message, f.file, f.line)


def _sort_key(f: AggregatedFinding) -> tuple[int, float, str, int]:
    # 严重度降序 → 置信度降序 → 文件升序 → 行号升序。
    # 后两项是「同严重度同置信度时」的确定性兜底，让报告的顺序可复现。
    return (-SEVERITY_RANK[f.severity], -f.adjusted_confidence, f.file, f.line)


def split_by_confidence(
    merged: Sequence[AggregatedFinding],
) -> tuple[list[AggregatedFinding], list[AggregatedFinding]]:
    """``(要发布的, 只入库的)``。

    被砍掉的那一批**必须留下来**：评测要用它们测量「这道闸砍掉了多少召回」。
    只存发布出去的，阈值就只能盲调 —— 而盲调出来的阈值在面试里一问就穿。
    """
    published: list[AggregatedFinding] = []
    suppressed: list[AggregatedFinding] = []
    for finding in merged:
        if finding.adjusted_confidence < SUPPRESS_THRESHOLD:
            suppressed.append(finding.model_copy(update={"stage": "suppressed"}))
        else:
            published.append(finding)
    return published, suppressed


def aggregate_run(
    run: RunRow,
    results: Sequence[WorkerResult],
    *,
    cost_usd: float = 0.0,
    now: datetime | None = None,
) -> ReviewReport:
    """把一次 run 的全部上报收成一份报告。**纯函数**（除了读 ``run`` 和 ``results``）。

    ``cost_usd`` 由调用方从 ``llm_calls`` 表汇总后传进来 —— 聚合层不连数据库，
    也不该自己算钱：成本是**账单**，不是模型输出的一部分。
    （价格表在 ``sfly_agent/llm/pricing.py``，写入在 Worker 那一侧。）

    ``now`` 是可注入的：报告里有一个真实的墙钟耗时（``totals.duration_ms``），
    而测试要能把它钉死。默认为当前 UTC 时间。
    """
    now = now or datetime.now(UTC)
    # **「missing」的含义是「这一路没有产出可用的结果」，不是「谁没上报」。**
    #
    # 这两个定义在超时路径上会分叉，而且分叉得很隐蔽：``wait`` 给掉队的 Worker
    # 补写过 failed 结果（约定 #2，不补屏障就闭合不了），所以「谁没上报」在
    # aggregate 跑的时候**永远是空集** —— 报告于是会显示 degraded=True 而
    # missing_workers=[]，前端那个降级徽章找不到任何一个可以显示的名字。
    #
    # 按「没有可用结果」来算就不依赖任何额外输入，也顺带覆盖了「Worker 上报了
    # 一条失败结果」那一类 —— 那种情况审查同样是不完整的，而它很容易被漏掉，
    # 因为屏障闭合得非常干净（三条结果都在，只是其中一条是 failed）。
    usable = {r.worker_type for r in results if r.status is not ResultStatus.FAILED}
    missing = [w for w in run.planned_workers if w not in usable]

    published, suppressed = split_by_confidence(merge_findings(results))

    return ReviewReport(
        task_id=run.task_id,
        repo_id=run.repo_id,
        repo_node_id=run.repo_node_id,
        pr_number=run.pr_number,
        head_sha=run.head_sha,
        base_sha=run.base_sha,
        findings=published,
        suppressed=suppressed,
        conflicts=[],  # M9
        files_total=run.files_total,
        files_reviewed=run.files_reviewed,
        diff_truncated=run.diff_truncated,
        # 「有哪一路没产出可用结果」就是降级的**全部**含义 —— 两个来源
        # （没上报、上报了失败结果）都已经并进 ``missing`` 里了，
        # 所以这里不需要再或一个条件。多写一条不改变结果的分支，只会在
        # 有人改动 ``missing`` 的定义时留下来继续说话。
        degraded=bool(missing),
        missing_workers=missing,
        totals=_totals(results, run, cost_usd=cost_usd, now=now),
    )


def finalize_run(report: ReviewReport) -> ReviewReport:
    """``finalize`` 节点：决定阻断、写评论正文。**在 ``aggregate`` 之后、``publish`` 之前。**

    分成两个节点的理由不是「代码放不下」，而是**两者的失败后果不同**：
    聚合失败意味着没有报告；渲染失败意味着报告在但没法发布（重跑渲染即可，
    不必重跑聚合）。M5 里两者都是纯函数、都不会失败，但 M7 之后
    ``publish`` 会真的去调 GitHub（会限流、会 401），那时这个边界就开始值钱了。
    """
    decision = decide(report.findings)
    report.block_merge = decision.block_merge
    report.decision_reason = decision.reason
    report.comment_body = render_comment(report)
    return report


def _totals(results: Iterable[WorkerResult], run: RunRow, *, cost_usd: float, now: datetime) -> RunTotals:
    totals = RunTotals(cost_usd=cost_usd)
    for result in results:
        totals.tokens_in += result.tokens_in
        totals.tokens_out += result.tokens_out
        totals.cached_tokens += result.cached_tokens
        # 每个 Worker 自己的耗时。**不加总**：三个 Worker 是并行跑的，
        # 把它们加起来会得到一个比实际大一倍的数字，而那个数字看起来完全合理，
        # 所以没有人会怀疑它。整条链路的耗时是下面的 duration_ms。
        totals.per_worker_ms[result.worker_type.value] = result.latency_ms
    started = run.dispatched_at or run.created_at
    totals.duration_ms = max(0, int((now - started).total_seconds() * 1000))
    return totals
