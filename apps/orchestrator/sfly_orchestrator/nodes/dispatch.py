"""``dispatch`` —— 把 ``planned_workers`` 各派发一条 ``TaskMessage``。

**这是整张图里唯一有不可撤销副作用的节点**（往队列里发消息）。
它会在两种情况下重跑，两种都不会产出错数据：

* **图重放** —— 恢复时 LangGraph 会重跑挂起的那个节点，而 ``dispatch``
  在 ``wait`` **之前**，它的写入已经在 checkpoint 里了，所以正常恢复不会重跑它。
  真正会重跑的是下面这一种。
* **orchestrator 在「派发完、checkpoint 还没写」之间崩掉** —— bootstrap 消息
  还没 ack，被 ``reclaim`` 捞回来，图从头再跑一遍，于是同一条任务被派发两次。

重派的代价是**幂等快路径救回来的**：副本重新拿到这条任务时，
``store.exists_result`` 会立刻命中（除非它还在跑），直接 ack，不烧 token。
就算它真的又跑了一遍，``worker_results`` 的复合主键会把重复结果吸收掉。

所以这里**不做「已派发过就跳过」的优化**：那需要在状态里记一份额外的账，
而那份账在崩溃时同样会丢 —— 用一个会丢的东西去防另一个会丢的东西。

### 三个 Worker 收到同一份补丁、不同的规则

``TaskMessage.rules`` 是 plan 阶段按 lane 检索好的。Worker 因此完全无状态，
也不需要依赖 RAG —— 这条边界让「规则检索有没有用」可以在评测里被单独关掉。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import BootstrapMessage, FilePatch, Rule, TaskMessage, WorkerType
from sfly_shared.logging import get_logger

log = get_logger(__name__)


async def dispatch(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    msg = BootstrapMessage.model_validate(state["bootstrap"])
    task_id = state["task_id"]

    patches = [FilePatch.model_validate(p) for p in state["file_patches"]]
    language = state.get("language", "unknown")
    rules_by_worker = state.get("rules", {})
    now = datetime.now(UTC)

    dispatched: dict[str, str] = {}
    for worker in _workers(state):
        rules = [Rule.model_validate(r) for r in rules_by_worker.get(worker.value, [])]
        task = TaskMessage(
            task_id=task_id,
            worker_type=worker,
            idempotency_key=msg.idempotency_key,
            repo_id=msg.repo_id,
            repo_node_id=msg.repo_node_id,
            pr_number=msg.pr_number,
            head_sha=msg.head_sha,
            base_sha=msg.base_sha,
            file_patches=patches,
            language=language,
            rules=rules,
            dispatched_at=now,
        )
        message_id = await ctx.queue.publish_task(task)
        dispatched[worker.value] = message_id
        await ctx.emit(
            task_id,
            "worker.dispatched",
            {
                "worker_type": worker.value,
                "message_id": message_id,
                # 这两个数字是运维用的：一条 `XRANGE` 只能看到 payload，
                # 想确认「规则真的送进去了」得解整包 JSON —— 那在排障时太慢。
                "files": len(patches),
                "rules": len(rules),
            },
        )

    log.info("node.dispatch", task_id=task_id, workers=list(dispatched), files=len(patches))
    return {"dispatched": dispatched}


def _workers(state: ReviewState) -> list[WorkerType]:
    """从状态里取回 ``planned_workers``。

    用 ``WorkerType(...)`` 而不是直接当字符串用：库里和队列里存的是 text，
    而 ``WorkerType("security")`` 会在遇到不认识的值时**当场报错**。
    直接当字符串用的话，一个拼错的 lane 名会安静地流过整条链路，
    直到某个 Worker 发现自己永远收不到消息。
    """
    return [WorkerType(w) for w in state.get("planned_workers", [])]
