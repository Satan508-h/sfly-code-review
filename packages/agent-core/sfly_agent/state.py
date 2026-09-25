"""LangGraph 的图状态。

两条规则，各自对应一个已经踩到的坑。

### 1. 约定 #3：会被多个节点或重放写入的字段一律是 dict

LangGraph 在恢复时会**重新执行节点**。list 累加器（``operator.add``）遇到重放
会产生重复条目 —— 而且不会有任何报错，只是时间线上多了一条、屏障统计多算一个。
dict 按键合并天然幂等：同一个键合并两次得到同一个 dict。

实现是 :func:`merge_by_key`，挂在字段上用的是 ``Annotated``。
注意**没有 reducer 的字段是「整个覆盖」**，所以 ``dispatched`` 这类字段如果
漏了 ``Annotated``，第二个 Worker 的派发记录会把第一个挤掉 ——
而症状只是「时间线上少一行」。

### 2. 状态里只放 JSON，不放 pydantic 对象

实测：``JsonPlusSerializer``（checkpointer 的默认序列化器）确实能把
``FilePatch`` / ``Rule`` 这些契约对象存进去再读回来，**但每次读都会打印**

    Deserializing unregistered type sfly_shared.contracts.FilePatch from
    checkpoint. This will be blocked in a future version.

要在将来继续可用，就得在构造 serde 时维护一份「允许的契约类型」白名单，
而那份清单一定会漂移：给某个契约加一个字段、引入一个新类型，忘了登记只会在
运行时看到一行警告 —— 这正是最容易被忽略的那类信号。

换成纯 JSON 之后这件事就不存在了，顺带让
``SELECT checkpoint FROM checkpoints`` 直接可读（排查图状态时很有用）。
代价是节点边界上要做一次 ``model_validate``，那是显式的、看得见的成本。
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from sfly_shared.contracts import BootstrapMessage

K = str
"""状态里所有的字典键都是字符串：``worker_type`` 的值、或者 ``task_id``。
写成别名是为了让下面那一堆 ``dict[K, ...]`` 读起来有意图。"""


def merge_by_key[V](left: dict[K, V], right: dict[K, V]) -> dict[K, V]:
    """dict 累加器：按键合并，右边的赢。

    右边的赢是必要的（同一次执行内后来的写入更新），而**幂等**来自
    「同一个键写两次得到同一个值」—— 恢复时节点重跑写的就是同一个值。
    """
    return {**left, **right}


class ReviewState(TypedDict, total=False):
    """一张 run 的全部状态。

    ``total=False``：字段由不同的节点在自己那一步写入，任何一个节点看到的
    都是「到目前为止」的子集。这在图里是常态而不是缺陷 —— 与其为每个节点
    声明一个输入子类型（LangGraph 支持，但会让「谁写了什么」散在两种地方），
    不如让节点自己在开头把需要的字段取出来并校验。
    """

    # -- 身份 -------------------------------------------------------------- #
    #: ``BootstrapMessage.model_dump(mode="json")`` —— **整包，不拆成字段**。
    #:
    #: 拆成 ``repo_id`` / ``pr_number`` / ``head_sha`` 一堆标量看起来更规整，
    #: 但那会多出一份「契约有哪些字段」的拷贝，而这类拷贝的失效方式最难发现：
    #: 契约里加了字段，这里忘了搬，那个字段在图里永远是空的，且不报错。
    #: 整包放进来之后，「契约 → 状态」只剩 :func:`initial_state` 一个转换点。
    bootstrap: dict[str, Any]
    #: run 的标识。**通常等于 ``bootstrap["task_id"]``，但不总是** ——
    #: 幂等键撞上已有 run 时（GitHub 超时重投同一个 webhook），
    #: 图要跟着**已有**的那个 run 走。见 ``runner.GraphRunner``。
    task_id: str

    # -- plan -------------------------------------------------------------- #
    #: 排序 + 截断之后的补丁。三个 Worker 收到的是同一份 —— 检索到的规则才不同
    file_patches: list[dict[str, Any]]
    language: str
    #: ``worker_type`` → 检索好的规则
    rules: Annotated[dict[K, list[dict[str, Any]]], merge_by_key]
    files_total: int
    files_reviewed: int
    diff_truncated: bool
    #: ISO 字符串而不是 datetime：状态只放 JSON（见模块文档）
    deadline_at: str
    planned_workers: list[str]

    # -- dispatch ---------------------------------------------------------- #
    #: ``worker_type`` → 队列里的消息 id。运维用（``XRANGE`` 时对得上）
    dispatched: Annotated[dict[K, str], merge_by_key]

    # -- wait -------------------------------------------------------------- #
    #: ``worker_type`` → 是否已上报（成功**或失败**，见约定 #2）
    completed: Annotated[dict[K, bool], merge_by_key]
    #: deadline 之前没有上报的 Worker。**注意它和 ``missing_workers`` 不是一回事** ——
    #: 见 ``nodes/wait.py`` 里那段说明。
    deadline_missed: list[str]
    #: 报告里的「哪一路没有产出可用的结果」（含**上报了失败结果**的那些）
    missing_workers: list[str]
    degraded: bool

    # -- aggregate / finalize ---------------------------------------------- #
    #: ``ReviewReport`` 的 ``model_dump(mode="json")``
    report: dict[str, Any]


def initial_state(msg: BootstrapMessage) -> ReviewState:
    """图的入口状态：**契约 → 状态**的那道边界，也是唯一的转换点。"""
    return ReviewState(
        bootstrap=msg.model_dump(mode="json"),
        task_id=msg.task_id,
    )
