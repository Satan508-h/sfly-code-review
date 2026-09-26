"""冲突消解 —— 确定性规则引擎，**零 LLM 调用**。

两个 Worker 在同一处给出**严重度差 ≥ 2** 的判定时，必须有人裁决。用 LLM 当裁判
会引入非确定性，直接把评测的可复现性毁掉（同一批输入跑两次得到不同的报告，
「改了阈值精确率涨了 3%」这句话就没有意义了）。所以这里是一串**顺序匹配的规则**，
每一条都能说出「为什么是它」。

### 什么算冲突

   同路径 + |Δline| ≤ 3 + **同类目** + 不同 Worker + 严重度差 ≥ 2

### 「同类目」这一条是后加的，它挡掉了一个会丢发现的坑

原设计里没有它。加上它是因为：**同一位置上的不同类目根本不是矛盾的两次评估，
而是两件事。** 安全 Worker 说「第 42 行有 SQL 注入」（CRITICAL），风格 Worker
说「第 42 行的变量名不规范」（LOW）—— 两条都成立，严重度差 3 也满足原判据。
按「职责域优先」裁决的结果是风格 Worker 那条被丢掉 —— 而那是一条
**完全正确的发现**，只是因为旁边有个更严重的问题就消失了。

所以冲突的判据收窄到「同类目」：只有两条评估在谈**同一类问题**时，
严重度分歧才是一次真正的矛盾。

### 为什么必须在聚类**之前**裁决

聚类按代表选举取「严重度更高的那条」，会顺手把「两个 Worker 对严重度有分歧」
这件事**抹平**。抹平之后没有冲突可发现，而且看起来一切正常 —— 一条 CRITICAL
和一条 MEDIUM 被合成一条 CRITICAL，谁也不会问「它俩当时是不是吵过」。

另一面：一条被裁决掉的败方，**不是印证**。所以败方要在聚类之前移除，
否则它会混进胜者的 ``corroboration_count`` 里，把一次分歧算成一次互相支持。

### 四条规则，顺序匹配（第一条命中即止）

1. ``category_authority`` —— 高严重度那条的类目正好是它自己的职责域
   （``CATEGORY_OWNER``）→ **它胜**。这个领域的严重度该怎么评，归它说话。
2. ``out_of_lane_downgrade`` —— 高严重度那条**越界**了（该类目属于对方）→
   **对方胜**，越界的那条降一级。一个性能 Worker 说某处是 CRITICAL 的 SQL 注入，
   不该压过安全 Worker 说它是 LOW。
3. ``evidence_adjudication`` —— 证据落在 diff 变更行上的一方胜（``source_line_verified``，
   Worker 从真实 diff 回填的）。**这一类只在前两条都不适用时才会走到** ——
   也就是类目不在职责域表里（LLM 编了一个类目名）的时候。
4. ``unresolved`` —— 谁也不占理 → 保留严重度更高的那条，标 ``needs_human_review``，
   并把置信度压到 ``min(两条的置信度) × 0.85``。**宁可交给人看一眼，
   也不假装算出了一个答案。**
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from sfly_agent.aggregate.fingerprint import fingerprint
from sfly_shared.contracts import (
    CATEGORY_OWNER,
    SEVERITY_RANK,
    ConflictRecord,
    Finding,
    Severity,
    WorkerResult,
    WorkerType,
    normalize_path,
)

#: 严重度差达到这个数才算冲突。差 1 级是正常的判断抖动，不值得裁决 ——
#: 把 1 级差也当冲突，会把大量「一个说 high 一个说 critical」的正常分歧
#: 卷进冲突面板，面板就不再是「需要人看一眼的东西」了。
CONFLICT_SEVERITY_GAP = 2

#: 与 ``cluster.MAX_LINE_DRIFT`` **是同一个数、也是同一个理由**：
#: 相差三行以内算「同一处」。两边取不同的值会让「合并了但不冲突」
#: 和「冲突了但不合并」这两类中间状态变得无法解释。
MAX_LINE_DRIFT = 3

#: 无法裁定时给胜者的折扣。取 ``min(两条)`` 而不是 ``胜者自己`` ——
#: 分歧本身就是「有人不同意」的证据，应当反映到置信度上。
UNRESOLVED_PENALTY = 0.85

#: 严重度阶梯，用于「降一级」。从 ``SEVERITY_RANK`` 派生而不是另写一份。
_LADDER: tuple[Severity, ...] = tuple(sorted(Severity, key=lambda s: SEVERITY_RANK[s]))


@dataclass(frozen=True, slots=True)
class _Claim:
    """一条带归属的声称。``Finding`` 上没有 ``worker_type``（它属于 ``WorkerResult``），
    而这里每一处判断都要问「这是谁说的」。"""

    worker: WorkerType
    finding: Finding

    @property
    def key(self) -> str:
        return fingerprint(self.finding)


@dataclass(frozen=True, slots=True)
class ConflictOutcome:
    """冲突消解的产物 —— **内部形态，不是契约**。

    ``winner_conflicts`` 和 ``needs_human_review`` 都用**指纹**做键，而不是
    ``id()`` 或列表下标：指纹是发现本身的稳定标识，它能穿过聚类一直跟到
    合并后的代表身上（``AggregatedFinding`` 是 ``Finding`` 的子类，
    文件/类目/消息/行号都没变，所以指纹也一样）。
    """

    results: list[WorkerResult]
    records: list[ConflictRecord]
    #: 胜者指纹 → 该记在它身上的那条冲突（契约里 ``conflict`` 是单值）
    winner_conflicts: dict[str, ConflictRecord]
    #: 无法裁定的胜者指纹 —— 会被标 ``needs_human_review``
    needs_human_review: frozenset[str]


def downgrade(severity: Severity) -> Severity:
    """降一级。``info`` 已经到底，保持不变。"""
    return _LADDER[max(0, SEVERITY_RANK[severity] - 1)]


def _is_conflict(high: _Claim, low: _Claim) -> bool:
    """判据见模块文档。**先便宜的判断，最后才是严重度差。**"""
    if high.worker is low.worker:
        return False
    if normalize_path(high.finding.file) != normalize_path(low.finding.file):
        return False
    if high.finding.category != low.finding.category:
        return False
    if abs(high.finding.line - low.finding.line) > MAX_LINE_DRIFT:
        return False
    return SEVERITY_RANK[high.finding.severity] - SEVERITY_RANK[low.finding.severity] >= (
        CONFLICT_SEVERITY_GAP
    )


def _adjudicate(
    high: _Claim,
    low: _Claim,
    authority: Mapping[str, WorkerType],
) -> tuple[str, _Claim, _Claim, str, Severity | None, bool]:
    """四条规则顺序匹配。返回 ``(规则名, 胜者, 败者, 理由, 降级到, 要不要人看)``。"""
    category = high.finding.category
    owner = authority.get(category)

    if owner is high.worker:
        return (
            "category_authority",
            high,
            low,
            (
                f"{category} 属于 {high.worker.value} 的职责域，该类问题严重度以它为准；"
                f"{low.worker.value} 在相邻位置判为 {low.finding.severity.value}，"
                f"与 {high.finding.severity.value} 相差 "
                f"{SEVERITY_RANK[high.finding.severity] - SEVERITY_RANK[low.finding.severity]} 级，采纳前者。"
            ),
            None,
            False,
        )

    if owner is low.worker:
        lowered = downgrade(high.finding.severity)
        return (
            "out_of_lane_downgrade",
            low,
            high,
            (
                f"{category} 属于 {low.worker.value} 的职责域，"
                f"{high.worker.value} 是越界判定；越界的高危结论降一级"
                f"（{high.finding.severity.value} → {lowered.value}）后不再高于"
                f"{low.finding.severity.value}，采纳在职责域内的一方。"
            ),
            lowered,
            False,
        )

    verified_high = high.finding.source_line_verified
    verified_low = low.finding.source_line_verified
    if verified_high != verified_low:
        winner, loser = (high, low) if verified_high else (low, high)
        return (
            "evidence_adjudication",
            winner,
            loser,
            (
                f"类目 {category!r} 不在职责域表里（LLM 可能编了类目名）；"
                f"改由证据裁决：{winner.worker.value} 判定所在的行落在 diff 变更行上，"
                f"{loser.worker.value} 那条没有。"
            ),
            None,
            False,
        )

    return (
        "unresolved",
        high,
        low,
        (
            f"两条判定都是 {high.finding.severity.value} / {low.finding.severity.value}，"
            f"类目 {category!r} 无法归属到任何 Worker，规则引擎裁不出来。"
            f"保留较严重的一条并把置信度压到两者较低值，交给人工确认。"
        ),
        None,
        True,
    )


def resolve_conflicts(
    results: Sequence[WorkerResult],
    *,
    authority: Mapping[str, WorkerType] | None = None,
) -> ConflictOutcome:
    """把互相矛盾的上报裁定掉。返回**去掉了败方**的上报。

    ``authority`` 默认取契约层的 ``CATEGORY_OWNER``（唯一数据来源）；
    测试传一张显式的表就不必依赖规则库。传空表等于「没有职责域信息」，
    于是所有冲突都会落到第 3、4 条规则上 —— 这正是要能测到的分支。

    **败方的处理是「移除」而不是「降级保留」**：报告是给人看的，同一个位置
    挂着两条互相矛盾的评论，等于把裁决这件事推给了 PR 作者 ——
    而「集中式决策」正是这个系统的卖点。败方没有消失：它连同裁决理由
    完整地记在 ``ConflictRecord`` 里，前端有专门的冲突面板展示。
    """
    table = CATEGORY_OWNER if authority is None else authority
    claims = [
        _Claim(worker=result.worker_type, finding=finding)
        for result in results
        for finding in result.findings
    ]

    dropped: set[int] = set()
    records: list[ConflictRecord] = []
    winner_conflicts: dict[str, ConflictRecord] = {}
    needs_human_review: set[str] = set()

    # 置信度补丁：``unresolved`` 的胜者要把原始置信度压下来，而压缩必须发生在
    # 原始 ``Finding`` 上 —— ``adjusted_confidence`` 是从它算出来的，
    # 改算完的结果会被后面的重算覆盖掉。
    patched: dict[int, Finding] = {}

    # **贪心，但顺序确定。** 一对一对地看过去，已经败掉的不再参战
    # （一条被裁决掉的声称不该再去裁决别人）。按下标升序处理是确定的：
    # 三条结果的到达顺序不固定，但这里按下标而不是按到达顺序，
    # 所以同一批输入永远得到同一批记录。
    for i in range(len(claims)):
        if i in dropped:
            continue
        for j in range(i + 1, len(claims)):
            if j in dropped:
                continue
            first, second = claims[i], claims[j]
            pair = sorted(
                (first, second),
                key=lambda c: (
                    -SEVERITY_RANK[c.finding.severity],
                    c.worker.value,
                    c.finding.line,
                    c.finding.message,
                ),
            )
            high, low = pair[0], pair[1]
            if not _is_conflict(high, low):
                continue

            rule, winner, loser, rationale, _lowered, human = _adjudicate(high, low, table)
            record = ConflictRecord(
                file=normalize_path(winner.finding.file),
                line=winner.finding.line,
                winner_worker=winner.worker,
                loser_worker=loser.worker,
                winner_severity=winner.finding.severity,
                loser_severity=loser.finding.severity,
                resolution_rule=rule,
                rationale=rationale,
            )
            records.append(record)
            winner_conflicts.setdefault(winner.key, record)

            loser_index = j if loser is second else i
            dropped.add(loser_index)

            if human:
                needs_human_review.add(winner.key)
                floor = min(winner.finding.confidence, loser.finding.confidence)
                winner_index = i if winner is first else j
                patched[winner_index] = winner.finding.model_copy(
                    update={"confidence": floor * UNRESOLVED_PENALTY}
                )

            # 越界降级（``_lowered``）被降的是**败方**那条，而它已经被丢掉了，
            # 所以降级本身在报告里看不见 —— 它的作用是解释「为什么越界的高危
            # 判不过在职责域内的低危」，那句话写在 ``rationale`` 里。
            # 这里不做任何事，是刻意的：把败方降级后再保留，等于同一个位置
            # 挂两条评论，把裁决推给 PR 作者。

    survivors: list[WorkerResult] = []
    index = 0
    for result in results:
        findings: list[Finding] = []
        for finding in result.findings:
            if index not in dropped:
                findings.append(patched.get(index, finding))
            index += 1
        survivors.append(result.model_copy(update={"findings": findings}))

    return ConflictOutcome(
        results=survivors,
        records=records,
        winner_conflicts=winner_conflicts,
        needs_human_review=frozenset(needs_human_review),
    )
