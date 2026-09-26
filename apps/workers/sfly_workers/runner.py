"""WorkerRunner —— 一次审查的完整过程，从 diff 到 ``WorkerResult``。

**这个类不知道队列、不知道 Redis、不知道 Postgres。** 它拿到文件补丁和规则，
返回一条结果。M3 的消费循环负责在它外面套上「先落库、再发布、最后 ack」的
顺序（CLAUDE.md 约定 #1），M5 的编排器负责把它的结果收进屏障。

这样切分的直接好处：``python -m sfly_workers --spec security --diff x.diff``
能以完全相同的方式跑同一个对象，一行都不用改。调试提示词时这个入口比走
完整链路快一个数量级。
"""

from __future__ import annotations

import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from functools import lru_cache

from sfly_agent.llm.base import LLMProvider
from sfly_agent.llm.structured import complete_structured
from sfly_agent.prompt import build_system_prompt, build_user_prompt, dominant_language
from sfly_agent.rag.loader import load_rules
from sfly_shared.config import Settings, get_settings
from sfly_shared.contracts import (
    ErrorClass,
    FilePatch,
    Finding,
    ResultStatus,
    Rule,
    WorkerResult,
    WorkerType,
    normalize_path,
)
from sfly_shared.logging import bind_task, get_logger
from sfly_workers.specs import WorkerSpec

log = get_logger(__name__)


@lru_cache(maxsize=1)
def library_rule_ids() -> frozenset[str]:
    """**规则库**里全部合法的规则 id。

    这是「模型有没有编造 rule_id」的唯一正确参照物，理由见
    :func:`reconcile_findings` 的文档第 3 条。

    读的是随包发布的 YAML 数据文件（``load_rules`` 自己也带缓存），
    **不是检索**：Worker 依然不做 BM25 排序、不挑规则 —— 那些仍然由编排层
    的 ``plan`` 节点做完再随任务发下来。这里只是要一份「哪些 id 是合法的」清单。
    """
    return frozenset(rule.id for rule in load_rules().rules)


@dataclass(slots=True)
class ReconcileReport:
    """回填与丢弃的统计。评测要按丢弃原因分类，所以不能只留一个总数。"""

    kept: list[Finding]
    file_mismatch: list[str]
    unverified_lines: int
    hallucinated_rules: int

    @property
    def dropped(self) -> int:
        return len(self.file_mismatch)


def reconcile_findings(
    findings: Sequence[Finding],
    patches: Sequence[FilePatch],
    library_ids: Collection[str],
) -> ReconcileReport:
    """把模型的输出对齐到**真实的输入**上。

    纯函数，不碰网络也不碰数据库 —— 它是 Worker 里第二值得写测试的地方
    （第一是修复阶梯）。模型报出来的东西有三类不可信，逐类处理：

    1. **文件路径**：幻觉路径是结构错误里最常见的一种，而且**无法发布** ——
       GitHub 上没有这个文件，评论无处可挂。所以按归一化路径对齐；
       对不上但有唯一同名文件时接受（模型常把 ``src/a.py`` 写成 ``a.py``）；
       再对不上就丢弃，并记下原因。
    2. **行号**：不在变更行上不是错误（模型可能指向了上下文行），但必须
       **记下来** —— 发布阶段要把这类降级成文件级评论，因为 GitHub 会
       422 拒绝锚定在未变更行上的 inline 评论。
    3. **规则 id**：编造的 rule_id 会让这条 finding 拿到 ``grounded`` 的
       置信度加成。加成必须只给真正命中规则库的条目，否则置信度公式就废了。

       ``library_ids`` 的参数名与类型在这里很关键，它曾经是「本次检索到的规则」
       （``Sequence[Rule]``），而那是个**静默吃掉真阳性的 bug**：

       ``grounded`` 的文档说的是「命中**规则库**里的某一条」，而检索每轮只挑
       ``top_k`` 条 —— 两个集合差了十几倍。模型引用了一条真实存在、只是没被检索
       到的规则时，它既不是幻觉、也不该失去加成，但旧实现会把它清掉；
       少了那 ``+0.10``，一条真阳性就掉到 ``SUPPRESS_THRESHOLD`` 以下被砍掉，
       **而它不会出现在任何地方**。

       M9 的离线评测量到了这件事：改按规则库校验之后，注入组的召回率
       78.3% → 82.6%，重建组从一个真实 CVE 里找回了那条 SSRF 发现
       （精确率相应从 100% 降到 90.5% —— 多出来的那条是假阳性）。
    """
    canonical = {normalize_path(p.path): p.path for p in patches}
    changed = {p.path: set(p.changed_lines) for p in patches}

    by_basename: dict[str, list[str]] = {}
    for p in patches:
        by_basename.setdefault(p.path.rsplit("/", 1)[-1].lower(), []).append(p.path)

    kept: list[Finding] = []
    mismatches: list[str] = []
    unverified = 0
    fake_rules = 0

    for finding in findings:
        path = canonical.get(normalize_path(finding.file))
        if path is None:
            # 退一步：文件名唯一时接受。要求唯一是为了不把 a.py 的意见
            # 安到 b/a.py 上 —— 那比丢弃更糟，因为它是**错的**而不是缺的。
            same_name = by_basename.get(finding.file.rsplit("/", 1)[-1].lower(), [])
            if len(same_name) == 1:
                path = same_name[0]
            else:
                mismatches.append(finding.file)
                continue

        finding.file = path
        finding.source_line_verified = finding.line in changed.get(path, set())
        if not finding.source_line_verified:
            unverified += 1

        if finding.rule_id is not None and finding.rule_id not in library_ids:
            fake_rules += 1
            finding.rule_id = None

        kept.append(finding)

    return ReconcileReport(
        kept=kept,
        file_mismatch=mismatches,
        unverified_lines=unverified,
        hallucinated_rules=fake_rules,
    )


