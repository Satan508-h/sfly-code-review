"""``python -m sfly_workers --spec {security|performance|style}``

一套代码、三种部署。三个容器跑的是**同一个镜像同一个入口**，
只靠 ``--spec`` 区分消费者组和人设 —— 见 ``specs.py``。

两种运行形态：

* **独立 CLI**（``--diff fixtures/x.diff``）：不接队列、不起容器、不连数据库，
  对着一份 diff 跑单个 Worker 然后打印结果。调试提示词时它比走完整链路
  快一个数量级，而且**格式化输出到 stdout、人读的摘要到 stderr**，
  所以 ``... | jq '.findings[]'`` 直接用。
* **常驻消费**（默认）：接队列跑消费协程，顺序严格是「存库 → 发结果 → ack」。
  实现全在 ``pool.py`` —— 那边的消费循环被两个宿主共用（一个容器一条 lane，
  或者三条协程一个进程），这个文件只负责解析参数和装配依赖。

退出码：``0`` 有结果（含 partial）／``2`` 审查失败（解析不出 JSON）／
``3`` 输入有问题（文件不存在、里面没有 diff）。让脚本能区分
「模型没发现问题」和「这次没跑成」，是自动化里最要紧的一件事。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sfly_agent.labels import SEVERITY_LABEL, STATUS_LABEL
from sfly_agent.labels import worker_label as worker_label_of
from sfly_agent.llm.base import LLMProvider
from sfly_agent.llm.registry import build_llm
from sfly_agent.prompt import dominant_language
from sfly_agent.rag.loader import RuleSet, load_rules
from sfly_agent.rag.retriever import select_rules
from sfly_bus.factory import Dependencies, open_dependencies
from sfly_bus.postgres import migrate_on_startup
from sfly_shared.aio import run
from sfly_shared.config import Settings, get_settings
from sfly_shared.console import configure_streams
from sfly_shared.contracts import (
    FilePatch,
    ResultStatus,
    Rule,
    Severity,
    WorkerResult,
)
from sfly_shared.diff import DiffParseResult, parse_unified_diff
from sfly_shared.heartbeat import run_service
from sfly_shared.ids import new_task_id
from sfly_shared.logging import get_logger, setup_logging
from sfly_workers.pool import serve_spec
from sfly_workers.runner import WorkerRunner
from sfly_workers.specs import SPECS, WorkerSpec, spec_for

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_BAD_INPUT = 3

#: 摘要里的严重度顺序：最严重的排最前，人扫一眼就知道要不要停下来。
SEVERITY_ORDER = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)

# 显示用的中文标签住在 ``sfly_agent.labels`` —— M5 的评论渲染要用同一张表，
# 而两处各写一份的结果一定是漂移（改了一处，PR 评论和命令行摘要从此对同一个
# ``medium`` 用两个词）。见那个模块的文档。


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sfly_workers",
        description="sfly 专业审查 Worker",
    )
    p.add_argument(
        "--spec",
        required=True,
        choices=[w.value for w in SPECS],
        help="本进程负责的审查类型；决定消费者组与人设",
    )
    p.add_argument(
        "--diff",
        metavar="PATH",
        help="独立 CLI 模式：直接审查一份 unified diff 并打印结果，不接队列",
    )
    p.add_argument(
        "--max-files",
        type=int,
        metavar="N",
        help="最多审几个文件（默认取 PR_MAX_FILES）",
    )
    p.add_argument(
        "--no-rules",
        action="store_true",
        help="不注入规则库。用来验证「没有规则时模型会不会开始自由发挥」",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="不往 stdout 打 JSON，只保留 stderr 上的人读摘要",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# 独立 CLI 模式
# --------------------------------------------------------------------------- #


def _read_diff(path: str) -> tuple[str | None, int]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace"), EXIT_OK
    except FileNotFoundError:
        print(f"找不到 diff 文件：{path}", file=sys.stderr)
        return None, EXIT_BAD_INPUT
    except OSError as exc:
        print(f"读不了 {path}：{exc}", file=sys.stderr)
        return None, EXIT_BAD_INPUT


def _select_files(parsed: DiffParseResult, limit: int) -> tuple[list[FilePatch], bool]:
    """按上限截取文件。

    **这里没有风险排序** —— 那属于编排层的 ``plan`` 节点（M5），
    因为它需要知道整个 PR 的上下文（哪些是核心模块、哪些是测试）。
    CLI 模式只需要一个安全的上限，避免有人拿一个 200 文件的大 PR
    去撞上下文长度。
    """
    if limit <= 0 or len(parsed.patches) <= limit:
        return parsed.patches, False
    return parsed.patches[:limit], True


def _rules_for(spec: WorkerSpec, patches: list[FilePatch], *, enabled: bool) -> list[Rule]:
    if not enabled:
        return []
    corpus: RuleSet = load_rules()
    return select_rules(
        corpus,
        worker_type=spec.worker_type,
        language=dominant_language(patches),
        core_ids=spec.core_rule_ids,
        top_k=spec.top_k_rules,
    )


def _print_summary(
    result: WorkerResult,
    *,
    spec: WorkerSpec,
    parsed: DiffParseResult,
    patches: list[FilePatch],
    rules: list[Rule],
    cap_applied: bool,
) -> None:
    out = sys.stderr
    changed = sum(len(p.changed_lines) for p in patches)

    print(file=out)
    worker_label = worker_label_of(spec.name)
    print(f"── {worker_label}审查结果 ─────────────────────────────", file=out)
    print(f"  文件       {len(patches)} 个（{changed} 个变更行）", file=out)
    if cap_applied:
        print(f"            已按上限截取，原 diff 里共 {parsed.total_files} 个文件", file=out)
    if parsed.skipped:
        names = "、".join(list(parsed.skipped)[:3])
        more = f" 等 {len(parsed.skipped)} 个" if len(parsed.skipped) > 3 else ""
        print(f"  跳过       {names}{more}（二进制或无 hunk）", file=out)
    print(f"  规则       {len(rules)} 条", file=out)
    print(f"  状态       {STATUS_LABEL.get(result.status.value, result.status.value)}", file=out)
    print(
        f"  成本       输入 {result.tokens_in} tokens / 输出 {result.tokens_out} tokens"
        f"，耗时 {result.latency_ms} 毫秒"
        f"，模型 {result.model or '未知'}",
        file=out,
    )

    if result.status is ResultStatus.FAILED:
        print(file=out)
        print(f"  审查失败：{result.error}", file=out)
        if result.error_class:
            print(f"  错误分类：{result.error_class.value}（不重试）", file=out)
        print("  原始响应已写入 JSON 的 raw_response 字段。", file=out)
        return

    counts = dict.fromkeys(SEVERITY_ORDER, 0)
    for f in result.findings:
        counts[f.severity] += 1
    summary = " · ".join(f"{SEVERITY_LABEL[s]} {counts[s]}" for s in SEVERITY_ORDER if counts[s])
    print(f"  发现       {len(result.findings)} 条" + (f"：{summary}" if summary else ""), file=out)

    unverified = [f for f in result.findings if not f.source_line_verified]
    if unverified:
        # 这条不是警告，是**已排期的降级**：GitHub 会 422 拒绝锚定在
        # 未变更行上的 inline 评论，所以它们会变成文件级评论。
        print(f"  行号       {len(unverified)} 条不在变更行上，将降级为文件级评论", file=out)
    if result.dropped_findings:
        print(f"  丢弃       {result.dropped_findings} 条未通过校验", file=out)

    if result.findings:
        print(file=out)
        for f in sorted(result.findings, key=lambda x: (SEVERITY_ORDER.index(x.severity), x.file, x.line)):
            mark = f"[{SEVERITY_LABEL[f.severity]}]"
            rule = f"（规则 {f.rule_id}）" if f.rule_id else ""
            # 类目名（sqli / n_plus_one …）保留英文原样：它是契约里的标识符，
            # 上面 JSON 里、数据库里、规则库里用的都是同一个字符串。
            # 摘要里翻译它，会让人对不上 JSON —— 那反而更难用。
            print(f"  {mark:<8}{f.file}:{f.line}  {f.category}{rule}", file=out)
            print(f"          {f.message}", file=out)

    if not result.findings:
        # 「没发现问题」和「没跑成」必须读起来完全不同。混在一起的话，
        # 一个配置错误的 Worker 会看起来像一次干净的审查。
        print(file=out)
        print("  未发现问题。", file=out)


async def _review_once(args: argparse.Namespace, settings: Settings) -> int:
    spec = spec_for(args.spec)

    text, code = _read_diff(args.diff)
    if text is None:
        return code

    parsed = parse_unified_diff(text, max_patch_chars=settings.per_file_patch_chars)
    if parsed.is_empty:
        # 这一条极容易写成「未发现问题」—— 那是最坏的一种误导：
        # 用户以为审查通过了，实际上面向的是空气。
        print(f"{args.diff} 里没有解析出任何可审查的文件。", file=sys.stderr)
        if parsed.skipped:
            for name, why in parsed.skipped.items():
                print(f"  {name}：{why}", file=sys.stderr)
        print("  确认这是一份 unified diff（含 diff --git / --- / +++ / @@ 头部）。", file=sys.stderr)
        return EXIT_BAD_INPUT

    limit = args.max_files if args.max_files is not None else settings.pr_max_files
    patches, capped = _select_files(parsed, limit)
    rules = _rules_for(spec, patches, enabled=not args.no_rules)

    llm: LLMProvider = build_llm(settings, worker_types=(spec.worker_type,))
    runner = WorkerRunner(spec, llm, settings)
    result = await runner.review(task_id=new_task_id(), patches=patches, rules=rules)

    if not args.quiet:
        print(result.model_dump_json(indent=2))
    _print_summary(result, spec=spec, parsed=parsed, patches=patches, rules=rules, cap_applied=capped)

    closer = getattr(llm, "aclose", None)
    if closer is not None:
        await closer()

    return EXIT_FAILED if result.status is ResultStatus.FAILED else EXIT_OK


# --------------------------------------------------------------------------- #
# 常驻消费模式
#
# 实现全部在 ``pool.py`` —— 那边的消费循环被两个宿主共用（一个 lane 一个容器，
# 或者三条协程一个进程）。这里只剩「把 Dependencies 里的句柄递给它」。
# --------------------------------------------------------------------------- #


async def _serve(spec: WorkerSpec, stop: asyncio.Event, deps: Dependencies) -> None:
    """容器形态的常驻消费：一个进程一条 lane。"""
    queue = deps.queue
    if queue is None:  # factory 保证不会；类型上是可空的，所以在这里收窄一次
        raise RuntimeError("队列没有被装配 —— factory.open_dependencies 应当总是给出它")
    await serve_spec(spec, stop, deps_queue=queue, deps_store=deps.store)


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.diff:
        # 独立 CLI 模式**不连数据库**：它是对着一份 diff 调提示词的，
        # 快一个数量级的原因正是这条路径上没有任何外部依赖。
        return await _review_once(args, settings)

    spec = spec_for(args.spec)

    # 常驻模式的依赖与建表：Worker 是写库的一方（save_result），
    # 所以它也需要 Postgres，且启动时要保证表在。五个容器同时建表由
    # pg_advisory_xact_lock 串行化。
    deps = await open_dependencies(settings)
    try:
        await migrate_on_startup(deps.store)
        await run_service(f"worker-{spec.name}", lambda stop: _serve(spec, stop, deps))
    finally:
        await deps.close()
    return EXIT_OK


def main(argv: list[str] | None = None) -> None:
    configure_streams()

    args = _parse_args(argv if argv is not None else sys.argv[1:])
    settings = get_settings()
    if args.diff:
        # --diff 模式下人读的是下面那段中文摘要，日志会和它抢注意力，
        # 所以压到 WARNING。
        #
        # **不能**用 logging.getLogger().setLevel() 来降级 —— structlog 的
        # PrintLogger 不经过标准库的 root logger，那一行看着像在静音，
        # 实际一点作用都没有。这是踩过的坑：命令行上照样刷出一堆英文 INFO。
        # 真正的开关是传给 setup_logging 的 level。
        #
        # 压到 WARNING 而不是 ERROR：真出问题时仍要看到告警
        # （比如 schema_unrecoverable），它比摘要更早说明发生了什么。
        setup_logging("WARNING", json_output=False)
    else:
        setup_logging(settings.log_level, settings.log_json)

    # 用 sfly_shared.aio.run：Windows 默认的 ProactorEventLoop 跑不了 psycopg 异步模式
    raise SystemExit(run(_main_async(args)))


if __name__ == "__main__":
    main()
