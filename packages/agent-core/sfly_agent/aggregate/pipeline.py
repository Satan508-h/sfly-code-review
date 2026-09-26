"""主 Agent 的聚合流水线 —— 项目真正的 IP，**全确定性、零 LLM 调用**。

    results → 裁冲突 → 合并 → 置信度重算 → 分档 → ReviewReport

为什么整条链路上一个 LLM 都不调（除了 Worker 本身）：评测要可复现。
同一批 ``WorkerResult`` 跑一百遍必须得到一模一样的报告，否则
「改了聚类阈值，精确率涨了 3%」这句话就没有意义 —— 涨幅可能只是模型抖动。

### 合并分两层，两层都在 ``cluster.py`` 里

M5 只做了第一层：**按指纹精确合并**（同一个 Worker 用同一句话描述同一处）。
M9 补上第二层：**相似度聚类**（并查集 + rapidfuzz 阈值）。

分两步做是刻意的，但**不要把这两层理解成「快速路径 + 慢速兜底」** ——
它们其实是同一个判断的两个口径，而 M9 之后由第二层单独承担全部合并：

* 指纹相同 → 路径、类目、行号桶、归一化措辞全相同，那么聚类那四条判据
  **必然全部满足**（同一个桶内 ``|Δline| ≤ 2 < 3``，措辞相同则相似度为 1）。
  也就是说第一层能被第二层完全覆盖。
* 反过来不行：指纹不同而聚类该合并的情况，正是第二层存在的理由。

那为什么不先按指纹分组、只比较组代表？因为 **``line // 3`` 的桶边界会咬人**：
第 0 行和第 4 行分属两个桶（差 4 > 3，确实不该合并），但 ``{第 0 行, 第 2 行}``
这一组和第 4 行的那一组，代表选举（同分取行号小的）会选出第 0 行 ——
于是**一对本来该合并的成员（第 2 行与第 4 行）被代表挡住了**。
按发现全量两两比较就没有这个问题，而 n 只有几百，全量比较的代价可以忽略。

``fingerprint`` 因此不再参与合并判断，但它**没有变成死代码**：
它是 ``review_results.fingerprint`` 那一列的值，用于跨 run 的分析与排查。
合并逻辑与落库标识分开，本来就是两件事。

### 冲突消解**在**这个文件的上游，而且必须在聚类之前

同路径 + 邻近行号 + **同类目** + 不同 Worker + 严重度差 ≥ 2 才是冲突
（``conflicts.py``）。顺序是：

    results → 裁冲突（去掉败方）→ 聚类去重 → 置信度分档 → ReviewReport

这个顺序有两处不能换，两处都会静默出错：

* **聚类在裁冲突之后。** 聚类按代表选举取「严重度更高的那条」，会把
  「两个 Worker 对严重度有分歧」这件事抹平 —— 抹平之后没有冲突可发现，
  看起来一切正常（一条 CRITICAL 和一条 MEDIUM 合成了 CRITICAL，
  谁也不会问它俩当时是不是吵过）。
* **败方在聚类之前移除。** 一条被裁决掉的声称**不是印证**；留着它
  会混进胜者的 ``corroboration_count``，把一次分歧算成一次互相支持。

聚类那边也为冲突让了路：**类目不同的两条永不合并**。同一位置上的不同类目
是两件事（注入和命名可以同时成立），合并掉它就等于把一条真实发现静默吃掉。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sfly_agent.aggregate.cluster import cluster_findings
from sfly_agent.aggregate.confidence import SUPPRESS_THRESHOLD, adjusted_confidence
from sfly_agent.aggregate.conflicts import resolve_conflicts
from sfly_agent.aggregate.decision import decide
from sfly_agent.aggregate.fingerprint import fingerprint
from sfly_agent.aggregate.render import render_comment
from sfly_agent.llm.mock import is_mock_model
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
    """把全部 Worker 的发现聚成簇，返回每个簇的代表。

    **只有代表进入结果**，簇里的其它成员不单独出现 —— 但 ``sources`` 和
    ``corroboration_count`` 把它们记了下来。跨 Worker 印证是去重最值钱的产物：
    两个专家独立地说同一件事，比一个专家说两遍可信得多。

    簇怎么划在 ``cluster.py``；这里只管选代表、排序、写统计量。
    """
    merged = [_representative(members) for members in cluster_findings(results)]
    # 排序必须在**分档之前**：suppressed 也要按同样的顺序存，
    # 否则评测拿到的 suppressed 列表顺序是哈希序 —— 而它是用来人工抽查的。
    merged.sort(key=_sort_key)
    # ``cluster_id`` 排在排序之后分配，因为它记的是**这个簇在报告里的位置**，
    # 不是发现的属性。提前分配会让它跟着输入顺序漂（三条结果从 Redis 来的
    # 顺序不固定），于是同一份 diff 在不同机器上得到不同的 cluster_id。
    for index, finding in enumerate(merged):
        finding.cluster_id = index
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

    **标了 ``needs_human_review`` 的一律发布，不看置信度。** 这道闸的职责是
    去掉「大概率是假的」声称，而 ``needs_human_review`` 的含义正相反 ——
    「两个专家吵起来了，我们裁不出来」。把它按低置信度悄悄丢掉，
    等于**把一句「这件事我们不确定」变成了沉默**，而这正是整个项目一直在
    防的那类失败（低置信度的分数会照常显示出来，读的人自己会打折）。
    """
    published: list[AggregatedFinding] = []
    suppressed: list[AggregatedFinding] = []
    for finding in merged:
        if finding.needs_human_review:
            published.append(finding)
        elif finding.adjusted_confidence < SUPPRESS_THRESHOLD:
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
    reported = [r for r in results if r.status is not ResultStatus.FAILED]
    missing = [w for w in run.planned_workers if w not in {r.worker_type for r in reported}]

    # **顺序不能换：先裁冲突，再去重。**
    # 反过来的话，聚类会按代表选举取「严重度更高的那条」，把「两个 Worker
    # 对严重度有分歧」这件事抹平 —— 抹平之后没有冲突可发现，而且看起来
    # 一切正常。另一面：被裁决掉的败方**不是印证**，所以它必须在聚类之前
    # 移除，否则会混进胜者的 ``corroboration_count``，把一次分歧算成一次互相支持。
    outcome = resolve_conflicts(results)
    merged = merge_findings(outcome.results)
    for finding in merged:
        # 指纹能把「原始声称」和「合并后的代表」对上：AggregatedFinding 是
        # Finding 的子类，文件/类目/消息/行号都没变。用指纹而不是 ``id()``，
        # 是因为代表是 ``model_dump()`` 造出来的**新对象**。
        key = fingerprint(finding)
        record = outcome.winner_conflicts.get(key)
        if record is not None:
            finding.conflict = record
        if key in outcome.needs_human_review:
            finding.needs_human_review = True

    published, suppressed = split_by_confidence(merged)

    return ReviewReport(
        task_id=run.task_id,
        repo_id=run.repo_id,
        repo_node_id=run.repo_node_id,
        pr_number=run.pr_number,
        head_sha=run.head_sha,
        base_sha=run.base_sha,
        findings=published,
        suppressed=suppressed,
        conflicts=outcome.records,
        files_total=run.files_total,
        files_reviewed=run.files_reviewed,
        diff_truncated=run.diff_truncated,
        # 「有哪一路没产出可用结果」就是降级的**全部**含义 —— 两个来源
        # （没上报、上报了失败结果）都已经并进 ``missing`` 里了，
        # 所以这里不需要再或一个条件。多写一条不改变结果的分支，只会在
        # 有人改动 ``missing`` 的定义时留下来继续说话。
        degraded=bool(missing),
        missing_workers=missing,
        # 「这次的发现是不是全都来自扫描器」。判据落在**每条结果自己的模型名**上，
        # 而不是 ``LLM_PROVIDER`` —— 一个 run 可能一半来自模型、一半来自扫描器
        # （配额在审查中途用完，三个 Worker 各有各的账），那时 provider 配置
        # 仍然是 deepseek。所以只有「全部都来自扫描器」才算 ``scanned_only``：
        # 只要有一条是模型产的，这份报告就不是「规则扫描器的结果」。
        #
        # **只看 ``reported``**：失败的结果没有模型名（它什么都没产出），
        # 拿它参与 ``all()`` 会把一份全扫描器的报告拉成「有模型的报告」——
        # 而那份报告里的每一条发现确实都来自扫描器。没产出结果的 Worker
        # 由 ``degraded`` 那一栏说，不用在这里再说一次。
        scanned_only=bool(reported) and all(is_mock_model(r.model) for r in reported),
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
