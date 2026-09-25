"""``publish`` —— 把评论投出去。

### M5 的诚实说明

**这个节点现在不发任何东西。** GitHub 客户端是 M7 的交付物，所以当前它只做
「报告定稿 + 收尾」：状态推到 ``published``，事件里明写 ``posted: false``。

这么做的理由：M6 的 SSE 和 M8 的 UI 需要一个**终态**才能正常工作，
而缺了 ``publish`` 这一步，图就没有终点 —— 每个消费者都要自己判断
「这个 run 算不算跑完了」，判断写五遍就会有五种写法。

那个 ``posted: false`` 不是敷衍，是给读者和前端留的诚实标记：
**没有 ``github_comment_id`` 的 run 就是没发过评论**，UI 不该显示「已评论」。
M7 接上客户端之后，这一个字段就是唯一需要改的地方。

### M7 要在这里加的三道闸

1. 发帖前查 ``review_runs.github_comment_id`` —— 已经发过就不重发
2. 正文里的隐藏标记 ``<!-- sfly:run:{task_id} -->`` —— 另一道（见 render.py）
3. 失败时 ``set_status(PUBLISH_FAILED)`` 而**不是** FAILED ——
   报告已经落库了，这不是一次失败的审查，是一次失败的投递，
   两者在 UI 上的处置完全不同（后者应该有个「重新发布」按钮）

这三条现在写在注释里而不是先实现出来：没有 HTTP 调用的时候，
「防重复发布」的代码是**无法被验证**的，而一段验证不了的代码，
最好的情况是它没用，最坏的情况是它错了而你以为它在保护你。
"""

from __future__ import annotations

from typing import Any

from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import ReviewReport, RunStatus
from sfly_shared.logging import get_logger

log = get_logger(__name__)


async def publish(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    report = ReviewReport.model_validate(state["report"])
    task_id = report.task_id

    # M7：这里调 github 客户端发评论，拿到 comment_id 之后 mark_published。
    # 现在没有客户端，所以 posted 恒为 False。
    posted = False

    # **状态先写，事件后写。顺序是刻意的**，因为两者之间必然有一个窗口
    # （两次写库不可能原子），而两种顺序的失败方向不一样：
    #
    # * 状态先 → 崩在中间：run 读作「已完成」而时间线少了最后一条。客户端跟着
    #   ``run.finished`` 事件走的话，它等到的是连接超时，然后读一次状态发现
    #   已经完成了 —— 安全的失败方向。
    # * 事件先 → 崩在中间：run 永远停在 ``aggregating``，而**没有任何东西能
    #   唤醒它** —— 扫描器的 ``due_runs`` 只看 ``dispatched``/``waiting``
    #   （见那个方法的文档）。一个永远不动的 run 比一条缺失的事件难解释得多。
    #
    # 所以断言「run 到终态了」之后不能立刻去读事件 —— 那不是测试写得不对，
    # 是这两件事本来就没有先后保证。
    await ctx.store.set_status(task_id, RunStatus.PUBLISHED)
    await ctx.emit(
        task_id,
        "publish.done",
        {
            "posted": posted,
            "reason": "github_client_not_implemented" if not posted else "ok",
            "block_merge": report.block_merge,
            "findings": len(report.findings),
            "comment_chars": len(report.comment_body),
        },
    )
    await ctx.emit(
        task_id,
        "run.finished",
        {
            "status": RunStatus.PUBLISHED.value,
            "degraded": report.degraded,
            "findings": len(report.findings),
            "cost_usd": round(report.totals.cost_usd, 6),
            "duration_ms": report.totals.duration_ms,
        },
    )
    log.info(
        "node.publish",
        task_id=task_id,
        posted=posted,
        block_merge=report.block_merge,
        note="M5 不发评论（GitHub 客户端是 M7）；posted=false 会在 UI 上如实显示",
    )
    return {}
