"""``/api/runs`` —— 读 run 的状态、报告和时间线。

**全部只读。** 这里没有任何一条路径会创建或修改 run —— 那件事的唯一入口是
``review_bootstrap`` 流（编排层的 ``ingest`` 节点），而这是个刻意的边界：

如果 API 也能建 run，那么「谁负责让这个 run 有 ``deadline_at``」就有了两个
答案，而超时扫描器只认 ``dispatched``/``waiting`` —— 一条建出来但从未被
编排器接手的 ``queued`` run 会**永远停在那里**，扫不到、也没人知道。
只读让「run 的生命周期属于编排器」这件事没有第二种解释。

推论：投递之后立刻 ``GET /api/runs/{task_id}`` 可能 **404**。
run 要等编排器消费到那条 bootstrap 才存在（正常在百毫秒级）。
客户端的正确做法是重试到 200 再开流，见 ``routes/events.py``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from sfly_api.deps import require_deps
from sfly_api.schemas import RunDetailResponse, RunListResponse
from sfly_bus.factory import Dependencies

router = APIRouter(tags=["runs"])


@router.get("/runs", response_model=RunListResponse)
async def list_runs(
    deps: Annotated[Dependencies, Depends(require_deps)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunListResponse:
    """最近跑过的 run，按 ``task_id`` 倒序。

    ULID 前 48 位是毫秒时间戳，所以**主键索引就是时间索引** ——
    不需要额外的 ``created_at`` 排序或索引（见 ``001_init.sql`` 里的说明）。
    """
    runs = await deps.store.list_runs(limit=limit, offset=offset)
    return RunListResponse(runs=runs, count=len(runs))


@router.get("/runs/{task_id}", response_model=RunDetailResponse)
async def get_run(
    task_id: str,
    deps: Annotated[Dependencies, Depends(require_deps)],
) -> RunDetailResponse:
    """一个 run 的全部：状态 + 报告 + 时间线。

    时间线一次给全，前端首屏不用再开一条 SSE。之后接上 SSE 时带
    ``?after=<最大的 seq>`` 即可 —— SSE 的游标就是 ``seq``，两处是同一条查询。
    """
    run = await deps.store.get_run(task_id)
    if run is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"run {task_id} 不存在。如果刚刚才投递，可能是编排器还没消费到那条 bootstrap —— 重试即可。"
            ),
        )
    return RunDetailResponse(
        run=run,
        report=await deps.store.get_report(task_id),
        events=await deps.store.events_since(task_id, 0),
    )
