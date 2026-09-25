"""图状态的两条纪律 —— 约定 #3（累加器用 dict）和「状态里只放 JSON」。

这两条都不是风格问题：前者的失效方式是**重放时悄悄多出一条记录**，
后者的失效方式是 checkpoint 读回来时打印一行没人会看的弃用警告。
所以它们各自有一条测试。
"""

from __future__ import annotations

import json

import pytest

from factories import bootstrap, demo_patches
from sfly_agent.state import initial_state, merge_by_key

pytestmark = pytest.mark.unit


def test_merge_by_key_is_idempotent() -> None:
    """**这是约定 #3 的全部机制。**

    LangGraph 恢复时会重新执行节点，于是同一个键会被写第二次。
    dict 按键合并天然幂等：同一个键合并两次得到同一个 dict。
    换成 list 累加器（``operator.add``）就会得到两条重复记录 ——
    而那不会有任何报错，只是时间线上多一行、屏障统计多算一个。
    """
    once = merge_by_key({"security": "1-1"}, {"performance": "1-2"})
    twice = merge_by_key(once, {"performance": "1-2"})

    assert once == twice
    assert len(twice) == 2


def test_merge_by_key_lets_the_new_value_win() -> None:
    """同一键的新值覆盖旧值 —— 重放时重算出来的值应当生效。"""
    assert merge_by_key({"security": "1-1"}, {"security": "1-9"}) == {"security": "1-9"}


def test_merge_by_key_does_not_mutate_its_inputs() -> None:
    """LangGraph 会把 reducer 的返回值当成新状态；改到入参上，
    等于把上一次检查点的内容一起改了 —— 那是「恢复之后状态莫名其妙变了」
    这类问题的来源。"""
    left = {"a": 1}
    right = {"b": 2}
    merge_by_key(left, right)

    assert left == {"a": 1}
    assert right == {"b": 2}


def test_the_initial_state_is_pure_json() -> None:
    """**状态里只放 JSON。**

    实测：``JsonPlusSerializer`` 确实能把 ``FilePatch`` / ``Rule`` 这些契约
    对象存进 checkpoint 再读回来，但**每次读都会打印**
    「Deserializing unregistered type ... This will be blocked in a future
    version」。要在将来继续可用，就得维护一份会漂移的白名单。
    换成纯 JSON 之后这件事就不存在了。

    这条测试是那道边界的守门人：谁往状态里塞一个 pydantic 对象，它会红。
    """
    state = initial_state(bootstrap(file_patches=demo_patches()))

    json.dumps(state)  # 不可 JSON 序列化的话这一步就抛了
    assert isinstance(state["bootstrap"], dict)
    assert all(isinstance(p, dict) for p in state["bootstrap"]["file_patches"])
    assert state["task_id"] == state["bootstrap"]["task_id"]


def test_the_initial_state_carries_the_whole_bootstrap() -> None:
    """整包搬进来而不是拆成一个个标量字段。

    拆开会多出一份「契约有哪些字段」的拷贝，而它的失效方式最难发现：
    契约里加了字段、这里忘了搬，那个字段在图里永远是空的，且不报错。
    """
    msg = bootstrap(pr_title="给查询接口加上分页", pr_author="contributor")
    state = initial_state(msg)

    assert state["bootstrap"]["pr_title"] == "给查询接口加上分页"
    assert state["bootstrap"]["pr_author"] == "contributor"
    assert state["bootstrap"]["idempotency_key"] == msg.idempotency_key


def test_the_initial_state_serialises_enums_to_plain_strings() -> None:
    """``requested_workers`` 存成字符串列表。

    ``WorkerType`` 是 ``str`` 的子类，所以 ``json.dumps`` 能过 ——
    但存进去的是枚举实例，读回来的是字符串，两者在 ``in`` 和 ``==`` 上的
    行为不完全一样（枚举成员名 vs 值的比较）。在边界上一次性转掉。
    """
    from sfly_shared.contracts import WorkerType

    state = initial_state(bootstrap(requested_workers=[WorkerType.STYLE]))

    assert state["bootstrap"]["requested_workers"] == ["style"]
    assert all(type(w) is str for w in state["bootstrap"]["requested_workers"])
