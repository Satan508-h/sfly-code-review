"""HTTP 响应形状。

**为什么这些不在 ``sfly_shared/contracts.py`` 里。** 那份契约管的是
「跨进程传的东西」—— 五个 app 都在消费，所以它必须有唯一的真相来源。
这里是**本服务对外暴露的接口形状**：它随 API 演进，而消费它的是浏览器和
回放脚本，不是另一个 Python 进程。放进 contracts 会让「加一个只在 UI 里用的
字段」变成一次跨服务契约变更。

（``RunEvent`` / ``RunRow`` / ``ReviewReport`` 是例外 —— 它们本来就是契约，
这里直接复用，不重新包一层。包一层的话，加字段要在两个地方各加一次，
而漏掉一次不会有任何报错。）
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from sfly_shared.contracts import DeliveryRow, ReviewReport, RunEvent, RunRow

#: 一次 webhook 投递的四种结局。**和 ``DeliveryStatus`` 不是一回事** ——
#: 那个是账本里落库的状态（多一个 ``received``，表示「还没干完」）。
WebhookOutcome = Literal["accepted", "duplicate", "ignored", "rejected"]


class WebhookResponse(BaseModel):
    """webhook 的响应体。

    ``status`` 是**给人看的结论**，不是 HTTP 状态码的同义词：重复投递返回
    200 + ``duplicate``（这不是错误，是幂等性在工作），而被拒返回 4xx。
    把去重报告成错误会让 GitHub 的重投机制和我们的去重机制打架。
    """

    status: WebhookOutcome
    detail: str = ""
    delivery_id: str | None = None
    task_id: str | None = None
    #: 前端和脚本用它接着查这次 run，省得自己拼路径
    run_url: str | None = None


class RunListResponse(BaseModel):
    runs: list[RunRow]
    #: 本次返回的条数。**刻意不给 total** —— 那需要一条 ``COUNT(*)``，
    #: 而它在这个规模上没有任何用处，只是让人以为存在分页。
    count: int


class RunDetailResponse(BaseModel):
    run: RunRow
    #: 还没跑到 ``finalize`` 时是 ``None``（run 在跑、或者失败了）
    report: ReviewReport | None = None
    events: list[RunEvent] = Field(default_factory=list)
    """时间线。**冷启动时一次拿全**（前端首屏不该为了时间线再开一条 SSE），
    之后带 ``Last-Event-ID`` 接到 SSE 上，靠 ``seq`` 去重。"""


class DeliveryListResponse(BaseModel):
    """收到的 webhook 投递。**「我的 PR 为什么没被审」的第一个查询。**"""

    deliveries: list[DeliveryRow]
    count: int
