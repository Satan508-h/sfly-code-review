"""七个节点 —— 每个都是 ``async def node(state, ctx) -> dict[str, Any]``。

    ingest → plan → dispatch → wait → aggregate → finalize → publish

### 为什么是「一个节点一个文件」

不是为了整齐，是为了让**每个节点的失败模式**有一个自己的地方写下来。
这七个节点的差异不在代码量（最大的 ``wait`` 也就几十行），而在它们各自
「什么时候会失败、失败了之后系统处于什么状态」：

* ``ingest`` —— 唯一一个会被**重复执行**的节点（bootstrap 重投），
  所以它调的是幂等的 ``create_run``
* ``plan`` —— 唯一一个**可能提前结束整个 run** 的节点（PR 里没有可审的文件）
* ``dispatch`` —— 唯一一个有**副作用且不可撤销**的节点（往队列里发消息）
* ``wait`` —— 唯一一个会**挂起**的节点，也是全项目唯一用到 ``interrupt()`` 的地方
* ``aggregate`` / ``finalize`` —— 纯计算，失败只会重跑（幂等写入）
* ``publish`` —— M7 之后唯一一个**会对外发东西**的节点

### 每个节点都必须是幂等的

LangGraph 恢复时会重新执行节点。这不是「可能会」，是**正常路径**：
图在 ``wait`` 挂起后被唤醒时，``wait`` 会被完整地重跑一遍。

所以规则是：节点里的每一次写入都要能重放。对这四个节点来说，
它们调用的仓储方法本来就是幂等的（``create_run`` / ``set_plan`` / ``save_report``
都是 upsert），真正需要小心的是 ``dispatch``（见那个文件的文档）。
"""

from __future__ import annotations

from sfly_orchestrator.nodes.aggregate import aggregate
from sfly_orchestrator.nodes.dispatch import dispatch
from sfly_orchestrator.nodes.finalize import finalize
from sfly_orchestrator.nodes.ingest import ingest
from sfly_orchestrator.nodes.plan import plan
from sfly_orchestrator.nodes.publish import publish
from sfly_orchestrator.nodes.wait import wait

__all__ = ["aggregate", "dispatch", "finalize", "ingest", "plan", "publish", "wait"]
