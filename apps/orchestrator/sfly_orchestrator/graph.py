"""图的装配 —— 七个节点连成一条线，只有一个分支。

    START → ingest → plan ─┬→ dispatch → wait → aggregate → finalize → publish → END
                           └→ END（PR 里没有可审的文件）

**只有一个分支**，而且它不通向业务逻辑，只通向「提前结束」。
状态机里每多一个分支，就必须多一份「这条边什么时候会走」的说明 ——
而这份说明会和代码一起漂移。所以能不用条件边的地方就不用：
``wait`` 的屏障判断写成节点内部的循环（见 ``nodes/wait.py``），
而不是「闭合走 aggregate / 没闭合走自己」那种两条边的写法。

### 节点为什么是闭包

``build_graph(ctx)`` 把依赖包进每个节点的签名里（节点本身是
``async def node(state, ctx)``，见 ``nodes/__init__.py``）。
LangGraph 只认单参数的节点函数，所以要在这里做一次适配。

用闭包而不是 ``functools.partial``：LangGraph 会**检查节点函数的签名**
来决定它接受什么输入，而 partial 的签名推断在各版本里行为不完全一致 ——
一个装不起来或者静默丢掉参数的节点，排查成本远高于这里多写三行。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.nodes import aggregate, dispatch, finalize, ingest, plan, publish, wait

Node = Callable[[ReviewState, NodeContext], Awaitable[dict[str, Any]]]

#: 节点顺序。给日志、测试和 README 用 —— 只此一份。
NODES: tuple[str, ...] = ("ingest", "plan", "dispatch", "wait", "aggregate", "finalize", "publish")


def build_graph(
    ctx: NodeContext, checkpointer: BaseCheckpointSaver[str] | None = None
) -> CompiledStateGraph[ReviewState]:
    """装配并编译图。

    ``checkpointer`` **是必须的**（虽然类型上可空）：没有它 ``interrupt()``
    会直接抛错，而这个项目里 ``wait`` 就是靠 ``interrupt()`` 工作的。
    类型留成可空只是为了测试能编译一张不带持久化的图来验证线路。
    """
    builder: StateGraph[ReviewState] = StateGraph(ReviewState)

    for name, node in (
        ("ingest", ingest),
        ("plan", plan),
        ("dispatch", dispatch),
        ("wait", wait),
        ("aggregate", aggregate),
        ("finalize", finalize),
        ("publish", publish),
    ):
        # ``cast`` 不是偷懒：LangGraph 的 ``add_node`` 是一个由十来种回调形状
        # 组成的联合类型重载，而 mypy **推断不出** ``NodeInputT``（参数位置上的
        # 类型变量在联合重载里没有足够信息）。实测：直接传一个具体类型的
        # ``Callable`` 一定报「没有匹配的重载」，传 ``Any`` 才能过。
        #
        # 丢掉的是这个三行包装函数的类型检查，而真正需要检查的是
        # ``nodes/`` 里那些节点的签名 —— 它们仍然是完整的。
        builder.add_node(name, cast("Any", _bind(node, ctx)))

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "plan")
    builder.add_conditional_edges("plan", route_after_plan, {"dispatch": "dispatch", "end": END})
    builder.add_edge("dispatch", "wait")
    builder.add_edge("wait", "aggregate")
    builder.add_edge("aggregate", "finalize")
    builder.add_edge("finalize", "publish")
    builder.add_edge("publish", END)

    return builder.compile(checkpointer=checkpointer)


def route_after_plan(state: ReviewState) -> str:
    """``plan`` 之后走哪条边。**只看有没有可审的文件。**

    没有可审文件时提前结束，而不是让图跑完并产出一份「0 条发现」的报告 ——
    后者会被读成「审查通过」，而实际上什么都没看。见 ``nodes/plan.py``。

    用条件边而不是让 ``plan`` 返回 ``Command(goto=END)``：Command 的跳转
    在**图的结构上不可见**（``get_graph().draw_mermaid()`` 画不出来），
    而这个项目的图是要被画进 README 的。
    """
    return "dispatch" if state.get("planned_workers") else "end"


def _bind(node: Node, ctx: NodeContext) -> Callable[[ReviewState], Awaitable[Any]]:
    """把 ``(state, ctx)`` 适配成 LangGraph 要的 ``(state)``。

    返回类型写成 ``Any`` 而不是 ``dict[str, Any]``：LangGraph 的 ``add_node``
    对节点返回类型的重载很窄（要么是整个状态、要么是它的一个子集），
    而我们的节点返回的就是普通字典 —— 声明成 ``dict[str, Any]`` 会让
    mypy 报「没有匹配的重载」，而它期待的其实是 ``NodeInputT`` 的一部分。
    """

    async def bound(state: ReviewState) -> Any:
        return await node(state, ctx)

    # 让回溯里的函数名是节点名而不是一堆 ``bound``。
    # LangGraph 内部用的是 add_node 的键名，所以这只影响我们自己的日志和调试。
    bound.__name__ = f"{getattr(node, '__name__', 'node')}_node"
    return bound


def thread_config(task_id: str) -> RunnableConfig:
    """``thread_id`` = ``task_id``。**这是断点恢复的全部线索。**

    图的状态、checkpoint、屏障查询、超时扫描，五样东西靠这一个字符串关联起来。
    正因为它承担了这么多，``GraphRunner`` 在幂等键撞车时会**改写** bootstrap 的
    ``task_id``（见那个模块）—— 让它们指向同一个 run 比让它们各自正确重要得多。
    """
    return RunnableConfig(configurable={"thread_id": task_id})