class WorkerRunner:
    """一个 Worker 的全部业务逻辑。三种部署共用这一个类。"""

    def __init__(self, spec: WorkerSpec, llm: LLMProvider, settings: Settings | None = None) -> None:
        self.spec = spec
        self.llm = llm
        self.settings = settings or get_settings()

    async def review(
        self,
        *,
        task_id: str,
        patches: Sequence[FilePatch],
        rules: Sequence[Rule] = (),
        attempt: int = 1,
    ) -> WorkerResult:
        """审查一批文件补丁，返回一条可以落库的结果。

        **传输层故障会原样抛出**（超时、HTTP 错误）。那类错误的重试决策属于
        消费循环（它知道 attempt 和死信规则），不属于这里。而**解析失败不抛** ——
        它以 ``status="failed"`` 的结果返回，因为「模型返回了没法解析的东西」
        是一个必须记录、必须闭合屏障的业务结果。

        注意最后这一条的重要性：如果解析失败直接抛异常而不发结果，
        ``wait`` 节点的屏障永远不会闭合，整个 run 会挂到超时 ——
        CLAUDE.md 约定 #2「失败也是结果」说的就是这件事。
        """
        bind_task(task_id, self.spec.name)
        started = time.perf_counter()

        system = build_system_prompt(self.spec.persona)
        user = build_user_prompt(patches=patches, rules=rules, language=dominant_language(patches))

        outcome = await complete_structured(
            self.llm,
            system=system,
            user=user,
            item_type=Finding,
            max_tokens=self.settings.llm_max_tokens,
            max_repairs=self.settings.llm_max_repairs,
        )

        if not outcome.ok:
            log.warning(
                "worker.schema_unrecoverable",
                level=outcome.level,
                repairs=outcome.repairs,
                error=outcome.error,
            )
            return WorkerResult(
                task_id=task_id,
                worker_type=self.spec.worker_type,
                status=ResultStatus.FAILED,
                error=outcome.error,
                error_class=ErrorClass.SCHEMA_UNRECOVERABLE,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                cached_tokens=outcome.cached_tokens,
                latency_ms=_elapsed_ms(started),
                model=outcome.model,
                raw_response=outcome.raw or None,
                attempt=attempt,
            )

        report = reconcile_findings(outcome.items, patches, library_rule_ids())
        dropped = outcome.dropped + report.dropped
        status = ResultStatus.PARTIAL if dropped else ResultStatus.OK

        log.info(
            "worker.reviewed",
            status=status.value,
            findings=len(report.kept),
            dropped=dropped,
            level=outcome.level,
            repairs=outcome.repairs,
            unverified_lines=report.unverified_lines,
            latency_ms=outcome.latency_ms,
        )

        return WorkerResult(
            task_id=task_id,
            worker_type=self.spec.worker_type,
            status=status,
            findings=report.kept,
            # error 字段留空：status=partial 表示「部分条目被丢弃」，
            # 不是「这次审查失败」。往这里塞一段说明会让下游的
            # 「有 error 就是出问题了」判断误伤。
            tokens_in=outcome.tokens_in,
            tokens_out=outcome.tokens_out,
            cached_tokens=outcome.cached_tokens,
            latency_ms=_elapsed_ms(started),
            model=outcome.model,
            dropped_findings=dropped,
            attempt=attempt,
        )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def worker_types_of(spec: WorkerSpec) -> tuple[WorkerType, ...]:
    """给 ``build_llm`` 用的 lane。单独一个函数是为了让调用点读起来有意图。"""
    return (spec.worker_type,)
