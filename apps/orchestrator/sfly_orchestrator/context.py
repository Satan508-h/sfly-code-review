"""节点拿到的依赖句柄。

**节点是纯函数，依赖从参数进来。** 签名统一是
``async def node(state: ReviewState, ctx: NodeContext) -> dict[str, Any]``。

为什么不是闭包（节点直接捕获 store/queue）：
闭包写起来少一层，但它让**单测必须造一整套依赖**才能调一个节点 ——
而节点里确实有一批能纯逻辑验证的东西（``plan`` 的排序与截断、
``wait`` 的屏障判定与超时兜底）。参数化的写法让那些测试传一个假 ctx 就够了，
而假 ctx 只需要实现它真正用到的那两个方法。

为什么不是 LangGraph 的 ``context_schema``：那套机制（``Runtime[ContextT]``）
适合「同一次调用内共享的上下文」，而这里的 store/queue 是**进程级的长生命周期
对象**，它们的生命周期跟容器走，不跟某一次图执行走。用 context_schema
意味着每次 ``ainvoke`` 都要把它们塞进 config —— 多一处能塞错的地方，
而塞错的症状是节点里 ``ctx.store`` 是 None。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sfly_bus.base import Lock, RunStore, TaskQueue
from sfly_shared.config import Settings, get_settings
from sfly_shared.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class NodeContext:
    """一个图执行期间共享的依赖。

    ``queue`` 在类型上是可空的（``Dependencies`` 允许没有队列），
    但**图必须有队列才跑得动** —— 没有队列就没法派发任务，屏障永远闭合不了。
    所以 :meth:`require_queue` 在这里收窄一次，而不是让每个节点自己判空：
    判空写六遍，就会有六种不同的处理方式。
    """

    store: RunStore
    #: **图必须有队列才跑得动**（没有它就没法派发任务，屏障永远闭合不了）。
    #: ``Dependencies.queue`` 在类型上是可空的，所以装配图的那一层负责先收窄 ——
    #: 在这里声明成非空，节点就不用各自判一遍，也就不会各自用不同的方式处理。
    queue: TaskQueue
    settings: Settings = field(default_factory=get_settings)
    #: 唤醒选举用的短时锁。可以是 None（测试、以及没有 Redis 的场合）——
    #: 它省的是重复劳动，不是正确性（见 ``Lock`` 的协议文档）。
    lock: Lock | None = None

    async def emit(self, task_id: str, kind: str, payload: dict[str, Any] | None = None) -> int:
        """写一条事件到 ``run_events``（SSE 的权威来源），返回 ``seq``。

        **失败不吞。** 这张表是时间线的权威来源，SSE 只是快路径 ——
        少一条事件就是 UI 上一个永久的空洞，而空洞是静默的
        （客户端带 ``Last-Event-ID`` 补齐时不会发现少了一段）。
        节点的写入全是幂等的，所以让异常往上走、让节点重跑，代价比重放一次
        「已发布但没有 publish.done 事件」的 run 低得多。
        """
        seq = await self.store.append_event(task_id, kind, payload or {})
        log.info("node.event", task_id=task_id, kind=kind, seq=seq)
        return seq
