"""SSE 事件流的收流逻辑。

这里测的是**收流条件**，也就是这个模块唯一会出错的地方。它有三个容易写错的点，
每一个都用一条测试钉住：

1. 终态之后**不能立刻收流** —— ``publish`` 先写状态、后写事件，
   中间那一步之差会让客户端永远看不到 ``run.finished``。
2. ``id`` 必须是 ``seq`` —— 它是重连时 ``Last-Event-ID`` 的来源，
   用别的东西当 id，重连会从错误的位置继续，而那种错误只在断线时才出现。
3. 游标解析的三种输入（头、查询参数、垃圾）都要有明确的行为。

轮询间隔与宽限期在测试里被调小（改的是**模块属性**，函数里读的正是它）——
否则每条测试都要等 5 秒，而它验证的东西和「等多久」毫无关系。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any, cast

import pytest

import sfly_api.sse as sse
from factories import DEFAULT_TASK_ID, event, run_row
from sfly_api.sse import event_stream, frame, parse_cursor
from sfly_bus.base import RunStore
from sfly_shared.contracts import RunEvent, RunRow, RunStatus

pytestmark = pytest.mark.unit


class _FakeStore:
    """只实现流要用的两个方法。

    ``on_poll`` 让测试能在两次轮询之间改状态 —— 「事件比状态晚到」这种
    时序问题**只能这样构造**：先让流看到终态，再把事件塞进去。
    """

    def __init__(
        self,
        run: RunRow | None,
        events: list[RunEvent] | None = None,
        *,
        on_poll: Callable[[int], None] | None = None,
    ) -> None:
        self.run = run
        self.events = list(events or [])
        self.on_poll = on_poll
        self.polls = 0

    async def events_since(self, task_id: str, after_seq: int) -> list[RunEvent]:
        return [e for e in self.events if e.seq > after_seq]

    async def get_run(self, task_id: str) -> RunRow | None:
        self.polls += 1
        if self.on_poll is not None:
            self.on_poll(self.polls)
        return self.run


def _data(frame_dict: dict[str, str]) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(frame_dict["data"])
    return parsed


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """把轮询间隔和宽限期调小。见模块文档 —— 它们与要验证的性质无关。"""
    monkeypatch.setattr(sse, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(sse, "TERMINAL_GRACE_S", 0.15)


async def _collect(store: _FakeStore, *, after: int = 0, limit: int | None = None) -> list[dict[str, str]]:
    """收流。``limit`` 用来在流不会自己结束的场景下拿前 N 条。"""
    out: list[dict[str, str]] = []
    # cast：``_FakeStore`` 只实现流用到的两个方法，而 ``event_stream`` 要的是
    # 整个 ``RunStore`` 协议。用 cast 而不是把假对象补全 —— 补全一个用不到的
    # 方法只是多一处会漂移的地方。
    stream: AsyncIterator[dict[str, str]] = event_stream(
        cast("RunStore", store), DEFAULT_TASK_ID, after_seq=after
    )
    async for item in stream:
        out.append(item)
        if limit is not None and len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- #
# 帧
# --------------------------------------------------------------------------- #


def test_a_frame_uses_the_seq_as_its_id() -> None:
    """``id`` = ``seq``。见模块文档第 2 条。"""
    f = frame(event(17, "worker.result", worker_type="security"))
    assert f["id"] == "17"
    assert f["event"] == "worker.result"
    # data 是整条 RunEvent（含它自己的 payload 字段），不是只把 payload 展开 ——
    # 客户端拿到的是完整的事件对象，和 ``GET /api/runs/{id}`` 里那个形状一致。
    assert _data(f)["payload"]["worker_type"] == "security"
    assert _data(f)["seq"] == 17


def test_the_data_is_a_single_line() -> None:
    """``data`` 必须是单行 JSON。

    SSE 允许多行 data，但每一行都要自己带 ``data:`` 前缀 —— 手写时漏一个，
    那一行就会被当成新的字段名，客户端解析出一个残缺的对象。
    """
    f = frame(event(1, "aggregate.done", findings=3, degraded=False))
    assert "\n" not in f["data"]
    assert "\r" not in f["data"]


# --------------------------------------------------------------------------- #
# 游标
# --------------------------------------------------------------------------- #


def test_the_header_wins_over_the_query_parameter() -> None:
    """重连时浏览器带的是头，那才是「客户端已经收到哪了」的权威答案。"""
    assert parse_cursor("12", "3") == 12
    assert parse_cursor(None, "3") == 3
    assert parse_cursor("12", None) == 12


@pytest.mark.parametrize("bad", ["", "   ", "abc", "-5", "1.5", None])
def test_a_bad_cursor_falls_back_to_zero(bad: str | None) -> None:
    """解析不了就当 0（全量重放），**不报错**。

    游标错了的正确处置是重发：客户端按 ``seq`` 去重本来就该做。
    报错会让一个手抖的 URL 变成一片空白，而空白看起来像「这个 run 没有事件」。
    """
    assert parse_cursor(bad, None) == 0


# --------------------------------------------------------------------------- #
# 收流
# --------------------------------------------------------------------------- #


async def test_a_finished_run_replays_everything_and_closes() -> None:
    store = _FakeStore(
        run_row(status=RunStatus.PUBLISHED),
        [event(3), event(7, "run.finished")],
    )
    frames = await asyncio.wait_for(_collect(store), timeout=3)
    assert [f["id"] for f in frames] == ["3", "7"]


async def test_the_cursor_skips_what_the_client_already_has() -> None:
    store = _FakeStore(run_row(status=RunStatus.PUBLISHED), [event(3), event(7), event(9)])
    frames = await asyncio.wait_for(_collect(store, after=3), timeout=3)
    assert [f["id"] for f in frames] == ["7", "9"]


async def test_an_event_written_after_the_terminal_status_is_still_delivered() -> None:
    """**这条是宽限期存在的全部理由。**

    ``publish`` 先写 ``status=published``、后写 ``run.finished``，
    两步之间有真实的窗口。没有宽限期的话，客户端在窗口里连上来会看到
    「终态 + 没有新事件」，于是立刻收流 —— 而那条事件**永远送不出去**，
    症状是时间线上缺最后一行，看起来还挺正常。
    """
    store = _FakeStore(run_row(status=RunStatus.AGGREGATING), [event(1)])

    def _finish(poll: int) -> None:
        # 第 1 次轮询看到的是「还在聚合」；第 2 次：状态翻成终态但事件还没落库；
        # 第 3 次：事件到了 —— 这正是真实世界里那两步的时间差
        if poll == 2:
            store.run = run_row(status=RunStatus.PUBLISHED)
        elif poll == 3:
            store.events.append(event(2, "run.finished"))

    store.on_poll = _finish
    frames = await asyncio.wait_for(_collect(store), timeout=3)
    assert [f["id"] for f in frames] == ["1", "2"]
    assert frames[-1]["event"] == "run.finished"


async def test_a_running_run_keeps_the_stream_open_and_delivers_what_arrives_later() -> None:
    """还没结束的 run 不能收流。

    「状态不是终态」这个条件很容易被写成「拿到事件就收」（因为大多数时候
    事件就是跟着状态一起来的）—— 那样一来，一次审查会在 ``run.created``
    之后就关掉流，而之后的十条事件全部丢失。

    构造：第 1 次轮询什么也没有，第 2 次才出现一条事件。能收到它，
    就说明流跨过了轮询边界还活着。
    """
    store = _FakeStore(run_row(status=RunStatus.WAITING), [])
    store.on_poll = lambda poll: store.events.append(event(5, "worker.result")) if poll == 2 else None

    frames = await asyncio.wait_for(_collect(store, limit=1), timeout=3)
    assert [f["id"] for f in frames] == ["5"]
    assert store.polls >= 2, "流在第 2 次轮询之前就结束了"


async def test_a_run_that_disappeared_closes_the_stream() -> None:
    """run 被删了（``purge_older_than`` / 手工清库）—— 收流比空转好。"""
    store = _FakeStore(None, [])
    frames = await asyncio.wait_for(_collect(store), timeout=3)
    assert frames == []


async def test_the_grace_period_does_not_outlive_a_terminal_run_forever() -> None:
    """终态 + 没有新事件 → 宽限期一到就收流。

    收不了的话，每次看一个已完成的 run 都会留下一条永不结束的连接。
    """
    store = _FakeStore(run_row(status=RunStatus.FAILED), [event(1)])
    loop = asyncio.get_running_loop()
    started = loop.time()
    frames = await asyncio.wait_for(_collect(store), timeout=3)
    elapsed = loop.time() - started

    assert len(frames) == 1
    # 读的是**模块属性**而不是 import 进来的那个名字：`_fast` 改的是模块属性，
    # 而 import 进来的名字在导入那一刻就被绑定死了（改不动）。
    # 这个区别在测试里很容易搞混，症状是「断言的是一个从没生效过的默认值」。
    assert elapsed >= sse.TERMINAL_GRACE_S
    assert elapsed < 2.0, "收流拖得太久，宽限期大概没生效"
