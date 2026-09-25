"""``python -m sfly_orchestrator`` —— 容器 2：LangGraph 状态机 + 协调协程 + 超时扫描器。

两种运行形态：

* **常驻**（默认）：图 + coordinator + sweeper 三条长驻协程，
  Worker 是另外三个容器。完整模式的形态。
* **单进程端到端**（``--task fixtures/task.json``）：上面三条**再加上
  一个 ``WorkerPool``**，投一条任务然后跟到终态，把报告和时间线打出来。

第二种形态存在的理由：**一条命令能证明整条链路是通的**。
分容器跑的时候「没出报告」有十几种可能（worker 没起来、Redis 连错、队列名不对、
consumer group 没建……），而单进程形态把变量全部固定，只剩业务逻辑本身。
它也是 M4 那次验收里手工做的事的自动化版本。

**它和容器共存是无害的**：三条 lane 加入的是同一批消费者组，
Redis 保证每条消息只投给组内一个成员 —— 所以「一起跑」= 分担负载，
不是重复处理。这正是消费者组的定义，不需要任何应用层代码协调。

退出码：``0`` 报告已生成／``2`` 失败或超时／``3`` 输入有问题。
和 Worker 的 ``--diff`` 同一个约定，方便脚本串联。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from langgraph.graph.state import CompiledStateGraph

from sfly_agent.diff import parse_unified_diff
from sfly_agent.labels import SEVERITY_LABEL, SEVERITY_ORDER, STATUS_LABEL, worker_label
from sfly_agent.state import ReviewState
from sfly_bus.base import TaskQueue
from sfly_bus.factory import Dependencies, open_dependencies
from sfly_bus.postgres import migrate_on_startup
from sfly_orchestrator.checkpointer import open_checkpointer, setup_on_startup
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.coordinator import Coordinator
from sfly_orchestrator.graph import NODES, build_graph
from sfly_orchestrator.runner import TERMINAL_STATUSES, GraphRunner
from sfly_orchestrator.sweeper import Sweeper
from sfly_shared.aio import run
from sfly_shared.config import Settings, get_settings
from sfly_shared.console import configure_streams
from sfly_shared.contracts import (
    BootstrapMessage,
    ReviewReport,
    RunEvent,
    RunRow,
    RunStatus,
    idempotency_key_for,
)
from sfly_shared.heartbeat import run_service
from sfly_shared.ids import new_id, new_task_id
from sfly_shared.logging import get_logger, setup_logging
from sfly_workers.pool import WorkerPool

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 2
EXIT_BAD_INPUT = 3

#: 跟终态时的轮询间隔。**没有走 SSE 或事件流** —— 这里要的是「等到它结束」，
#: 而轮询是这件事最不容易写错的形式（断线、丢事件、重连都不存在）。
_FOLLOW_INTERVAL_S = 0.5

#: 演示用的仓库身份。**不是真实存在的仓库** —— M6 会从 webhook 载荷里拿这些值，
#: 而现在是手工投递，所以需要一个明确写着「这是假的」的默认值。
_DEMO_REPO = "demo/sfly-playground"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sfly_orchestrator",
        description="sfly 编排器：LangGraph 状态机 + 协调协程 + 超时扫描器",
    )
    source = p.add_mutually_exclusive_group()
    source.add_argument(
        "--task",
        metavar="PATH",
        help="单进程端到端：从 JSON 文件读一条 BootstrapMessage，投出去并跟到终态",
    )
    source.add_argument(
        "--diff",
        metavar="PATH",
        help="单进程端到端：从一份 unified diff 构造任务（比 --task 少一个 fixture）",
    )
    p.add_argument(
        "--pr",
        type=int,
        default=1,
        metavar="N",
        help="--diff 模式下用的 PR 编号（只影响幂等键与报告里的展示）",
    )
    p.add_argument(
        "--repo",
        default=_DEMO_REPO,
        metavar="OWNER/NAME",
        help=f"--diff 模式下用的仓库名（默认 {_DEMO_REPO}）",
    )
    p.add_argument(
        "--new",
        action="store_true",
        help="当成一次全新的投递（换个 head_sha）。默认复用同一个 run —— 那正是幂等键该有的行为",
    )
    p.add_argument(
        "--timeout",
        type=float,
        metavar="秒",
        help="等待终态的上限（默认 RUN_DEADLINE_S + 60）",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="不往 stdout 打报告 JSON，只保留 stderr 上的人读摘要",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# 常驻模式
# --------------------------------------------------------------------------- #


async def _serve(stop: asyncio.Event, deps: Dependencies, graph: CompiledStateGraph[ReviewState]) -> None:
    """三条长驻协程。**任何一条结束都算异常** —— 它们都是 ``while True`` 的形态。"""
    ctx = _context(deps)
    workers = [
        asyncio.create_task(GraphRunner(ctx=ctx, graph=graph).run(stop), name="graph-runner"),
        asyncio.create_task(Coordinator(ctx=ctx, graph=graph).run(stop), name="coordinator"),
        asyncio.create_task(
            Sweeper(ctx=ctx, graph=graph, interval_s=ctx.settings.sweeper_interval_s).run(stop),
            name="sweeper",
        ),
    ]
    try:
        await stop.wait()
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


# --------------------------------------------------------------------------- #
# 单进程端到端模式
# --------------------------------------------------------------------------- #


async def _run_once(
    args: argparse.Namespace, deps: Dependencies, graph: CompiledStateGraph[ReviewState]
) -> int:
    msg, code = _load_task(args)
    if msg is None:
        return code
    if args.new:
        msg = _as_new_delivery(msg)
    queue = _require_queue(deps)

    run = await deps.store.create_run(msg)
    if run.status in TERMINAL_STATUSES:
        # **重放路径**：同一个 PR 的同一个 head_sha 又投了一次。
        # 这正是 M6 要演示的「投 3 次 → 1 run + 2 duplicate」，
        # 只是现在是从命令行触发的。
        print(
            f"这条任务已经跑过了（{run.status.value}）—— 不重复投递。（M6 的 webhook 会返回 duplicate）",
            file=sys.stderr,
        )
        await _print_outcome(deps, run, quiet=args.quiet)
        return EXIT_OK

    await queue.publish_bootstrap(msg)
    log.info("cli.published", task_id=run.task_id, pr=f"{run.repo_id}#{run.pr_number}")

    ctx = _context(deps)
    stop = asyncio.Event()
    pipeline = [
        asyncio.create_task(GraphRunner(ctx=ctx, graph=graph).run(stop), name="graph-runner"),
        asyncio.create_task(Coordinator(ctx=ctx, graph=graph).run(stop), name="coordinator"),
        asyncio.create_task(
            Sweeper(ctx=ctx, graph=graph, interval_s=ctx.settings.sweeper_interval_s).run(stop),
            name="sweeper",
        ),
        # 三种 Worker 跑在同一个进程里 —— ``WorkerPool`` 和精简模式用的是同一个类。
        asyncio.create_task(WorkerPool(queue=queue, store=deps.store).run(stop), name="worker-pool"),
    ]

    try:
        final = await _follow(deps, run, budget_s=_timeout_of(args, ctx.settings))
    finally:
        stop.set()
        for task in pipeline:
            task.cancel()
        await asyncio.gather(*pipeline, return_exceptions=True)

    await _print_outcome(deps, final, quiet=args.quiet)
    return EXIT_OK if final.status is RunStatus.PUBLISHED else EXIT_FAILED


async def _follow(deps: Dependencies, run: RunRow, *, budget_s: float) -> RunRow:
    """轮询到终态。超时返回**当时那一行**（不是 None）—— 调用方要能打印它。"""
    deadline = asyncio.get_running_loop().time() + budget_s
    current = run
    while asyncio.get_running_loop().time() < deadline:
        current = await deps.store.get_run(run.task_id) or current
        if current.status in TERMINAL_STATUSES:
            return current
        await asyncio.sleep(_FOLLOW_INTERVAL_S)

    log.error(
        "cli.follow_timeout",
        task_id=run.task_id,
        status=current.status.value,
        timeout_s=budget_s,
        hint="run 没有在超时前到终态。查 events_since 看它停在哪一步。",
    )
    return current


def _load_task(args: argparse.Namespace) -> tuple[BootstrapMessage | None, int]:
    """从 ``--task``（JSON）或 ``--diff``（unified diff）构造一条 bootstrap。"""
    if args.task:
        path = Path(args.task)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            print(f"找不到任务文件：{path}", file=sys.stderr)
            return None, EXIT_BAD_INPUT
        except (OSError, json.JSONDecodeError) as exc:
            print(f"读不了 {path}：{exc}", file=sys.stderr)
            return None, EXIT_BAD_INPUT
        try:
            return BootstrapMessage.model_validate(payload), EXIT_OK
        except Exception as exc:
            # 契约校验失败要**原样报出来**：BootstrapMessage 的校验器会检查
            # idempotency_key 与 repo/pr/head_sha 是否一致，而那条错误信息
            # 是唯一能说明「这个 fixture 哪里写错了」的东西。
            print(f"{path} 不是合法的 BootstrapMessage：\n  {exc}", file=sys.stderr)
            return None, EXIT_BAD_INPUT

    if args.diff:
        return _task_from_diff(args)

    print("要么给 --task，要么给 --diff。", file=sys.stderr)
    return None, EXIT_BAD_INPUT


def _task_from_diff(args: argparse.Namespace) -> tuple[BootstrapMessage | None, int]:
    """把一份 diff 变成一条 bootstrap。

    和 ``sfly_workers --diff`` 走同一个解析器（``parse_unified_diff``），
    所以「CLI 看得见的文件」和「编排器派下去的文件」永远是同一批 ——
    两处各解析一遍的话，`skipped` 的判定会慢慢分叉。

    ``head_sha`` 用 diff 内容的哈希：**同一个 diff 只投一次**
    （幂等键 = ``repo:pr:head_sha``），改了 diff 就是一次新的审查。
    这正是 GitHub 上 ``head_sha`` 的行为，只是这里没有 git 可以问。
    """
    path = Path(args.diff)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        print(f"找不到 diff 文件：{path}", file=sys.stderr)
        return None, EXIT_BAD_INPUT
    except OSError as exc:
        print(f"读不了 {path}：{exc}", file=sys.stderr)
        return None, EXIT_BAD_INPUT

    settings = get_settings()
    parsed = parse_unified_diff(text, max_patch_chars=settings.per_file_patch_chars)
    if parsed.is_empty:
        print(f"{path} 里没有解析出任何可审查的文件。", file=sys.stderr)
        for name, why in parsed.skipped.items():
            print(f"  {name}：{why}", file=sys.stderr)
        return None, EXIT_BAD_INPUT

    head_sha = _pseudo_sha(text)
    return (
        BootstrapMessage(
            task_id=new_task_id(),
            idempotency_key=idempotency_key_for(args.repo, args.pr, head_sha),
            repo_id=args.repo,
            repo_node_id=f"demo-node-{args.pr}",
            pr_number=args.pr,
            head_sha=head_sha,
            base_sha=head_sha,
            file_patches=parsed.patches,
            pr_title=path.name,
            pr_author="cli",
        ),
        EXIT_OK,
    )


def _as_new_delivery(msg: BootstrapMessage) -> BootstrapMessage:
    """把这条任务变成一个**新的幂等键**，也就是一个新 run。

    幂等键是 ``repo:pr:head_sha``（契约里由 ``assert_idempotency_key`` 强制），
    所以「新的投递」只能靠换 ``head_sha`` 来表达 —— 这也正是 GitHub 上的真实
    行为：同一个 PR 推了新提交，head_sha 变了，于是自然触发一次新审查。
    这里手工加一个随机后缀来模拟那件事。

    为什么需要它：不带 ``--new`` 时，第二次跑同一个 diff 会命中幂等键，
    CLI 会如实报告「已经跑过了」并打印上一次的报告。那是对的，但**演示**
    要的是每次都有新东西看。两者都要能表达，所以做成一个开关而不是改默认值。
    """
    payload = msg.model_dump()
    head_sha = f"{msg.head_sha}-{new_id('new')}"
    payload.update(
        {
            "task_id": new_task_id(),
            "head_sha": head_sha,
            "idempotency_key": idempotency_key_for(msg.repo_id, msg.pr_number, head_sha),
        }
    )
    return BootstrapMessage.model_validate(payload)


def _pseudo_sha(text: str) -> str:
    """diff 内容的 sha1 前 12 位，当作 head_sha 用。见 :func:`_task_from_diff`。"""
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def _timeout_of(args: argparse.Namespace, settings: Settings) -> float:
    """跟终态的上限。默认比 run 自己的 deadline 多 60 秒 —— 刚好够图跑完
    ``aggregate → finalize → publish`` 这三步。"""
    if args.timeout is not None:
        return float(args.timeout)
    return float(settings.run_deadline_s) + 60.0


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #


async def _print_outcome(deps: Dependencies, run: RunRow, *, quiet: bool) -> None:
    """打时间线（stderr）、人读摘要（stderr）、报告 JSON（stdout）。"""
    events = await deps.store.events_since(run.task_id, 0)
    report = await deps.store.get_report(run.task_id)

    _print_timeline(events)
    _print_summary(run, report)
    if report is not None and not quiet:
        print(report.model_dump_json(indent=2))


def _print_timeline(events: list[RunEvent]) -> None:
    out = sys.stderr
    print(file=out)
    print(f"── 时间线（{len(events)} 条事件）─────────────────────────", file=out)
    for event in events:
        payload = " ".join(f"{k}={_short(v)}" for k, v in sorted(event.payload.items()))
        print(f"  #{event.seq:<4}{event.kind:<20}{payload}", file=out)
    if not events:
        print("  （没有任何事件 —— run 卡在建记录之前）", file=out)


def _print_summary(run: RunRow, report: ReviewReport | None) -> None:
    out = sys.stderr
    print(file=out)
    print("── 审查结果 ─────────────────────────────────────────", file=out)
    print(f"  run        {run.task_id}", file=out)
    print(f"  PR         {run.repo_id}#{run.pr_number} @ {run.head_sha[:12]}", file=out)
    print(f"  状态       {STATUS_LABEL.get(run.status.value, run.status.value)}", file=out)
    print(
        f"  Worker     {', '.join(worker_label(w) for w in run.planned_workers) or '（无）'}"
        + (f"，缺 {', '.join(worker_label(w) for w in run.missing_workers)}" if run.missing_workers else ""),
        file=out,
    )
    print(
        f"  文件       {run.files_reviewed}/{run.files_total} 个"
        + ("（已按风险截断）" if run.diff_truncated else ""),
        file=out,
    )

    if report is None:
        print(file=out)
        print("  没有报告 —— run 没走到 aggregate。看上面的时间线停在哪一步。", file=out)
        return

    totals = report.totals
    print(
        f"  成本       输入 {totals.tokens_in} / 输出 {totals.tokens_out} tokens"
        f"，耗时 {totals.duration_ms / 1000:.1f} 秒"
        f"，{len(totals.per_worker_ms)} 个 Worker 上报"
        + (f"，${totals.cost_usd:.4f}" if totals.cost_usd else ""),
        file=out,
    )
    print(
        f"  结论       {'🔴 建议修改后再合并' if report.block_merge else '💬 供参考'}"
        f"（{report.decision_reason}）",
        file=out,
    )
    if report.degraded:
        print("  降级       是 —— 有 Worker 没上报或上报了失败结果", file=out)

    counts = dict.fromkeys(SEVERITY_ORDER, 0)
    for finding in report.findings:
        counts[finding.severity] += 1
    detail = " · ".join(f"{SEVERITY_LABEL[s]} {counts[s]}" for s in SEVERITY_ORDER if counts[s])
    print(f"  发现       {len(report.findings)} 条" + (f"：{detail}" if detail else ""), file=out)
    if report.suppressed:
        print(f"             另有 {len(report.suppressed)} 条置信度不足，只入库不发布", file=out)

    if report.findings:
        print(file=out)
        for finding in report.findings:
            mark = f"[{SEVERITY_LABEL[finding.severity]}]"
            sources = "、".join(worker_label(w) for w in finding.sources)
            print(
                f"  {mark:<8}{finding.file}:{finding.line}  {finding.category}"
                f"  置信度 {finding.adjusted_confidence:.0%}  来自 {sources}",
                file=out,
            )
            print(f"          {finding.message}", file=out)


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 40 else text[:37] + "…"


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


def _context(deps: Dependencies) -> NodeContext:
    return NodeContext(store=deps.store, queue=_require_queue(deps), lock=deps.lock)


def _require_queue(deps: Dependencies) -> TaskQueue:
    """编排器**没有队列就干不了活**（派发、消费 bootstrap、消费结果全靠它）。

    在这里硬失败而不是让节点各自判空：一个起不来但看起来正常的编排器，
    比一个直接报错的编排器难查一个数量级。
    """
    if deps.queue is None:  # pragma: no cover - factory 保证不会
        raise RuntimeError("编排器需要队列，但 factory 没有装配它")
    return deps.queue


@asynccontextmanager
async def _prepared(
    settings: Settings,
) -> AsyncIterator[tuple[Dependencies, CompiledStateGraph[ReviewState]]]:
    """打开依赖 → 建表 → 开 checkpointer → 编译图。

    两种运行形态共用这一段。``finally`` 里关依赖：停机信号、异常、取消三条
    路径都要走到 —— 漏掉的话容器停止时会留下没关的连接，而 Postgres 侧要等到
    TCP 超时才发现（表现为重启后的第一波查询变慢）。
    """
    deps = await open_dependencies(settings)
    try:
        # 建表（幂等）。五个容器同时启动时会一起走到这里 —— 串行化靠
        # pg_advisory_xact_lock，见 sfly_bus/migrations/。
        await migrate_on_startup(deps.store)

        # checkpointer 有自己的连接池（autocommit，见 checkpointer.py），
        # 生命周期也只到这里为止。
        async with open_checkpointer(settings.database_url) as saver:
            await setup_on_startup(saver)
            yield deps, build_graph(_context(deps), checkpointer=saver)
    finally:
        await deps.close()


async def _once(args: argparse.Namespace) -> int:
    """``--task`` / ``--diff``：投一条任务，跟到终态，打报告。"""
    async with _prepared(get_settings()) as (deps, graph):
        return await _run_once(args, deps, graph)


async def _serve_body(stop: asyncio.Event) -> None:
    """常驻模式。三条长驻协程，Worker 是另外三个容器。"""
    settings = get_settings()
    async with _prepared(settings) as (deps, graph):
        log.info(
            "orchestrator.ready",
            wait_strategy=settings.wait_strategy,
            sweeper_interval_s=settings.sweeper_interval_s,
            run_deadline_s=settings.run_deadline_s,
            conflict_resolver=settings.conflict_resolver,
            pipeline=" → ".join(NODES),
        )
        await _serve(stop, deps, graph)


def main(argv: list[str] | None = None) -> None:
    # 报告正文里有中文和 emoji：不设编码的话，重定向到文件时会抛
    # UnicodeEncodeError —— 而那时报告已经生成好了，命令却以非零码退出。
    configure_streams()
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.task or args.diff:
        # 单次运行的形态：人读的是下面那段中文摘要，日志会和它抢注意力。
        # 压到 WARNING 而不是 ERROR —— 真出问题时仍要看到告警（比如
        # schema_unrecoverable），它比摘要更早说明发生了什么。
        # 注意不能用 logging.getLogger().setLevel()：structlog 不走标准库的
        # root logger，那一行看着像在静音，实际无效。见 sfly_workers/__main__.py。
        setup_logging("WARNING", json_output=False)
        # 用 sfly_shared.aio.run：Windows 默认的 ProactorEventLoop 跑不了
        # psycopg 的异步模式（见该模块）。
        raise SystemExit(run(_once(args)))

    run(run_service("orchestrator", _serve_body))


if __name__ == "__main__":
    main()
