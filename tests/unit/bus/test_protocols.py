"""协议实现的一致性与命名约定。

这里有两类测试，分量不一样：

* **结构一致性**（前几条）—— mypy 其实已经在编译期把这件事查了：
  ``InMemoryQueue`` 少一个方法、签名对不上，``test_memory_queue.py`` 里的
  ``QueueContract[InMemoryQueue]`` 就会报错。这里再用 ``isinstance`` 兜一遍
  是因为 ``@runtime_checkable`` 只查「有没有这个名字」、不查签名 ——
  而**协程函数和普通函数在运行时长得一样**：一个忘记写 ``async def`` 的
  ``ack()`` 会让调用方拿到一个未被 await 的协程对象，什么都不报，
  只是那条消息永远留在 PEL 里。
* **约定 #4 的可执行化**（最后两条）—— 全项目最中心的那条约定，
  在此之前只活在一句注释里。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from contracts.queue_contract import bootstrap
from sfly_bus.base import (
    CONSUMER_GROUPS,
    STREAMS,
    WORKER_TYPES,
    Lock,
    MessageHandle,
    TaskQueue,
    group_for,
)
from sfly_bus.memory import InMemoryLock, InMemoryQueue
from sfly_bus.redis_streams import RedisLock, RedisStreamsQueue
from sfly_shared.contracts import WorkerType

#: 一个没人监听的 Redis 地址。**这里刻意连不上任何东西** ——
#: 下面几条测的是「形状对不对」，不是「能不能跑」，而两个实现的构造函数
#: 都不产生网络 IO（连是 ``start()`` 才做的事），所以不需要真的 Redis。
#: 这也让这几条测试留在单测层（无 Docker、毫秒级）。
DEAD_URL = "redis://127.0.0.1:1/15"

# --------------------------------------------------------------------------- #
# 结构一致性
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_memory_queue_satisfies_the_task_queue_protocol() -> None:
    assert isinstance(InMemoryQueue(), TaskQueue)


@pytest.mark.unit
def test_redis_queue_satisfies_the_task_queue_protocol() -> None:
    """Redis 实现也必须满足协议 —— 不需要真的 Redis 就能查。

    ``@runtime_checkable`` 只查「有没有这个名字」、不查签名，所以它抓不到
    「``reclaim`` 少了个参数」这类漂移。真正查签名的是 mypy 和那份共用契约，
    这条是第三道、也是最便宜的一道：对着一个连不上的地址构造出来即可。
    """
    assert isinstance(RedisStreamsQueue(DEAD_URL), TaskQueue)


@pytest.mark.unit
def test_memory_lock_satisfies_the_lock_protocol() -> None:
    assert isinstance(InMemoryLock(), Lock)


@pytest.mark.unit
def test_redis_lock_satisfies_the_lock_protocol() -> None:
    assert isinstance(RedisLock(DEAD_URL), Lock)


@pytest.mark.unit
def test_both_implementations_expose_the_same_maintenance_methods() -> None:
    """两种实现各自的自省接口**必须同名同义**。

    它们不在 Protocol 里（契约测试用不上），但排查手法的价值恰恰在于
    「两种模式用的是同一套动作」—— 名字一分叉，线上出问题时就会有人去
    ``redis-cli`` 里数一遍 PEL，而精简模式那边根本没有这个动作。
    """
    for name in ("pending_count", "trim", "reclaim", "lag"):
        assert hasattr(InMemoryQueue(), name), f"内存实现缺 {name}"
        assert hasattr(RedisStreamsQueue(DEAD_URL), name), f"Redis 实现缺 {name}"


@pytest.mark.unit
async def test_the_handle_satisfies_the_message_handle_protocol() -> None:
    q = InMemoryQueue()
    await q.start()
    try:
        await q.publish_bootstrap(bootstrap())
        handle, _ = await anext(q.consume_bootstrap())
        assert isinstance(handle, MessageHandle)
        await handle.ack()
    finally:
        await q.close()


# --------------------------------------------------------------------------- #
# 命名约定：两种实现必须是同一套名字
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_stream_names_are_pinned() -> None:
    """这四个名字会出现在日志、``XRANGE`` 排查、Redis 的键列表和文档里。

    改名不会有任何测试失败，只会让「线上查到的东西」和「文档里写的名字」
    对不上 —— 所以在这里钉住。
    """
    assert STREAMS == {
        "bootstrap": "review_bootstrap",
        "tasks": "review_tasks",
        "results": "review_results",
        "dead_letter": "dead_letter",
    }


@pytest.mark.unit
def test_group_name_is_derived_from_worker_type() -> None:
    """``--scale worker-security=3`` 能工作，全靠组名能被三个副本独立算出来。"""
    assert group_for("security") == "security-group"
    assert group_for(WorkerType.STYLE) == "style-group"


@pytest.mark.unit
def test_every_worker_type_gets_a_group() -> None:
    """往 ``WorkerType`` 里加一个值、忘了别处，是「看起来最无害的改动」。

    漏掉的后果是那个新 Worker 的任务永远没人消费 —— 而 ``lag`` 会一直涨，
    没有任何地方会报错。
    """
    assert set(WORKER_TYPES) == set(WorkerType)


@pytest.mark.unit
def test_the_dead_letter_stream_has_no_consumer_group() -> None:
    """死信**刻意**不建消费者组。

    消费组的语义是一读一 ack、游标只往前挪，所以用 ``XREADGROUP`` 去读死信，
    读完那条就从组的视角消失了。而死信的全部价值就是事后能回看 ——
    正确的访问方式是只读的 ``XRANGE dead_letter - +``。
    """
    assert "dead_letter" not in CONSUMER_GROUPS


# --------------------------------------------------------------------------- #
# 约定 #4：只有 factory.py 分支后端变量
# --------------------------------------------------------------------------- #

#: 那两个只能出现在工厂里的设置项。
_BACKEND_ATTRS = frozenset({"queue_backend", "lock_backend"})

#: 唯一允许判断它们的文件。
_FACTORY = Path("packages/bus/sfly_bus/factory.py")

#: 扫描范围。刻意**不含** tests/ —— 这个文件自己就写了这些名字。
_SCAN_ROOTS = ("packages", "apps")


def _backend_branches(source: str) -> list[tuple[int, str]]:
    """源码里所有「拿后端设置做判断」的位置，``(行号, 属性名)``。

    用 AST 而不是 grep。注释和文档字符串里会大量出现这两个名字 ——
    它们在解释这条约定本身 —— grep 会把它们全算成违规，于是这条测试不是
    被人加一堆 noqa 就是被删掉，两种情况都比没有测试更糟。

    覆盖 ``if`` / ``elif`` / 三元表达式 / ``match`` / 推导式的 ``if`` ——
    分支长成什么样不重要，「拿它做判断」这件事才重要。
    """
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        tests: list[ast.expr] = []
        if isinstance(node, ast.If | ast.IfExp):
            tests.append(node.test)
        elif isinstance(node, ast.Match):
            tests.append(node.subject)
        elif isinstance(node, ast.comprehension):
            tests.extend(node.ifs)
        for test in tests:
            for sub in ast.walk(test):
                if isinstance(sub, ast.Attribute) and sub.attr in _BACKEND_ATTRS:
                    # 行号取判断表达式自己的：``ast.comprehension`` 根本没有 lineno
                    hits.append((test.lineno, sub.attr))
    return hits


@pytest.mark.unit
def test_the_branch_detector_actually_finds_branches() -> None:
    """守卫本身也要被测。

    一个坏掉的守卫（路径错了、AST 用法过时了）会让下面那条测试**永远通过**,
    而它守的是全项目最中心的那条约定。静默失效的守卫比没有守卫更糟 ——
    它让人以为有人在守。
    """
    assert _backend_branches("if s.queue_backend == 'redis':\n    pass\n") == [(1, "queue_backend")]
    assert _backend_branches("x = s.queue_backend\n") == [], "读取不算分支"
    assert _backend_branches("# if s.lock_backend == 'memory': 说明\n") == [], "注释不算"
    assert _backend_branches("if s.mode == 'lite':\n    pass\n") == [], "别的设置项不在这条约定的范围里"
    assert _backend_branches("match s.lock_backend:\n    case 'redis': pass\n") == [(1, "lock_backend")]
    assert _backend_branches("xs = [x for x in y if s.queue_backend in {'a'}]\n") == [(1, "queue_backend")]
    assert _backend_branches("y = 1 if s.queue_backend else 2\n") == [(1, "queue_backend")]


@pytest.mark.unit
def test_only_the_factory_branches_on_the_transport_backend() -> None:
    """**全项目最中心的一条约定，在这里变成可执行的。**

    一旦某个节点或 Worker 开始判断「我用的是不是 Redis」，两种拓扑就跑在
    不同的代码路径上，「一套代码」从事实退化成宣传 —— 而且不会有任何东西报错。

    注：这条检查抓的是「拿设置做判断」。真正的防线仍然是评审 ——
    换个写法（比如把 ``s.queue_backend`` 存进变量再判断）就绕过去了。
    它是烟雾报警器，不是防火墙。
    """
    root = Path(__file__).resolve().parents[3]
    offenders: list[str] = []
    scanned = 0

    for top in _SCAN_ROOTS:
        for path in sorted((root / top).rglob("*.py")):
            relative = path.relative_to(root)
            if relative == _FACTORY:
                continue
            scanned += 1
            source = path.read_text(encoding="utf-8")
            for lineno, attr in _backend_branches(source):
                offenders.append(f"{relative.as_posix()}:{lineno} 判断了 .{attr}")

    # 路径解析一旦失效，rglob 会返回空、测试会「通过」—— 这里挡住那种情况
    assert scanned > 10, f"只扫到 {scanned} 个文件，扫描路径大概是错的"

    assert offenders == [], (
        "只有 packages/bus/sfly_bus/factory.py 允许分支队列/锁后端，"
        "以下位置违反了约定 #4：\n  " + "\n  ".join(offenders)
    )
