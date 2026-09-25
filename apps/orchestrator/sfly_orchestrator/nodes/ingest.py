"""``ingest`` —— 建 run 记录，写第一条时间线事件。

这是**唯一一个会被重复执行的节点**（bootstrap 消息被回收重投时），
所以它调的是幂等的 ``create_run``：撞上幂等键就返回已有的那一行，
不会新建、也不会把状态倒退。

注意 ``run.task_id`` 未必等于 ``bootstrap["task_id"]``：GitHub 超时重投同一个
webhook 时，第二次的 task_id 是新的，而幂等键（``repo:pr:head_sha``）是旧的 ——
``create_run`` 会返回**已有**的 run。图要跟着已有那个 run 走，否则一次 PR
会产出两份报告、两条评论。这件事在 ``GraphRunner`` 里就已经定好了
（它拿返回的 ``run.task_id`` 当 ``thread_id``），这里只负责把
状态里的 ``task_id`` 对齐成同一个 —— 状态和线程必须指向同一个 run。
"""

from __future__ import annotations

from typing import Any

from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import BootstrapMessage
from sfly_shared.logging import bind_task, get_logger

log = get_logger(__name__)


async def ingest(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    msg = BootstrapMessage.model_validate(state["bootstrap"])

    run = await ctx.store.create_run(msg)
    # 绑在**解析出来的** task_id 上：幂等键撞车时它和 bootstrap 里的那一个不一样，
    # 而之后所有日志都该跟着真正在跑的那个 run。
    bind_task(run.task_id)
    await ctx.emit(
        run.task_id,
        "run.created",
        {
            "repo_id": run.repo_id,
            "pr_number": run.pr_number,
            "head_sha": run.head_sha,
            "pr_title": msg.pr_title,
            "pr_author": msg.pr_author,
            "files": len(msg.file_patches),
        },
    )
    log.info(
        "node.ingest",
        task_id=run.task_id,
        pr=f"{run.repo_id}#{run.pr_number}",
        files=len(msg.file_patches),
        resumed=run.status.value != "queued",
    )
    # ``task_id`` 会覆盖入口状态里的那一个 —— 见模块文档。
    # 这不是「修正输入」，是让状态和线程指向同一个 run。
    return {"task_id": run.task_id}
