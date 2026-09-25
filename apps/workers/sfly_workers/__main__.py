"""``python -m sfly_workers --spec {security|performance|style}``

一套代码、三种部署。三个容器跑的是**同一个镜像同一个入口**，
只靠 ``--spec`` 区分消费者组和人设 —— 见 ``specs.py``。

两种运行形态：

* **独立 CLI**（``--diff fixtures/x.diff``）：不接队列、不起容器、不连数据库，
  对着一份 diff 跑单个 Worker 然后打印结果。调试提示词时它比走完整链路
  快一个数量级，而且**格式化输出到 stdout、人读的摘要到 stderr**，
  所以 ``... | jq '.findings[]'`` 直接用。
* **常驻消费**（默认）：接队列跑消费循环（M3 实现）。

退出码：``0`` 有结果（含 partial）／``2`` 审查失败（解析不出 JSON）／
``3`` 输入有问题（文件不存在、里面没有 diff）。让脚本能区分
「模型没发现问题」和「这次没跑成」，是自动化里最要紧的一件事。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from sfly_agent.diff import DiffParseResult, parse_unified_diff
from sfly_agent.llm.base import LLMProvider
from sfly_agent.llm.registry import build_llm
from sfly_agent.prompt import dominant_language
from sfly_agent.rag.loader import RuleSet, load_rules
from sfly_agent.rag.retriever import select_rules
from sfly_shared.aio import run
from sfly_shared.config import Settings, get_settings
from sfly_shared.contracts import FilePatch, ResultStatus, Rule, Severity, WorkerResult
from sfly_shared.heartbeat import run_service
from sfly_shared.ids import new_task_id
from sfly_shared.logging import get_logger, setup_logging
from sfly_workers.runner import WorkerRunner
from sfly_workers.specs import SPECS, WorkerSpec, spec_for

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_BAD_INPUT = 3

#: 摘要里的严重度顺序：最严重的排最前，人扫一眼就知道要不要停下来。
_SEVERITY_ORDER = (Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW, Severity.INFO)

# --------------------------------------------------------------------------- #
# 显示用的中文标签
#
# 契约里的取值（``critical`` / ``security`` / ``ok``）是**标识符**，它们会进
# JSON、进数据库、进 API 响应，一个字都不能改。但人读的那段摘要不该出现英文 ——
# 命令行上的读者不需要为了看懂「这条要不要紧」而先学会一套英文词表。
# 所以这里做的是**显示层翻译**：JSON 里仍然是 ``critical``，摘要里是「严重」。
# --------------------------------------------------------------------------- #

_SEVERITY_LABEL: dict[Severity, str] = {
    Severity.CRITICAL: "严重",
    Severity.HIGH: "高危",
    Severity.MEDIUM: "中危",
    Severity.LOW: "低危",
    Severity.INFO: "提示",
}

_WORKER_LABEL: dict[str, str] = {
    "security": "安全",
    "performance": "性能",
    "style": "风格",
}

_STATUS_LABEL: dict[str, str] = {
    "ok": "正常",
    "partial": "部分成功（有条目未通过校验）",
    "failed": "失败",
}


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
    worker_label = _WORKER_LABEL.get(spec.name, spec.name)
    print(f"── {worker_label}审查结果 ─────────────────────────────", file=out)
    print(f"  文件       {len(patches)} 个（{changed} 个变更行）", file=out)
    if cap_applied:
        print(f"            已按上限截取，原 diff 里共 {parsed.total_files} 个文件", file=out)
    if parsed.skipped:
        names = "、".join(list(parsed.skipped)[:3])
        more = f" 等 {len(parsed.skipped)} 个" if len(parsed.skipped) > 3 else ""
        print(f"  跳过       {names}{more}（二进制或无 hunk）", file=out)
    print(f"  规则       {len(rules)} 条", file=out)
    print(f"  状态       {_STATUS_LABEL.get(result.status.value, result.status.value)}", file=out)
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

    counts = dict.fromkeys(_SEVERITY_ORDER, 0)
    for f in result.findings:
        counts[f.severity] += 1
    summary = " · ".join(f"{_SEVERITY_LABEL[s]} {counts[s]}" for s in _SEVERITY_ORDER if counts[s])
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
        for f in sorted(result.findings, key=lambda x: (_SEVERITY_ORDER.index(x.severity), x.file, x.line)):
            mark = f"[{_SEVERITY_LABEL[f.severity]}]"
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
# --------------------------------------------------------------------------- #


async def _consume_forever(spec: WorkerSpec, stop: object) -> None:
    """常驻消费循环。**M4 交付，不是 M3。**

    队列那一层（M3）已经就绪 —— ``bus.consume_tasks`` 在真 Redis 上跑得通，
    回收、重试计数、死信都有测试。这个循环之所以还差一步，缺的是
    ``store.save_result()``：投递顺序铁律要求「先落库、再 XADD、最后 XACK」，
    而落库要等 M4 的 Postgres schema 和仓储。

    先落库这一条不能省：反过来（先 XADD）会让编排器被唤醒去读一个还不存在的
    结果，屏障检查失败；先 XACK 则会让结果同时从 PEL 和数据库消失。

    形态是（顺序即约定 #1）：
        async for handle, task in bus.consume_tasks(spec.worker_type):
            if await already_done(task):          # 幂等快路径
                await handle.ack(); continue
            try:
                result = await runner.review(...)
            except Exception as exc:
                # 失败也是结果：先补一条 failed 结果闭合屏障，再决定是否进死信
                result = WorkerResult.failed(task.task_id, spec.worker_type, str(exc),
                                             classify(exc), attempt=handle.attempt)
            await store.save_result(result)        # 1. 先落库
            await bus.publish_result(result)       # 2. 再唤醒编排器
            await handle.ack()                     # 3. 最后离开 PEL
    """
    settings = get_settings()
    log.info(
        "worker.skeleton",
        consumer_group=spec.consumer_group,
        stream=spec.stream,
        categories=len(spec.categories),
        concurrency=settings.worker_concurrency,
        claim_idle_ms=settings.claim_idle_ms,
        status="消费循环等 M4 的 RunStore；队列层（M3）已就绪",
    )
    raise SystemExit(
        "常驻消费模式还没实现（等 M4 的 Postgres 仓储）。\n"
        f"  现在可以用：python -m sfly_workers --spec {spec.name} --diff fixtures/security_demo.diff\n"
        "  想看队列层已经能做到什么（Worker 猝死 → 副本回收）：python scripts/demo_reclaim.py"
    )


async def _main_async(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.diff:
        return await _review_once(args, settings)

    spec = spec_for(args.spec)
    await run_service(f"worker-{spec.name}", lambda stop: _consume_forever(spec, stop))
    return EXIT_OK


def _configure_streams() -> None:
    """按「输出到哪儿」决定编码。

    这里是两台机器共用一份代码时的经典问题，而且**两个方向都会错**：

    * 输出到**管道或文件**（``> out.json``、``| jq``）时必须是 UTF-8。
      Windows 上 Python 默认用系统编码（中文系统是 cp936），于是重定向出来的
      json 文件不是合法 UTF-8 —— 本地看着正常，别的工具一读就乱码。
    * 输出到**控制台**时保留控制台自己的编码。强行改成 UTF-8 会让中文
      在 cp936 终端里变成一堆问号，也就是把一个能看的结果变成一个不能看的。

    所以判据是 ``isatty()`` 而不是平台。``errors="replace"`` 两侧都要加：
    diff 里什么字符都可能有，输出少一个字符远比整条命令失败好。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        if stream.isatty():
            reconfigure(errors="replace")
        else:
            reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> None:
    _configure_streams()

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
