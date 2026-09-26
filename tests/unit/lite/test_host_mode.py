"""精简模式的装配 —— 一个进程里五条角色，共享**同一份**依赖。

这个文件钉住的是一件**错了不会报错**的事：精简模式里 API 必须用宿主
（``sfly_lite``）那一份 ``Dependencies``，而不是自己再开一套。

各开一套的后果不是异常，是「webhook 返回 202，然后永远不会发生任何事」——
消息投进了 API 自己那个 ``InMemoryQueue``，而 ``GraphRunner`` 在另一个上等。
两个队列都是好的，各自也都工作正常，所以没有任何一层会报警：日志干净、
健康检查全绿、HTTP 状态码全对。这正是本项目里最难查的一类故障的形状。

所以这里的断言刻意是「**它没有被调用**」（用一个调用即爆炸的替身），
而不是「调用之后结果碰巧对」—— 后者在回归发生的当天仍然是绿的。

真实装配的端到端证据在别处：``python -m sfly_lite`` 起来之后投一条 webhook，
12 条事件无缺口（见 README 的 M10 一节）。单测只能证明「这几条线接对了」，
证明不了「整条链路通」。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from sfly_api import main as api_main
from sfly_api.main import create_app
from sfly_bus.factory import Dependencies
from sfly_lite.__main__ import _context, _task_error
from sfly_shared.aio import run

pytestmark = pytest.mark.unit


def _deps(*, queue: object | None = object()) -> Dependencies:
    """最小可用的 ``Dependencies``。

    ``postgres`` 那一栏给 None：这几条路径用不到它，而类型上它要的是
    ``PostgresPool`` —— 这里 cast，**不想**为了测试把字段改成可空，
    那会让生产代码多出一个永远不会发生的分支。
    """
    return Dependencies(
        postgres=cast("Any", None),
        store=cast("Any", None),
        queue=cast("Any", queue),
        lock=None,
    )


class _RecordingHeartbeat:
    """只记「有没有人调 start」。见模块文档：断言的是没被调用。"""

    def __init__(self) -> None:
        self.started = 0

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        return None


def test_a_hosted_app_opens_no_dependencies_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """宿主模式下，lifespan 不该去开依赖，也不该去关心跳。

    两件事在同一个测试里，因为它们其实是同一件事：这个 app 在一个**已经拥有
    进程级资源**的宿主里跑，它自己什么都不该拥有。
    """
    hit = _RecordingHeartbeat()

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(
            "宿主模式自己开了依赖 —— 那会得到第二个 InMemoryQueue，"
            "于是 run 永远不会开始，而 webhook 仍然返回 202"
        )

    monkeypatch.setattr(api_main, "open_dependencies", _boom)
    monkeypatch.setattr(api_main, "_heartbeat", hit)

    deps = _deps()
    app = create_app(deps=deps)
    # TestClient 用 ``with`` 才会跑 lifespan —— 不带 with 的写法在本仓库其它
    # 测试里也有（它们不关心启动路径），这里必须带。
    with TestClient(app):
        assert app.state.deps is deps, "lifespan 把注入的依赖换掉了"

    assert hit.started == 0, "宿主模式不该动心跳：心跳属于宿主进程，不属于这个 app"


def test_an_unhosted_app_keeps_owning_its_dependencies() -> None:
    """默认形态（完整模式）没有变 —— ``deps`` 不给就不进宿主模式。

    这条是防「为了让精简模式跑起来，把完整模式悄悄改掉」的。
    """
    app = create_app()
    assert getattr(app.state, "host_deps", False) is False


def test_the_lite_context_refuses_a_deps_without_a_queue() -> None:
    """没有队列时**当场报错**，而不是让节点各自判空。

    ``NodeContext.queue`` 声明非空（没有队列图就跑不动，屏障永远闭合不了），
    而 ``Dependencies.queue`` 在类型上可空。收窄必须在装配这一层做一次 ——
    留给六个节点各做一次，就会得到六种不同的处理方式。
    """
    with pytest.raises(RuntimeError, match="队列"):
        _context(_deps(queue=None), github=None)


def test_task_error_tells_cancellation_apart_from_failure() -> None:
    """``_task_error`` 的三档：正常返回 / 被取消 / 真的抛了。

    取消那一档是重点：``Task.exception()`` 对被取消的任务**会抛**
    ``CancelledError``。所以「顺手取一下异常文本」这个写法的后果是——在本来
    就已经出事的那条路径上再抛一个异常，而它盖住的正是我们想看的那条日志。
    """

    async def _scenario() -> tuple[str | None, str | None, str | None]:
        async def _ok() -> None:
            return None

        async def _bad() -> None:
            raise ValueError("炸了")

        async def _slow() -> None:
            await asyncio.sleep(60)

        ok = asyncio.create_task(_ok())
        bad = asyncio.create_task(_bad())
        slow = asyncio.create_task(_slow())
        # 让前两个跑完，再把第三个取消 —— 三种终态各来一个
        await asyncio.sleep(0.01)
        slow.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await slow
        return _task_error(ok), _task_error(bad), _task_error(slow)

    # 走 sfly_shared.aio.run 而不是 asyncio.run：Windows 上的循环选择统一由它
    # 决定（见那个模块）。这里没有数据库，但没理由为测试维护第二条路径。
    ok, bad, slow = run(_scenario())

    assert ok is None, "正常返回不是错误"
    assert bad == "ValueError: 炸了", "异常要带类型名 —— 光有文本不好搜"
    assert slow is None, "取消不是错误：那是我们在 finally 里自己干的"
