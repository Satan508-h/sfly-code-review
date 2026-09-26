"""``GET /api/runs/{task_id}/events`` —— SSE 时间线。

### 断线补齐的完整约定（前端照着做就行）

    let cursor = 0;                       // 从详情接口拿到的最大 seq，没有就是 0
    const es = new EventSource(`/api/runs/${id}/events?after=${cursor}`);
    es.onmessage = (e) => { cursor = Number(e.lastEventId); render(JSON.parse(e.data)); };
    // 浏览器自动重连时**会带上 Last-Event-ID: <它收到的最后一个 id>**，
    // 服务端从表里补齐缺口 —— 前端不用自己写重连逻辑。

上面那个 `onmessage` 能收到**全部**事件，因为帧里不带 `event:` 字段 ——
带的话它只会触发 `addEventListener("<名字>")`，`onmessage` 一条都收不到，
而那是「连接成功、然后永远静默」这种最难查的表现。理由写在 `sse.frame()`
的文档里，`tests/unit/api/test_sse.py` 里有一条测试钉着它。

两点必须知道：

* **收到 ``run.finished`` 要自己 ``es.close()``。** EventSource 在服务端关流后
  会**自动重连**（这是它的规范行为，不是 bug），于是变成「连上 → 没有新事件 →
  收流 → 再连上」的循环。服务端没法告诉它「别连了」（SSE 协议里没有这个信号），
  所以这件事只能由客户端做。
* **``seq`` 可能重复。** 首屏从详情接口拿过一批事件、之后又开流时，
  ``?after=`` 和 ``Last-Event-ID`` 之间可能有重叠。按 ``seq`` 去重是客户端的
  责任 —— 服务端只保证**一个都不会少**，不保证一个都不多。

### 为什么 run 不存在时是 404

因为「404」和「空流」的区别就是「重试」和「永远等下去」的区别。
刚投递完的那一小段窗口里 run 还不存在（它由编排器创建），
客户端应该重试到 200 再开流 —— 而不是连上一条**永远不会有事件的流**。
第二类错误（拼错了 task_id）也只能靠 404 才看得出来。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sse_starlette.sse import EventSourceResponse

from sfly_api.deps import require_deps
from sfly_api.sse import event_stream, parse_cursor
from sfly_bus.factory import Dependencies

router = APIRouter(tags=["events"])


@router.get("/runs/{task_id}/events")
async def stream_events(
    task_id: str,
    request: Request,
    deps: Annotated[Dependencies, Depends(require_deps)],
    after: Annotated[
        int | None, Query(ge=0, description="起始游标。首次连接用它，重连时浏览器会带 Last-Event-ID")
    ] = None,
) -> EventSourceResponse:
    run = await deps.store.get_run(task_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"run {task_id} 不存在。如果刚刚才投递，可能是编排器还没消费到那条 bootstrap —— 重试即可。"
            ),
        )

    # 已经到终态的 run 也照常开流：客户端会立刻拿到全部历史事件，
    # 宽限期一过就收流。这让「事后翻一个旧 run」和「盯着一个新 run」
    # 走的是同一条代码路径 —— 不需要为「历史」单独做一个接口。
    cursor = parse_cursor(request.headers.get("Last-Event-ID"), str(after) if after is not None else None)
    return EventSourceResponse(event_stream(deps.store, task_id, after_seq=cursor))
