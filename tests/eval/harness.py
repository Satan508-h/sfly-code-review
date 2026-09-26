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
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from sfly_agent.aggregate.pipeline import aggregate_run, finalize_run
from sfly_agent.llm.base import LLMProvider
from sfly_agent.llm.registry import build_llm
from sfly_agent.prompt import dominant_language
from sfly_agent.rag.loader import load_rules
from sfly_agent.rag.retriever import select_rules
from sfly_agent.risk import select_files
from sfly_shared.config import Settings
from sfly_shared.contracts import (
    SEVERITY_RANK,
    ReviewReport,
    RunRow,
    RunStatus,
    Severity,
    WorkerType,
    normalize_path,
)
from sfly_shared.diff import parse_unified_diff
from sfly_shared.ids import new_task_id
from sfly_workers.runner import WorkerRunner
from sfly_workers.specs import spec_for

CASES_DIR = Path(__file__).parent / "cases"

#: 严格档的行号容忍度。**与 ``cluster.MAX_LINE_DRIFT`` 取同一个数** ——
#: 系统认为「三行以内是同一处」，评测就该按同一个尺子量，
#: 否则会出现「系统合并了、评测算它没找到」这种自相矛盾的结果。
LINE_TOLERANCE = 3

#: 评测集的三组。**干净组是多数学生完全跳过的那一组** ——
#: 缺了它，精确率的分母里只有「有问题的地方」，数字会好看得没有意义。
GROUPS = ("rebuilt", "injected", "clean")


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
        result = await WorkerRunner(spec, provider, s).review(
            task_id=task_id,
            patches=patches,
            rules=rules,
        )
        results.append(result)

    wall_ms = int((time.perf_counter() - started) * 1000)

    now = datetime.now(UTC)
    run = RunRow(
        task_id=task_id,
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
        files_total=parsed.total_files,
        files_reviewed=len(patches),
        diff_truncated=truncated,
        created_at=now,
        dispatched_at=now,
    )
    report = finalize_run(aggregate_run(run, results, now=now))

    return CaseRun(
        case=case,
        report=report,
        wall_ms=wall_ms,
        tokens_in=sum(r.tokens_in for r in results),
        tokens_out=sum(r.tokens_out for r in results),
        cached_tokens=sum(r.cached_tokens for r in results),
        cost_usd=report.totals.cost_usd,
    )


def run_all(
    cases: Iterable[EvalCase],
    *,
    settings: Settings | None = None,
    workers: Sequence[WorkerType] | None = None,
) -> list[CaseRun]:
    """顺序跑完一批用例。

    **故意串行**：并发会让 p50/p95 延迟失去意义，也会让真实层的限流
    变成一串重试 —— 而重试会污染 token 统计。评测慢一点没关系，
    数字被污染才是问题。
    """

    async def _main() -> list[CaseRun]:
        return [await review_case(case, settings=settings, workers=workers) for case in cases]

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


def _tally(case: EvalCase, published: Sequence[Any]) -> tuple[Counts, Counts]:
    """按一份**已发布的发现集合**算出两档混淆矩阵。

    **贪心认领**：每条发现最多认领一条 ground truth。不给这一步的话，
    同一个问题报三遍会被算成三次真阳性 —— 于是去重做得越差，精确率越高。
    """
    strict, loose = Counts(), Counts()
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
        counts.fn += len(unclaimed)
    return strict, loose


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
        if run.case.group == "clean":
            m.clean_cases += 1
            m.clean_findings += len(published)

        m.strict.add(_tally(run.case, published)[0])
        m.loose.add(_tally(run.case, published)[1])

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
            run_strict, run_loose = _tally(run.case, kept)
            strict.add(run_strict)
            loose.add(run_loose)
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
        "| 用例 | 组 | 期望 | 发布 | 被砍 | 严格命中 | 假阳 | 冲突 | 耗时 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for run in runs:
        per = score([run])
        lines.append(
            f"| `{run.case.id}` | {run.case.group} | {len(run.case.expected)} | "
            f"{len(run.report.findings)} | {len(run.report.suppressed)} | "
            f"{per.strict.tp} | {per.strict.fp} | "
            f"{len(run.report.conflicts)} | {run.wall_ms} ms |"
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
