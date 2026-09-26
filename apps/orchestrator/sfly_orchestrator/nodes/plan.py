"""``plan`` —— 决定审哪些文件、送哪些规则、等多久。

这是编排层存在的**主要理由**。API 只写一条 bootstrap，不做任何判断
（CLAUDE.md：「API 不直接写 review_tasks」），所以三件事落在这里：

1. **文件风险排序与截断**（``PR_MAX_FILES``）。见 ``sfly_agent.risk``。
2. **规则检索**。检索结果进 ``TaskMessage.rules``，于是 Worker 保持无状态、
   不需要 RAG 依赖；同时也让「检索到的规则是否提升了精确率」成为一个
   **可测量的实验**（评测要能对比「带规则 / 不带规则」两份报告）——
   检索藏在 Worker 里的话，这件事就测不了。
3. **deadline**。``set_plan`` 把它落库，从那以后超时扫描器开始管这个 run。
   这是「任何可能卡住的状态都必须是一行带 deadline 的记录」那条约定的落点。

### 提前结束

PR 里没有可审的文件（全是二进制 / 纯删除 / 空 diff）时，这个节点直接结束整张图，
把 run 标成 ``skipped``。**不能让它走下去** —— 走下去会得到一份
「0 条发现」的报告，也就是一个"审查通过"的信号，而实际上什么都没看。
那是最坏的一种误导（``sfly_workers`` 的 CLI 在空 diff 上宁可退出码 3 也是这个道理）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sfly_agent.prompt import dominant_language
from sfly_agent.rag.loader import load_rules
from sfly_agent.rag.retriever import select_rules
from sfly_agent.risk import rank_files, select_files
from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import BootstrapMessage, FilePatch, RunStatus, WorkerType
from sfly_shared.github_files import patches_from_files
from sfly_shared.logging import get_logger
from sfly_workers.specs import spec_for

log = get_logger(__name__)


async def plan(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    msg = BootstrapMessage.model_validate(state["bootstrap"])
    task_id = state["task_id"]

    patches = await _patches_to_review(msg, ctx, task_id)
    selected, truncated = select_files(patches, ctx.settings.pr_max_files)
    if not selected:
        await ctx.store.set_status(task_id, RunStatus.SKIPPED)
        await ctx.emit(
            task_id,
            "run.finished",
            {"status": RunStatus.SKIPPED.value, "reason": "no_reviewable_files"},
        )
        log.warning(
            "node.plan_nothing_to_review",
            task_id=task_id,
            incoming_files=len(patches),
            hint="全是二进制/纯删除/空 diff —— 标 skipped 而不是产出一份「0 条发现」的报告",
        )
        # 空 dict 也能走，但状态里要留下 planned_workers=[]：
        # ``route_after_plan`` 靠它决定去 dispatch 还是直接结束。
        return {"planned_workers": [], "files_total": 0, "files_reviewed": 0}

    language = dominant_language(selected)
    workers = planned_workers(msg)
    rules = _rules_for(workers, language, ctx)
    deadline = datetime.now(UTC) + timedelta(seconds=ctx.settings.run_deadline_s)

    await ctx.store.set_plan(
        task_id,
        workers,
        files_total=len(patches),
        files_reviewed=len(selected),
        diff_truncated=truncated,
        deadline_at=deadline,
    )
    await ctx.emit(
        task_id,
        "node.finished",
        {
            "node": "plan",
            "planned_workers": [w.value for w in workers],
            "files_total": len(patches),
            "files_reviewed": len(selected),
            "diff_truncated": truncated,
            "deadline_at": deadline.isoformat(),
        },
    )

    ranked = rank_files(selected[:5])
    log.info(
        "node.plan",
        task_id=task_id,
        workers=[w.value for w in workers],
        files=len(selected),
        truncated=truncated,
        language=language,
        rules={w.value: len(rules[w.value]) for w in workers},
        # 前几个文件以及「为什么它们排前面」—— 截断发生时，这是唯一能解释
        # 「为什么那个文件没被审」的地方，而默认的 DEBUG 级别等于没有。
        top=[f"{r.patch.path}({r.score}:{r.reason})" for r in ranked],
    )

    return {
        "file_patches": [p.model_dump(mode="json") for p in selected],
        "language": language,
        "rules": rules,
        "files_total": len(patches),
        "files_reviewed": len(selected),
        "diff_truncated": truncated,
        "deadline_at": deadline.isoformat(),
        "planned_workers": [w.value for w in workers],
    }


async def _patches_to_review(
    msg: BootstrapMessage,
    ctx: NodeContext,
    task_id: str,
) -> list[FilePatch]:
    """这次要审的补丁。**载荷里没有就自己去 GitHub 拉。**

    GitHub 的 ``pull_request`` 事件**不带任何代码** —— 它只有元数据
    （标题、作者、head_sha、仓库 id）。所以线上那条路上 ``msg.file_patches``
    **永远是空的**，文件得自己调一次
    ``GET /repos/{owner}/{repo}/pulls/{n}/files``。

    本地一直没暴露这件事，是因为 fixture 是**录出来的**：录制脚本把那次 API
    的响应一起塞进了载荷（见 ``scripts/record_pr_fixture.py``），于是回放时
    看着像一切正常。线上收到的是真载荷，里面没有 ``files`` 这个键 ——
    少了这一步，开一个 PR 的结局是 webhook 返回 200、run 被标成 ``skipped``、
    PR 上什么都不会出现。

    ### 拉不到就**抛**，绝不允许走到「没有可审的文件」

    ``skipped`` 的含义是「这个 PR 确实没有可审的东西」（全是二进制 / 纯删除 /
    空 diff）。把一次拉取失败也归到那里，等于把「我们根本没看到代码」说成
    「代码没有问题」—— 一份看起来完全正常的空报告。这是本项目最贵的一类
    bug，所以两者的处置必须分开。

    抛出去的后果是对的：``GraphRunner`` 会重试到 ``MAX_ATTEMPTS``（限流、
    网络抖动都会自愈），次数用完了才把 run 标成 ``failed`` —— 一个终态。
    抛之前先写一条事件，因为那条 run 在 UI 上只有状态没有原因，而原因在这里。
    """
    if msg.file_patches:
        return list(msg.file_patches)

    if ctx.github is None:
        # 没配 token。**这不是「没有文件」** —— 是「我们没法去看」。
        reason = "载荷里没有文件，而且没配 GITHUB_TOKEN，拉不到代码"
        await ctx.emit(task_id, "plan.fetch_failed", {"reason": reason, "pr": _pr(msg)})
        raise RuntimeError(reason)

    try:
        files = await ctx.github.pull_files(msg.repo_id, msg.pr_number)
    except Exception as exc:
        # 先把原因写进时间线，再让异常往上走 —— 重试的决策属于消费循环，
        # 不属于这里（见模块文档）。
        await ctx.emit(
            task_id,
            "plan.fetch_failed",
            {"reason": f"拉取文件失败：{type(exc).__name__}", "error": str(exc)[:300], "pr": _pr(msg)},
        )
        raise

    patches, skipped = patches_from_files(files, max_patch_chars=ctx.settings.per_file_patch_chars)
    log.info(
        "node.plan_fetched",
        task_id=task_id,
        pr=_pr(msg),
        files=len(patches),
        skipped=len(skipped),
        # 跳过原因要能看见：「审了 3 个文件」和「有 5 个文件、2 个没法审」
        # 是两件事，而后者只在日志里说得出为什么。
        skip_reasons=dict(list(skipped.items())[:5]),
    )
    return patches


def _pr(msg: BootstrapMessage) -> str:
    return f"{msg.repo_id}#{msg.pr_number}"


def planned_workers(msg: BootstrapMessage) -> list[WorkerType]:
    """这次要跑哪几个 Worker。

    ``requested_workers`` 为空是**常态**（GitHub 的 webhook 不知道我们有几种
    Worker），表示「全部」—— 从 ``WorkerType`` 枚举推导而不是写字面量，
    加第四个 Worker 时这里自动跟上。这正是 ``base.py`` 里 ``WORKER_TYPES``
    那段注释说的同一件事。
    """
    if not msg.requested_workers:
        return list(WorkerType)
    wanted = set(msg.requested_workers)
    return [w for w in WorkerType if w in wanted]


def _rules_for(workers: list[WorkerType], language: str, ctx: NodeContext) -> dict[str, list[dict[str, Any]]]:
    """每个 lane 检索自己的规则。返回 JSON（状态里只放 JSON）。

    规则库加载失败会**抛出来**（``RuleCorpusError``）：一个加载不全的规则库
    会让检索静默地少几条，而症状是「某个类目的问题从来没人报」——
    那比一次失败的 run 难发现得多。
    """
    corpus = load_rules()
    out: dict[str, list[dict[str, Any]]] = {}
    for worker in workers:
        spec = spec_for(worker)
        picked = select_rules(
            corpus,
            worker_type=worker,
            language=language,
            core_ids=spec.core_rule_ids,
            top_k=spec.top_k_rules,
        )
        out[worker.value] = [r.model_dump(mode="json") for r in picked]
    return out
