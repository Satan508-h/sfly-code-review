"""``aggregate`` —— 主 Agent：把各 Worker 的上报收成一份报告。

**结果不是从图状态里读的，是从 Postgres 读的。** 这是全项目最重要的一个
设计选择，值得说清楚：

* ``wait`` 的屏障查询读的是 ``worker_results``（不是流、不是状态）
* 这里读的也是 ``worker_results``
* 扫描器的恢复查询读的是 ``review_runs``

也就是说，**图状态里根本没有 Worker 结果**。好处是三条：

1. 挂起的图不需要把 N 条结果背在 checkpoint 里（Neon 免费版只有 0.5GB）；
2. 恢复时不存在「状态里的结果和数据库里的不一致」这种问题 —— 只有一个真相来源；
3. 协调协程、扫描器、手工重跑，三个入口唤醒图时看到的东西完全一样。

聚合本身是纯函数（``sfly_agent.aggregate.pipeline``），这里只负责取数据、
写回结果。**不含任何 LLM 调用** —— 理由见那个模块的文档（评测要可复现）。
"""

from __future__ import annotations

from typing import Any

from sfly_agent.aggregate.pipeline import aggregate_run
from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import RunStatus
from sfly_shared.logging import get_logger

log = get_logger(__name__)


async def aggregate(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    task_id = state["task_id"]

    run = await ctx.store.get_run(task_id)
    if run is None:
        # 理论到不了：``ingest`` 刚把它建出来，而 run 的删除只发生在
        # ``purge_older_than``（14 天前）。真的到了说明有人在跑测试时手工清了库 ——
        # 那时报错比产出一份「针对不存在的 run 的报告」好得多。
        raise LookupError(f"run {task_id} 不存在，无法聚合")

    results = await ctx.store.get_results(task_id)
    # 成本从 llm_calls 汇总（Worker 在写结果之后记的账）。
    # 拿不到就是 0 —— 成本不该让一份报告生成失败。
    costs = await ctx.store.sum_costs(task_id)
    report = aggregate_run(run, results, cost_usd=costs.get("cost_usd", 0.0))

    await ctx.store.set_status(
        task_id,
        RunStatus.AGGREGATING,
        degraded=report.degraded,
        missing_workers=report.missing_workers,
    )
    # **先落库再往后走。** publish（M7 之后）会真的去调 GitHub，那里会限流、
    # 会 401、会因为 diff 变了而 422 —— 报告存下来了，重新发布就不必重新聚合。
    await ctx.store.save_report(report)

    await ctx.emit(
        task_id,
        "aggregate.done",
        {
            "findings": len(report.findings),
            "suppressed": len(report.suppressed),
            "conflicts": len(report.conflicts),
            "degraded": report.degraded,
            "missing_workers": [w.value for w in report.missing_workers],
            "tokens": report.totals.tokens_in + report.totals.tokens_out,
            "cost_usd": round(report.totals.cost_usd, 6),
        },
    )
    log.info(
        "node.aggregate",
        task_id=task_id,
        findings=len(report.findings),
        suppressed=len(report.suppressed),
        degraded=report.degraded,
        workers=[r.worker_type.value for r in results],
    )
    return {
        "report": report.model_dump(mode="json"),
        "degraded": report.degraded,
        "missing_workers": [w.value for w in report.missing_workers],
    }
