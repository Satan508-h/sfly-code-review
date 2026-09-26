"""评测骨架 —— 把一份 diff 跑成一条可打分的报告，再把分数汇总成指标。

### 这里最重要的一条设计：**复用生产路径，不重写一遍**

    select_files → select_rules → WorkerRunner.review（×3）→ aggregate_run

这四个都直接调生产代码（``sfly_agent.risk`` / ``sfly_agent.rag.retriever`` /
``sfly_workers.runner`` / ``sfly_agent.aggregate.pipeline``），和编排器的
``plan`` 节点、gateway 之后的真实链路走的是同一批函数。评测里**没有任何一段
重写的审查逻辑** —— 一旦重写，「评测通过」和「系统能用」就成了两件事，
而这两件事会越漂越远，且不会有任何东西报错。

评测**不碰队列也不碰数据库**：它测的是审查质量（召回、精确、成本），
不是管道。管道由单测、集成测试和端到端脚本负责 —— 让评测去连 Redis 与
Postgres，只会让它变慢、变脆，然后在需要频繁跑它的时候被跳过。

### 两层评测，读数完全不同，不能混着讲

* **离线层（Mock LLM）**：Mock 是一个确定性正则扫描器，它的输出是固定的。
  所以这一层量的是**聚合层**（聚类/去重/置信度闸/冲突消解）对精确率和召回率的
  影响 —— 数字是硬的，能进 CI。**它不量模型的审查能力**，因为这里没有模型。
* **真实层（DeepSeek）**：量的是模型审查质量。慢、要花钱、结果有抖动。

### 匹配口径

一条ground truth 命中一条**已发布**的发现，需要：路径归一后相同 +
类目相同（严格档还要求 ``|Δline| ≤ 3``）。

**一条发现只能认领一条 ground truth**（贪心认领）。否则同一个问题报三遍
会被算成三次真阳性 —— 而「把重复报出来」正是这个系统要罚的行为，
评测口径必须跟着罚它，不然去重做得越差分数越高。
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from sfly_agent.aggregate.pipeline import aggregate_run, finalize_run
from sfly_agent.llm.base import LLMProvider
from sfly_agent.llm.pricing import estimate_cost_usd
from sfly_agent.llm.registry import build_llm
from sfly_agent.prompt import dominant_language
from sfly_agent.rag.loader import load_rules
from sfly_agent.rag.retriever import select_rules
from sfly_agent.risk import select_files
from sfly_shared.config import Settings
from sfly_shared.contracts import (
    SEVERITY_RANK,
    ReviewReport,
    Rule,
    RunRow,
    RunStatus,
    Severity,
    WorkerResult,
    WorkerType,
    normalize_path,
)
from sfly_shared.diff import parse_unified_diff
from sfly_shared.ids import new_task_id
from sfly_shared.logging import get_logger
from sfly_workers.runner import WorkerRunner, failed_result
from sfly_workers.specs import SPECS, WorkerSpec, spec_for

CASES_DIR = Path(__file__).parent / "cases"

log = get_logger(__name__)

#: 严格档的行号容忍度。**与 ``cluster.MAX_LINE_DRIFT`` 取同一个数** ——
#: 系统认为「三行以内是同一处」，评测就该按同一个尺子量，
#: 否则会出现「系统合并了、评测算它没找到」这种自相矛盾的结果。
LINE_TOLERANCE = 3

#: 评测集的三组。**干净组是多数学生完全跳过的那一组** ——
#: 缺了它，精确率的分母里只有「有问题的地方」，数字会好看得没有意义。
GROUPS = ("rebuilt", "injected", "clean")


def baseline_persona() -> str:
    """单 Agent 基线的人设：三个专家人设**逐字拼起来**，只加一段衔接说明。

    逐字拼而不是重写成一段「综合人设」：重写就引入了第二个变量，
    于是「差多少」里就分不清哪些来自拓扑、哪些来自措辞。
    基线要回答的是「同样的知识、同样的规则，一个 Agent 干三个人的活会怎样」。
    """
    return (
        "你同时负责三个方面：安全、性能、代码风格。下面是你在这三个方面的完整职责说明，"
        "你必须一次审完，并在一次输出里报出全部发现。\n\n"
        + "\n\n---\n\n".join(spec.persona for spec in SPECS.values())
    )


@dataclass(frozen=True, slots=True)
class Expected:
    """一条 ground truth：这个文件、这一行、这个类目上有一个真问题。"""

    file: str
    line: int
    category: str
    severity: Severity


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    group: str
    origin: str
    note: str
    diff_path: Path
    expected: tuple[Expected, ...]
    repo: str | None = None
    commit: str | None = None

    @property
    def diff(self) -> str:
        # ``newline=""`` 不是装饰：Windows 上默认会把 ``\n`` 翻译成 ``\r\n``，
        # 于是同一份用例在两台机器上得到不同的字节 —— 而 diff 解析器对
        # 行尾是敏感的（``_norm_line``），评测就不再可复现了。
        #
        # 用 ``open`` 而不是 ``Path.read_text``：后者不接受 ``newline``
        # （typeshed 里就没有这个参数），而且**这里必须显式关掉翻译**。
        with self.diff_path.open(encoding="utf-8", newline="") as handle:
            return handle.read()


def load_cases(cases_dir: Path | None = None) -> list[EvalCase]:
    """读全部用例。**按 id 排序** —— 报告里的顺序必须与文件系统枚举顺序无关。"""
    directory = cases_dir or CASES_DIR
    cases: list[EvalCase] = []
    for path in sorted(directory.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        expected = tuple(
            Expected(
                file=str(item["file"]),
                line=int(item["line"]),
                category=str(item["category"]),
                severity=Severity(item["severity"]),
            )
            for item in raw.get("expected") or ()
        )
        cases.append(
            EvalCase(
                id=str(raw["id"]),
                group=str(raw["group"]),
                origin=str(raw.get("origin", "handcrafted")),
                note=str(raw.get("note", "")),
                diff_path=path.with_suffix(".diff"),
                expected=expected,
                repo=raw.get("repo"),
                commit=raw.get("commit"),
            )
        )
    return cases


# --------------------------------------------------------------------------- #
# 跑一条用例
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CaseRun:
    """一条用例跑完的全部产物。"""

    case: EvalCase
    report: ReviewReport
    wall_ms: int
    #: 被置信度闸砍掉、只入库不发布的那些 —— 召回损失要能归因到这道闸上
    suppressed_expected_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0


async def review_case(
    case: EvalCase,
    *,
    settings: Settings | None = None,
    workers: Sequence[WorkerType] | None = None,
    llm: LLMProvider | None = None,
) -> CaseRun:
    """审查一份 diff 并聚合成报告。**不连队列、不连数据库。**

    ``workers`` 用来做消融（只跑一个或两个 Worker）；``llm`` 用来注入
    一个录音回放的 provider（真实层重跑时不花钱）。两者都不给就是
    默认的「三个 Worker + 配置里的 provider」。
    """
    task_id = new_task_id()
    s = settings or Settings()
    parsed = parse_unified_diff(case.diff, max_patch_chars=s.per_file_patch_chars)
    patches, truncated = select_files(parsed.patches, s.pr_max_files)
    language = dominant_language(patches)
    ruleset = load_rules()

    planned = tuple(workers) if workers is not None else tuple(WorkerType)
    started = time.perf_counter()

    results = []
    for worker_type in planned:
        spec = spec_for(worker_type)
        provider = llm or build_llm(s, worker_types=(worker_type,))
        rules = select_rules(
            ruleset,
            worker_type=worker_type,
            language=language,
            core_ids=spec.core_rule_ids,
            top_k=spec.top_k_rules,
        )
        try:
            result = await WorkerRunner(spec, provider, s).review(
                task_id=task_id,
                patches=patches,
                rules=rules,
            )
        except Exception as exc:
            # **一次传输故障不该毁掉整轮评测。** 真实层上整轮要跑半小时、
            # 要花钱，而一次 provider 超时（实测撞到过一次 120 秒不返回）
            # 会把已经跑完的全部丢掉 —— 那既贵又什么都没学到。
            #
            # 走的是生产那条路：``failed_result`` 是「失败也是结果」
            # （CLAUDE.md 约定 #2）的落点，消费循环用的是同一个函数。
            # 于是这一条在报告里表现为**降级的 run**（``missing_workers``
            # 里有它），而不是「模型没发现」——
            # 这两件事在指标上看起来一模一样，所以报告里单独有一列标出来。
            log.warning(
                "eval.worker_failed",
                case=case.id,
                worker=worker_type.value,
                error=str(exc)[:200],
            )
            result = failed_result(task_id, worker_type, exc)
        results.append(result)

    wall_ms = int((time.perf_counter() - started) * 1000)
    return _finish_run(case, results, planned, wall_ms, parsed.total_files, len(patches), truncated)


async def review_case_single_agent(
    case: EvalCase,
    *,
    settings: Settings | None = None,
    llm: LLMProvider | None = None,
) -> CaseRun:
    """**单 Agent 基线**：一次调用包办安全 / 性能 / 风格三件事。

    这是「多 Agent 到底值不值」这个问题的另一半答案。没有它，评测只能回答
    「我的系统得了多少分」；有了它才能回答「比一个人干贵多少、多找回几条」。

    与生产路径的**唯一**差别是规格和提示词，其余（选文件、检索规则、
    ``WorkerRunner``、聚合）逐字相同 —— 基线必须走同一条链路，
    否则比的就不是「一个 Agent 还是三个」，而是「两套实现」。

    基线规格**只存在于评测里**（``WorkerType`` 只有三个值，加第四个会污染
    ``CATEGORY_OWNER`` 和整条冲突消解）。它是一次对照实验，不是一种运行模式。
    """
    task_id = new_task_id()
    s = settings or Settings()
    parsed = parse_unified_diff(case.diff, max_patch_chars=s.per_file_patch_chars)
    patches, truncated = select_files(parsed.patches, s.pr_max_files)
    language = dominant_language(patches)
    ruleset = load_rules()
    spec = baseline_spec()

    # **三个 lane 的规则全给它**：单 Agent 要包办三件事，就该看到三份规则。
    # 各取各的 top_k 再拼起来，而不是把 top_k 调大 —— 后者会让它在
    # 「谁该多看几条」上和三个 Worker 的分配方式不一样，比的就不只是拓扑了。
    rules: list[Rule] = []
    for worker_type in WorkerType:
        lane = spec_for(worker_type)
        rules += select_rules(
            ruleset,
            worker_type=worker_type,
            language=language,
            core_ids=lane.core_rule_ids,
            top_k=lane.top_k_rules,
        )

    started = time.perf_counter()
    provider = llm or build_llm(s, worker_types=None)  # None = 三条 lane 都报
    try:
        result = await WorkerRunner(spec, provider, s).review(
            task_id=task_id,
            patches=patches,
            rules=rules,
        )
    except Exception as exc:
        log.warning("eval.baseline_failed", case=case.id, error=str(exc)[:200])
        result = failed_result(task_id, spec.worker_type, exc)
    wall_ms = int((time.perf_counter() - started) * 1000)
    # ``planned`` 必须**只写它自己那一个 Worker**。写成 ``tuple(WorkerType)``
    # 的话，聚合层会算出「有三个 Worker 没产出可用结果」，于是每个基线用例都被
    # 标成降级 —— 而「降级」这一列是给「有 Worker 超时/报错」用的，
    # 拿它去描述「本来就只有一个人干活」会让整列失去意义。
    # （实测踩到过：基线的降级列显示 30/30，读起来像基线整个坏了。）
    return _finish_run(
        case, [result], (spec.worker_type,), wall_ms, parsed.total_files, len(patches), truncated
    )


def baseline_spec() -> WorkerSpec:
    """把三个专家压成一个人的那份规格。见 ``review_case_single_agent``。"""
    return WorkerSpec(
        # ``worker_type`` 是占位：基线不按 lane 分，但契约要求这个字段。
        # 取 SECURITY 不改变任何行为 —— 结果里的 ``worker_type`` 只影响
        # ``totals.per_worker_ms`` 的键名，评测不读它。
        worker_type=WorkerType.SECURITY,
        consumer_group="baseline",
        stream="review_tasks",
        categories=tuple(c for s in SPECS.values() for c in s.categories),
        core_rule_ids=tuple(r for s in SPECS.values() for r in s.core_rule_ids),
        persona=baseline_persona(),
        # 基线自己拼规则（见上），这个值不会被用到 —— 留默认。
        top_k_rules=8,
    )


def _finish_run(
    case: EvalCase,
    results: list[WorkerResult],
    planned: tuple[WorkerType, ...],
    wall_ms: int,
    files_total: int,
    files_reviewed: int,
    truncated: bool,
) -> CaseRun:
    """把一批上报收成一份报告。三种跑法（多 Worker / 消融 / 基线）共用这一段。

    ``task_id`` 从上报里取而不是另传一个参数：报告和结果必须是同一个 run 的，
    多一个可以传错的参数就多一种对不上的方式，而那种错会让
    ``aggregate_run`` 拿一批别人的结果去汇总。
    """
    now = datetime.now(UTC)
    run = RunRow(
        task_id=results[0].task_id,
        idempotency_key=f"eval:{case.id}",
        repo_id="0",
        repo_node_id="R_eval",
        pr_number=0,
        head_sha="0" * 40,
        base_sha="0" * 40,
        # 状态和 deadline 都是必需的 —— 聚合时它们只被读不被改，
        # 但给一个假状态比省略字段好：契约层的字段没有默认值是有理由的。
        status=RunStatus.AGGREGATING,
        deadline_at=now + timedelta(minutes=5),
        planned_workers=list(planned),
        files_total=files_total,
        files_reviewed=files_reviewed,
        diff_truncated=truncated,
        created_at=now,
        dispatched_at=now,
    )
    # 成本在这里算，**不是**让 ``aggregate_run`` 去查库：评测不连数据库。
    # 生产链路上这笔账来自 ``llm_calls`` 表（Worker 写结果时记的），
    # 而评测手上只有 ``WorkerResult`` 里的 token 计数 —— 于是复用同一张价目表
    # （``pricing.estimate_cost_usd``），口径和生产完全一致。
    # **Mock 的单价是 0，那不是「没算」**，见 pricing.py 的模块文档第 3 条。
    cost_usd = sum(
        estimate_cost_usd(
            result.model,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cached_tokens=result.cached_tokens,
        )
        for result in results
    )
    report = finalize_run(aggregate_run(run, results, cost_usd=cost_usd, now=now))

    return CaseRun(
        case=case,
        report=report,
        wall_ms=wall_ms,
        tokens_in=sum(r.tokens_in for r in results),
        tokens_out=sum(r.tokens_out for r in results),
        cached_tokens=sum(r.cached_tokens for r in results),
        cost_usd=cost_usd,
    )


def budget_from_env(default: float = 5.0) -> float:
    """真实层的花费上限，读 ``EVAL_BUDGET_USD``。

    上限**必须存在且必须真的会拦**：一个没有上限的评测脚本迟早会在
    某次「就再跑一遍」里花掉一个不打算花的数，而那时没有东西会拦它。
    """
    raw = os.environ.get("EVAL_BUDGET_USD", "")
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def run_all(
    cases: Iterable[EvalCase],
    *,
    settings: Settings | None = None,
    workers: Sequence[WorkerType] | None = None,
    budget_usd: float | None = None,
) -> list[CaseRun]:
    """顺序跑完一批用例。

    **故意串行**：并发会让 p50/p95 延迟失去意义，也会让真实层的限流
    变成一串重试 —— 而重试会污染 token 统计。评测慢一点没关系，
    数字被污染才是问题。

    ``budget_usd`` 给定时**边跑边记花费**，超了就停在当前用例上并返回
    已经跑完的部分。停在哪比「跑到一半被 provider 拒绝」好：
    前者是报告里写明的一行，后者是一串看不出原因的报错。
    """
    logger = get_logger(__name__)
    pending = list(cases)

    async def _main() -> list[CaseRun]:
        runs: list[CaseRun] = []
        spent = 0.0
        for case in pending:
            run = await review_case(case, settings=settings, workers=workers)
            runs.append(run)
            spent += run.cost_usd
            if budget_usd is not None and spent >= budget_usd:
                logger.warning(
                    "eval.budget_exhausted",
                    spent=round(spent, 4),
                    budget=budget_usd,
                    done=len(runs),
                    total=len(pending),
                )
                break
        return runs

    return asyncio.run(_main())


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #


def _hits(expected: Expected, findings: Sequence[Any], *, strict: bool) -> int:
    """``expected`` 被几条发现命中。**只看有没有，不看几条** —— 认领在 ``score`` 里做。"""
    return sum(1 for f in findings if _matches(expected, f, strict=strict))


def _matches(expected: Expected, finding: Any, *, strict: bool) -> bool:
    if normalize_path(expected.file) != normalize_path(finding.file):
        return False
    if expected.category != finding.category:
        return False
    return not strict or abs(expected.line - finding.line) <= LINE_TOLERANCE


@dataclass(slots=True)
class Counts:
    """混淆矩阵的一档（严格或宽松）。"""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    severity_exact: int = 0

    def add(self, other: Counts) -> None:
        """累加另一份计数（逐用例汇总用）。"""
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn
        self.severity_exact += other.severity_exact

    @property
    def precision(self) -> float:
        total = self.tp + self.fp
        return self.tp / total if total else 0.0

    @property
    def recall(self) -> float:
        total = self.tp + self.fn
        return self.tp / total if total else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass(slots=True)
class Metrics:
    runs: int = 0
    depth: int = 3
    caches_hit: float = 0.0

    published: int = 0
    suppressed: int = 0
    conflicts: int = 0
    strict: Counts = field(default_factory=Counts)
    loose: Counts = field(default_factory=Counts)

    #: 干净组上发布的发现数 —— 这就是误报率的分子，分母是干净用例数
    clean_cases: int = 0
    clean_findings: int = 0
    #: 被置信度闸砍掉、但其实命中了 ground truth 的条数 —— 召回损失归因
    suppressed_hits: int = 0
    #: **有 Worker 没产出可用结果的用例数**（超时、报错）。
    #: 单独记是因为它在指标上和「模型没发现」长得一模一样 ——
    #: 不标出来的话，一次 provider 超时会被读成一次漏报。
    degraded_cases: int = 0

    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    latencies: list[int] = field(default_factory=list)

    @property
    def false_positive_rate(self) -> float:
        """干净组上每个 PR 平均报出几条。**这个数不是零就说明系统在发明问题。**"""
        return self.clean_findings / self.clean_cases if self.clean_cases else 0.0

    @property
    def cost_per_case(self) -> float:
        return self.cost_usd / self.runs if self.runs else 0.0

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.tokens_in if self.tokens_in else 0.0

    def percentile(self, pct: float) -> int:
        if not self.latencies:
            return 0
        ordered = sorted(self.latencies)
        index = min(len(ordered) - 1, round(pct / 100 * (len(ordered) - 1)))
        return ordered[index]


@dataclass(slots=True)
class Tally:
    strict: Counts
    loose: Counts
    #: 严格档下没认领到任何 ground truth 的那些**发现本身**。
    #: 报告要能把它们印出来 —— 见 ``render_report`` 的「假阳性明细」一节。
    false_positives: list[Any]


def _tally(case: EvalCase, published: Sequence[Any]) -> Tally:
    """按一份**已发布的发现集合**算出两档混淆矩阵。

    **贪心认领**：每条发现最多认领一条 ground truth。不给这一步的话，
    同一个问题报三遍会被算成三次真阳性 —— 于是去重做得越差，精确率越高。
    """
    strict, loose = Counts(), Counts()
    false_positives: list[Any] = []
    for is_strict, counts in ((True, strict), (False, loose)):
        unclaimed = list(case.expected)
        for finding in published:
            for index, expected in enumerate(unclaimed):
                if _matches(expected, finding, strict=is_strict):
                    counts.tp += 1
                    if finding.severity is expected.severity:
                        counts.severity_exact += 1
                    unclaimed.pop(index)
                    break
            else:
                counts.fp += 1
                if is_strict:
                    false_positives.append(finding)
        counts.fn += len(unclaimed)
    return Tally(strict=strict, loose=loose, false_positives=false_positives)


def all_merged(run: CaseRun) -> list[Any]:
    """一次 run 里**全部**合并后的发现。

    ``findings`` 和 ``suppressed`` 是同一批发现被置信度闸切开的两半，
    合起来才是完整的合并结果。阈值扫描要用完整的那一份 ——
    只拿发布出去的那半，扫描出来的曲线是假的。
    """
    return [*run.report.findings, *run.report.suppressed]


def score_by_group(runs: Sequence[CaseRun]) -> dict[str, Metrics]:
    """按组分别打分。

    **这一层是必须的，不是锦上添花。** 三个组量的是不同的东西：

    * ``injected`` —— 扫描器认得出的注入缺陷。离线层真正量的就是这一组。
    * ``rebuilt`` —— 真实 CVE 修复回退出来的。**Mock 基本认不出它们**
      （它们是真代码，不是为正则准备的），所以离线层在这一组上的低分
      说明的是扫描器弱，不是聚合层差。这一组的分数只有真实层才有意义。
    * ``clean`` —— 误报。

    把三组混在一个数字里，得到的既不是「聚合层有多好」也不是「模型有多好」,
    而是一个取决于用例配比的数 —— 那种数字面试官一问就散。
    """
    grouped: dict[str, list[CaseRun]] = {}
    for run in runs:
        grouped.setdefault(run.case.group, []).append(run)
    return {group: score(group_runs) for group, group_runs in grouped.items()}


def score(runs: Sequence[CaseRun]) -> Metrics:
    """把一批跑完的用例汇总成指标。**纯函数**（除了读 ``CaseRun``）。"""
    m = Metrics(runs=len(runs))
    for run in runs:
        published = run.report.findings
        m.published += len(published)
        m.suppressed += len(run.report.suppressed)
        m.conflicts += len(run.report.conflicts)
        m.cost_usd += run.cost_usd
        m.tokens_in += run.tokens_in
        m.tokens_out += run.tokens_out
        m.cached_tokens += run.cached_tokens
        m.latencies.append(run.wall_ms)
        if run.report.degraded:
            m.degraded_cases += 1
        if run.case.group == "clean":
            m.clean_cases += 1
            m.clean_findings += len(published)

        tally = _tally(run.case, published)
        m.strict.add(tally.strict)
        m.loose.add(tally.loose)

        for suppressed in run.report.suppressed:
            if any(_matches(e, suppressed, strict=False) for e in run.case.expected):
                m.suppressed_hits += 1

    return m


# --------------------------------------------------------------------------- #
# 阈值扫描 —— 评测集存在的**主要**理由
# --------------------------------------------------------------------------- #


def threshold_sweep(runs: Sequence[CaseRun], thresholds: Sequence[float]) -> list[SweepRow]:
    """把置信度闸按不同阈值重切一遍，看精确率/召回率怎么变。

    只用报告里已有的数据，所以**不需要重跑审查** —— 这一点是刻意的：
    真实层跑一轮要花钱、要等，而调一个阈值不应该再付一次那笔钱。
    （代价是扫描无法反映「阈值变了、模型的行为也跟着变」这种情况 ——
    但阈值只影响发布，不影响模型，所以这里安全。）

    ``needs_human_review`` 的条目照旧豁免（见 ``pipeline.split_by_confidence``）。
    """
    rows: list[SweepRow] = []
    for threshold in thresholds:
        published_total = 0
        clean_findings = 0
        clean_cases = 0
        strict, loose = Counts(), Counts()
        for run in runs:
            kept = [f for f in all_merged(run) if f.needs_human_review or f.adjusted_confidence >= threshold]
            published_total += len(kept)
            if run.case.group == "clean":
                clean_cases += 1
                clean_findings += len(kept)
            run_tally = _tally(run.case, kept)
            strict.add(run_tally.strict)
            loose.add(run_tally.loose)
        rows.append(
            SweepRow(
                threshold=threshold,
                published=published_total,
                strict=strict,
                loose=loose,
                clean_findings=clean_findings,
                clean_cases=clean_cases,
            )
        )
    return rows


@dataclass(slots=True)
class Variant:
    """一次消融配置的跑分。``calls`` 是**总 LLM 调用次数**（用例数 × 每条几个 Worker）。"""

    label: str
    metrics: Metrics
    calls: int


def render_ablation(variants: Sequence[Variant]) -> list[str]:
    """消融与基线的对照表。返回 Markdown 行。

    **这张表回答的是「多 Agent 到底值不值」**，而那需要两样东西才算回答完整：
    一个单 Agent 基线（不然只有「我的系统多少分」），
    和一条增量曲线（不然不知道第三个 Worker 是不是白花钱）。

    「每多发现一个真问题的边际成本」只在**相邻两档之间**才有定义 ——
    拿它去和基线比是没有意义的，基线的「增量」不是从零开始的。
    """
    if not variants:
        return []

    lines = [
        "## 消融与基线",
        "",
        "| 配置 | LLM 调用 | 发布 | 命中真问题 | 严格召回率 | 精确率 | 降级用例 | 成本 | 相对上一档 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for index, variant in enumerate(variants):
        m = variant.metrics
        if index == 0:
            delta = "—（基线）"
        else:
            previous = variants[index - 1]
            gained = m.strict.tp - previous.metrics.strict.tp
            spent = m.cost_usd - previous.metrics.cost_usd
            if gained > 0:
                delta = f"+{gained} 条，每多一条 ${spent / gained:.4f}"
            elif spent > 0:
                delta = f"**+0 条，多花 ${spent:.4f}**"
            else:
                delta = "+0 条，$0"
        # **降级用例数必须在这一行里。** 一次 provider 超时会让某个 Worker
        # 交不出结果，那一档的召回率于是偏低 —— 而它和「那个 Worker 没找到」
        # 在指标上完全一样。真实层实测撞到过一次（2 Worker 阶段 performance
        # 超时），当时这一列还不存在。
        degraded = f"⚠️ {m.degraded_cases}" if m.degraded_cases else "0"
        lines.append(
            f"| {variant.label} | {variant.calls} | {m.published} | {m.strict.tp} | "
            f"{m.strict.recall * 100:.1f}% | {m.strict.precision * 100:.1f}% | "
            f"{degraded} | ${m.cost_usd:.4f} | {delta} |"
        )

    lines += [
        "",
        "> **「+0 条」不等于那一档没用。** 召回率只看 ground truth 命中，"
        "而增量 Worker 常常发现的是没被标注的真问题（评测集标不完）。"
        "所以这一列真正能下结论的是**成本**那一半：花钱买不到标注里的召回时，"
        "该问的是「它报出来的那些是不是真的」，那要用发布的原始条目去人工抽查 ——"
        "自动化指标到这里就到头了。",
        "",
    ]
    return lines


@dataclass(slots=True)
class SweepRow:
    threshold: float
    published: int
    strict: Counts
    loose: Counts
    clean_findings: int
    clean_cases: int

    @property
    def false_positive_rate(self) -> float:
        return self.clean_findings / self.clean_cases if self.clean_cases else 0.0


# --------------------------------------------------------------------------- #
# 出报告
# --------------------------------------------------------------------------- #


def render_report(
    metrics: Metrics,
    runs: Sequence[CaseRun],
    *,
    title: str,
    meta: Sequence[str],
    sweep: Sequence[SweepRow] = (),
    current_threshold: float | None = None,
    by_group: dict[str, Metrics] | None = None,
) -> str:
    """Markdown 报告。**人读的第一份产物**，所以数字要带单位和口径。"""

    def pct(value: float) -> str:
        return f"{value * 100:.1f}%"

    # 逐条列出没认领到 ground truth 的发现。**报告里必须有这一节** ——
    # 精确率是唯一一个「低下去之后无法靠自动化指标继续解释」的数字，
    # 再往下只能逐条读它们。不印出来，那个动作就没有入口。
    false_positives: list[tuple[str, Any]] = [
        (run.case.id, finding)
        for run in runs
        for finding in _tally(run.case, run.report.findings).false_positives
    ]

    # meta 里的一条**空串表示分段**：它之前的条目是项目符号，之后的是正文段。
    # 直接产出一行空的 ``- `` 会看起来像「有一项忘了填」。
    bullets: list[str] = []
    blurb: list[str] = []
    target = bullets
    for line in meta:
        if line:
            target.append(line)
        else:
            target = blurb

    lines = [
        f"# {title}",
        "",
        *[f"- {line}" for line in bullets],
        "",
        *blurb,
        "",
        "## 总览",
        "",
        "| 指标 | 严格档 | 宽松档 |",
        "|---|---|---|",
        f"| 真阳性 | {metrics.strict.tp} | {metrics.loose.tp} |",
        f"| 假阳性 | {metrics.strict.fp} | {metrics.loose.fp} |",
        f"| 漏报 | {metrics.strict.fn} | {metrics.loose.fn} |",
        f"| **精确率** | **{pct(metrics.strict.precision)}** | {pct(metrics.loose.precision)} |",
        f"| **召回率** | **{pct(metrics.strict.recall)}** | {pct(metrics.loose.recall)} |",
        f"| F1 | {metrics.strict.f1:.3f} | {metrics.loose.f1:.3f} |",
        f"| 严重度判对 | {metrics.strict.severity_exact} | — |",
        "",
        "> 严格档 = 文件 + 类目 + 行号（±3 行内）全对；宽松档 = 文件 + 类目对，不看行号。",
        "> 差距说明的是「定位准不准」，而不是「有没有找到」。",
        ">",
        "> **上面这个总数只能当索引看** —— 三组量的是不同的东西，"
        "混在一起的数字取决于用例配比。分组数字在下一节。",
        "",
    ]

    if by_group:
        lines += [
            "## 分组指标（**这才是有意义的那些数**）",
            "",
            "| 组 | 用例 | 期望 | 发布 | 严格精确率 | 严格召回率 | 被砍的真阳性 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for group, gm in by_group.items():
            expected = sum(len(r.case.expected) for r in runs if r.case.group == group)
            lines.append(
                f"| `{group}` | {gm.runs} | {expected} | {gm.published} | "
                f"{pct(gm.strict.precision)} | {pct(gm.strict.recall)} | {gm.suppressed_hits} |"
            )
        lines += [
            "",
            "读法（每一组的数字说明什么，差别很大）：",
            "",
            "* `injected` —— **离线层真正量的就是这一组**。用例是照着扫描器认得出的写法"
            "准备的，所以这里量的是**聚合层**（去重、置信度闸、冲突消解）对精确率和"
            "召回率的影响。",
            "* `rebuilt` —— 真实 CVE 修复回退出来的代码。**Mock 基本认不出它们**，"
            "因为它们是真代码，不是为正则准备的。这一组的低分说明的是**扫描器弱**，"
            "不是聚合层差；它的意义只有真实层才发挥得出来。",
            "* `clean` —— 误报。见下一节。",
            "",
        ]

    lines += [
        "## 误报与召回损失",
        "",
        f"- 干净组：{metrics.clean_cases} 个用例，共发布 **{metrics.clean_findings}** 条发现"
        f" → 每个干净 PR 平均 {metrics.false_positive_rate:.2f} 条",
        f"- 被置信度闸砍掉、但确实命中 ground truth：**{metrics.suppressed_hits}** 条"
        "（这就是那道闸的召回代价；它只入库不发布，所以不在上面几个数里）",
        f"- 冲突裁决：{metrics.conflicts} 次",
        "",
    ]

    if false_positives:
        lines += [
            "### 假阳性明细（**前 20 条**）",
            "",
            "这些是严格档下没认领到 ground truth 的发现 —— 而**它们不一定是错的**。",
            "评测集永远标不完：一条真的、但没被标注的问题，在这里也长得和假阳性一样。",
            "所以这一节不是装饰，它是「自动化指标到头了」那个位置的入口 ——",
            "精确率低的时候，**唯一能继续走的一步是逐条读它们**，然后回答一个问题：",
            "「这条如果出现在我的 PR 上，我愿不愿意看到它？」",
            "",
            "| # | 用例 | 位置 | 严重度 | 说了什么 |",
            "|---:|---|---|---|---|",
        ]
        for index, (case_id, finding) in enumerate(false_positives[:20], start=1):
            lines.append(
                f"| {index} | `{case_id}` | `{finding.file}:{finding.line}` | "
                f"{finding.severity.value} | {escape_cell(finding.message[:96])} |"
            )
        if len(false_positives) > 20:
            lines.append(f"| … | | | | 另有 {len(false_positives) - 20} 条 |")
        lines += [""]

    lines += ["## 成本与延迟", ""]

    if sweep:
        lines += [
            "## 置信度闸的阈值扫描",
            "",
            "置信度门槛是**唯一一个纯策略数字**（其余都是量出来的），所以它不该靠猜。",
            "下表把同一批发现按不同阈值重切一遍 —— 不需要重跑审查，因为阈值只影响发布、不影响模型。",
            "",
            "| 阈值 | 发布条数 | 严格精确率 | 严格召回率 | F1 | 干净组每条 |",
            "|---:|---:|---:|---:|---:|---:|",
        ]
        for row in sweep:
            marker = (
                " **←当前**"
                if current_threshold is not None and abs(row.threshold - current_threshold) < 1e-9
                else ""
            )
            lines.append(
                f"| {row.threshold:.2f}{marker} | {row.published} | "
                f"{pct(row.strict.precision)} | {pct(row.strict.recall)} | "
                f"{row.strict.f1:.3f} | {row.false_positive_rate:.2f} |"
            )
        lines += [
            "",
            "> 读法：阈值调低会同时拉高召回和误报。**没有免费的档位** ——"
            "选哪一档取决于「漏掉一个真问题」和「多报一个假问题」哪个更贵，"
            "而那是产品决策，不是技术决策。",
            "",
        ]

    lines += [
        "## 成本与延迟",
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 用例数 | {metrics.runs} |",
        f"| Worker 数 | {metrics.depth} |",
        f"| 总成本 | ${metrics.cost_usd:.4f} |",
        f"| 每 PR 成本 | ${metrics.cost_per_case:.4f} |",
        f"| 输入 / 输出 token | {metrics.tokens_in} / {metrics.tokens_out} |",
        f"| 缓存命中率 | {pct(metrics.cache_hit_rate)} |",
        f"| 延迟 p50 / p95 | {metrics.percentile(50)} ms / {metrics.percentile(95)} ms |",
        "",
        "## 逐用例",
        "",
        "| 用例 | 组 | 期望 | 发布 | 被砍 | 严格命中 | 假阳 | 冲突 | 降级 | 耗时 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|---:|",
    ]
    for run in runs:
        per = score([run])
        lines.append(
            f"| `{run.case.id}` | {run.case.group} | {len(run.case.expected)} | "
            f"{len(run.report.findings)} | {len(run.report.suppressed)} | "
            f"{per.strict.tp} | {per.strict.fp} | "
            f"{len(run.report.conflicts)} | "
            f"{'⚠️ ' + ','.join(w.value for w in run.report.missing_workers) if run.report.degraded else ''} | "
            f"{run.wall_ms} ms |"
        )

    lines += ["", "## 用例说明", ""]
    for run in runs:
        if run.case.repo and run.case.commit:
            origin = f"，来源 `{run.case.repo}@{run.case.commit[:8]}`"
        elif run.case.repo:
            origin = f"，来源 `{run.case.repo}`"
        else:
            origin = ""
        lines.append(f"- `{run.case.id}`（{run.case.group}{origin}）：{run.case.note}")

    return "\n".join(lines).rstrip() + "\n"


def escape_cell(text: str) -> str:
    """Markdown 表格里 ``|`` 会把列切断。用例说明是人写的，随手就可能带上。"""
    return text.replace("|", "\\|")


def severity_order() -> list[Severity]:
    """严重度从高到低 —— 报告里的排序用它，而不是枚举的定义顺序。"""
    return sorted(Severity, key=lambda s: SEVERITY_RANK[s], reverse=True)
