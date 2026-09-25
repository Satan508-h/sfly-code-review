"""SSE —— 事件的**快路径**，``run_events`` 表才是权威来源。

这个区别决定了整个文件长什么样：流不是「推送」，而是**一个带游标的轮询循环，
把结果按 SSE 的格式吐出去**。客户端断了就按 ``Last-Event-ID`` 从表里补齐，
一个事件都不会少。

### 为什么是轮询而不是 LISTEN/NOTIFY 或 Redis Pub/Sub

因为「补齐」这件事必须由一个**持久**的游标来回答，而通知机制都不持久：
NOTIFY 在没人监听时静默丢弃，Pub/Sub 更是（Redis 的 pub/sub 没有历史）。
用它们意味着要维护两套状态（通知 + 数据库游标）并保证两者一致 —— 而现在
只有一套：**``seq > after_seq`` 这一条 SQL**，断线重连和实时推送走的是同一段代码。

代价是最坏情况 1 秒的延迟。对一个要跑十几秒的审查来说是噪音级的。
（真要更低延迟，``LISTEN/NOTIFY`` 只该用来**提前唤醒**这个循环，
而不是取代它 —— 那是 M11 之后的事。）

### 收流的条件：终态**加上一个宽限期**

不能一看到终态就收流。``publish`` 是**先写状态、后写事件**的
（见 CLAUDE.md，那里解释了为什么这个顺序的失败方向更安全），
两步之间有真实的窗口 —— 期间数据库里已经是 ``published``，而
``run.finished`` 还没落库。立刻收流会让客户端**永远看不到最后那条事件**，
而且它看起来完全正常：客户端只是没有再收到东西。

所以终态之后再等 ``TERMINAL_GRACE_S``，让最后那条事件有机会落进来。
崩溃在两步之间时这一等也是有效的（等满了就收），
这正是需要宽限期而不是「等 run.finished」的原因。

### 响应头交给 sse-starlette

``X-Accel-Buffering: no`` / ``Cache-Control: no-store`` / ``Connection: keep-alive``
都是它自己设的，这里**不要**再设一遍 —— 重复的同名头会变成两行，
而有些代理看到重复头会挑第一个（不一定是我们想给的那个）。
nginx 那边 ``infra/nginx/default.conf`` 还额外关了 ``proxy_buffering``：
两处都有是刻意的，这个头对任何兼容 nginx 的代理都有效，
而那份配置只覆盖我们自己那台。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

from sfly_bus.base import RunStore
from sfly_shared.contracts import TERMINAL_STATUSES, RunEvent
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 新事件的轮询间隔。见模块文档 —— 它换来的是「断线补齐和实时推送同一段代码」。
POLL_INTERVAL_S = 1.0

#: 终态之后再多看一会儿的时长。见模块文档「收流的条件」。
TERMINAL_GRACE_S = 5.0

#: 单条流的最长寿命。防的是「客户端连着一条永远不会结束的流」——
#: 正常的 run 最长 ``RUN_DEADLINE_S``（默认 600 秒）就该有结果，
#: 到点收流让客户端重连（重连会带上 Last-Event-ID，什么也不会丢）。
MAX_STREAM_S = 1800.0


def parse_cursor(header: str | None, query: str | None) -> int:
    """解析游标。``Last-Event-ID`` 头优先，其次是 ``?after=`` 查询参数。

    **两个入口都得支持**，因为浏览器只给了其中一个：
    ``EventSource`` 在**重连**时会自动带上 ``Last-Event-ID``，
    但你没法给它设自定义头 —— 所以**首次**连接的起点只能走查询参数。
    只支持头的话，首屏（打开一个已经跑完的 run）永远是空的。

    解析不了一律当 0（全量重放），**不报错**：游标错了的正确处置是重发，
    客户端按 ``seq`` 去重本来就该做 —— 而报错会让一个手抖的 URL 变成一片空白。
    """
    for raw in (header, query):
        if raw is None or not str(raw).strip():
            continue
        try:
            value = int(str(raw).strip())
        except ValueError:
            continue
        if value > 0:
            return value
    return 0


def frame(event: RunEvent) -> dict[str, str]:
    """一条事件 → SSE 帧。

    ``id`` 必须是 ``seq``：它是客户端重连时 ``Last-Event-ID`` 的来源，
    也就是**唯一的游标**。用别的什么（时间戳、随机数）会让重连从错误的
    位置继续，而那种错误只在断线时才出现 —— 平时测不到。

    ``data`` 用单行 JSON（``model_dump_json`` 不换行）。SSE 允许多行 data，
    但每一行都要自己带 ``data:`` 前缀，手写时极易漏 —— 漏了的话那一行会被
    当成新字段名，客户端解析出一个残缺的对象。
    """
    return {"event": event.kind, "id": str(event.seq), "data": event.model_dump_json()}


async def event_stream(
    store: RunStore,
    task_id: str,
    *,
    after_seq: int = 0,
) -> AsyncIterator[dict[str, str]]:
    """从 ``after_seq`` 开始吐出这个 run 的事件，到终态（宽限后）为止。"""
    cursor = after_seq
    started = time.monotonic()
    grace_until: float | None = None
    total = 0

    while True:
        for event in await store.events_since(task_id, cursor):
            cursor = event.seq
            total += 1
            yield frame(event)

        run = await store.get_run(task_id)
        if run is None:
            # 订阅一个不存在的 run。路由已经拦过一次了，到这儿说明它在流的
            # 生命周期里被删了（purge_older_than / 手工清库）—— 收流比空转好。
            log.warning("sse.run_vanished", task_id=task_id, sent=total)
            return

        now = time.monotonic()
        if run.status in TERMINAL_STATUSES:
            if grace_until is None:
                grace_until = now + TERMINAL_GRACE_S
            elif now >= grace_until:
                log.info("sse.stream_closed", task_id=task_id, status=run.status.value, sent=total)
                return
        if now - started >= MAX_STREAM_S:
            log.warning("sse.max_lifetime", task_id=task_id, sent=total)
            return

        # ``EventSourceResponse`` 在这个空档里发心跳（``: ping``），
        # 所以 nginx / Render 的代理不会因为「太久没数据」掐掉连接。
        await asyncio.sleep(POLL_INTERVAL_S)
