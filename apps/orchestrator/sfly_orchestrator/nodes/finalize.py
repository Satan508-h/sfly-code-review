"""``finalize`` —— 定稿：决定阻断、渲染评论正文、写回报告。

为什么它和 ``aggregate`` 是两个节点（而不是一个）：**两者的失败后果不同。**
聚合失败意味着没有报告；渲染失败意味着报告在、只是没法发布 ——
重跑渲染即可，不必重跑聚合。M5 里两者都是纯函数、都不会失败，
但 M7 之后 ``publish`` 会真的调 GitHub，那时这个边界就开始值钱了。

现在它的实际工作是三步：

1. ``decide()`` —— 规则引擎决定 ``block_merge``（**永不 APPROVE**）
2. ``render_comment()`` —— 生成 Markdown 正文，含隐藏标记
3. ``save_report()`` —— 覆盖写回（``ON CONFLICT DO UPDATE``，重跑安全）

正文**先落库再发布**：GitHub 限流时报告不能跟着一起丢，
``comment_body`` 存在库里，重新发布不需要重新聚合。
"""

from __future__ import annotations

from typing import Any

from sfly_agent.aggregate.decision import decision_text
from sfly_agent.aggregate.pipeline import finalize_run
from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import ReviewReport
from sfly_shared.logging import get_logger

log = get_logger(__name__)


async def finalize(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    report = ReviewReport.model_validate(state["report"])
    report = finalize_run(report)
    await ctx.store.save_report(report)
    # 决定与成本**另写一份到 run 行**：运行列表要显示「阻断 / 供参考」和花了多少钱，
    # 而它不该为了这两个值去解每一行的报告 jsonb。见 ``set_decision`` 的说明。
    await ctx.store.set_decision(report.task_id, block_merge=report.block_merge, totals=report.totals)

    await ctx.emit(
        state["task_id"],
        "node.finished",
        {
            "node": "finalize",
            "block_merge": report.block_merge,
            "decision_reason": report.decision_reason,
            "comment_chars": len(report.comment_body),
        },
    )
    log.info(
        "node.finalize",
        task_id=report.task_id,
        block_merge=report.block_merge,
        decision=report.decision_reason,
        decision_text=decision_text(report.decision_reason),
    )
    return {"report": report.model_dump(mode="json")}
