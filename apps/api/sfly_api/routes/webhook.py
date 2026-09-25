"""``POST /api/webhook`` —— GitHub 进来的唯一入口。

### 顺序是这条路由的全部内容

    1. 验签（不碰数据库）
    2. 解析 JSON（不碰数据库）
    3. 认领这次投递（第一次写库）
    4. 构造 bootstrap → 查重 → XADD（第二次写库）
    5. 了结这次投递

每一步的顺序都有理由，而且**错了不会报错，只会静默地做错事**：

* **验签必须在任何写库之前。** 未认证的请求能写库的话，任何人都能用垃圾
  投递灌满 ``webhook_deliveries`` —— 更糟的是，用未来的 delivery id 提前占位，
  真实的投递到达时会被判成重复而**被丢掉**。这是把去重机制反过来当攻击面用。
* **认领在干活之前。** 并发到达的两条同 id 投递，都想「先查有没有、没有就写」，
  于是两条都读到「没有」。主键冲突是唯一能在这里定胜负的东西。
* **查重在 XADD 之前。** 不是为了正确性（正确性在 ``review_runs`` 的
  幂等键唯一约束上，编排器也认这个键），是为了**答案**：调用方能立刻知道
  「这个提交已经审过了」，而不是收到一个 202 然后什么也没发生。
* **了结在最后。** 中途崩掉时那一行停在 ``received``，而 ``received``
  **可以被下一次重投接管** —— 这是唯一一条能让「记账了但没干成」被救回来的路径。
  所以 CAS 到这里就返回 5xx：让调用方重投，而不是把一次丢失伪装成成功。

### 关于「密钥没配」

``GITHUB_WEBHOOK_SECRET`` 默认为空（这个项目的默认是**零密钥可跑通全链路**），
所以本地 ``docker compose up`` 之后 webhook 是不验签的 —— 但只在 **full 模式**
（本地 compose，端口也只绑在本机）下放行，并且每个请求记一条 warning。

``lite`` 模式（Render，公网）**直接 503**。那里的防线是
``demo_access_key`` 和每日成本上限，而那是**花钱**的闸，不是**身份**的闸：
一个不验签的端点意味着谁都能替你花掉那笔预算。用 ``mode`` 而不是新加一个开关，
是因为它恰好就是「这个进程有没有暴露在公网」的代理变量 —— 多一个开关就多一个
配错的机会，而配错的方向是「看起来配了，实际没验」。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse

from sfly_api.deps import require_deps
from sfly_api.github_payload import PayloadError, bootstrap_from_payload, delivery_context
from sfly_api.schemas import DeliveryListResponse, WebhookResponse
from sfly_api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, verify
from sfly_bus.factory import Dependencies
from sfly_shared.config import get_settings
from sfly_shared.contracts import DeliveryRow, DeliveryStatus
from sfly_shared.ids import new_task_id
from sfly_shared.logging import bind_task, get_logger

log = get_logger(__name__)

router = APIRouter(tags=["webhook"])

#: 「另一个请求正在处理同一个投递」的判定窗口（秒）。
#:
#: 一次处理从认领到结算只有几次 IO（毫秒级），所以正常并发永远落在窗口内。
#: 而崩溃留下的那行 ``received`` 是一个**再也不会动**的时间戳 ——
#: 时间因此是区分「在处理」和「死在中途」的唯一依据。
#:
#: 取值远大于处理耗时、又远小于人的反应时间（GitHub 上重投是手动点的）——
#: 60 秒两头都满足。
INFLIGHT_WINDOW_S = 60.0


def _age_s(row: DeliveryRow) -> float:
    """这条投递被认领多久了（秒）。"""
    return (datetime.now(UTC) - row.received_at).total_seconds()


async def _reject(deps: Dependencies, delivery_id: str, event: str, reason: str) -> None:
    """载荷坏掉时的记账。

    **必须先认领再结算**：``finish_delivery`` 是 UPDATE，而这一步发生在
    第 3 步认领**之前** —— 直接结算的话，UPDATE 影响 0 行、**不报错**，
    于是账本上什么都没有（实测踩到：那条投递在库里根本不存在）。

    只在认领成功时才结算：重复到达的坏载荷不能把一条已经了结的记录
    翻成 ``rejected`` —— 那条记录是对的，坏的是这一次的字节。
    """
    if await deps.store.record_delivery(delivery_id, event=event):
        await deps.store.finish_delivery(delivery_id, DeliveryStatus.REJECTED, reason=reason)


def _response(status_code: int, body: WebhookResponse) -> JSONResponse:
    """统一出口。**显式给每个结局一个状态码**，不用装饰器的默认值 ——
    四个结局的状态码各不相同，写在装饰器上会让人以为它们一样。
    """
    if body.task_id:
        body.run_url = f"/api/runs/{body.task_id}"
    return JSONResponse(body.model_dump(), status_code=status_code)


@router.post("/webhook", response_model=WebhookResponse)
async def receive_webhook(
    request: Request,
    deps: Annotated[Dependencies, Depends(require_deps)],
) -> JSONResponse:
    settings = get_settings()
    delivery_id = (request.headers.get(DELIVERY_HEADER) or "").strip()
    event = (request.headers.get(EVENT_HEADER) or "").strip()

    # -- 1. 验签（不碰数据库） --------------------------------------------- #
    body = await request.body()  # 必须拿到**原始字节**，见 sfly_api/webhook.py
    secret = settings.github_webhook_secret
    if not secret:
        if settings.is_lite:
            log.error("webhook.secret_missing", mode=settings.mode)
            return _response(
                503,
                WebhookResponse(
                    status="rejected",
                    detail="服务未配置 GITHUB_WEBHOOK_SECRET，拒绝未验签的公网投递",
                ),
            )
        log.warning("webhook.unsigned_accepted", delivery_id=delivery_id, mode=settings.mode)
    elif not verify(body, request.headers.get(SIGNATURE_HEADER), secret):
        log.warning("webhook.signature_rejected", delivery_id=delivery_id, bytes=len(body))
        return _response(401, WebhookResponse(status="rejected", detail="签名校验失败"))

    if not delivery_id:
        # GitHub 一定会带这个头。没有它就无法去重，而「不去重的 webhook 入口」
        # 等于把重复审查交给运气 —— 宁可拒绝一条来路不明的请求。
        return _response(
            400,
            WebhookResponse(status="rejected", detail=f"缺少 {DELIVERY_HEADER} 请求头"),
        )

    # -- 2. 解析 JSON（不碰数据库） ---------------------------------------- #
    try:
        parsed: Any = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        await _reject(deps, delivery_id, event, f"载荷不是合法 JSON：{exc}")
        return _response(400, WebhookResponse(status="rejected", detail=f"载荷不是合法 JSON：{exc}"))
    if not isinstance(parsed, dict):
        await _reject(deps, delivery_id, event, "载荷不是 JSON 对象")
        return _response(400, WebhookResponse(status="rejected", detail="载荷不是 JSON 对象"))
    payload: dict[str, Any] = parsed

    # -- 3. 认领这次投递 --------------------------------------------------- #
    repo_id, pr_number = delivery_context(payload)
    claimed = await deps.store.record_delivery(delivery_id, event=event, repo_id=repo_id, pr_number=pr_number)
    if not claimed:
        existing = await deps.store.get_delivery(delivery_id)
        if existing is not None and existing.is_settled:
            log.info(
                "webhook.delivery_duplicate",
                delivery_id=delivery_id,
                previous=existing.status.value,
                task_id=existing.task_id,
            )
            return _response(
                200,
                WebhookResponse(
                    status="duplicate",
                    detail=f"这次投递之前已经处理过（{existing.status.value}）",
                    delivery_id=delivery_id,
                    task_id=existing.task_id,
                ),
            )

        # 撞上了**没结算**的那一行。它有两种可能，而两者的正确处置正好相反：
        # 「此刻有另一个请求正在处理」和「上一次处理到一半就没了」。
        # 区分它们的唯一依据是时间 —— 一次处理从认领到结算只有几次 IO，
        # 而崩溃留下的是一个**再也不会动**的时间戳。
        if existing is not None and _age_s(existing) < INFLIGHT_WINDOW_S:
            log.info("webhook.delivery_inflight", delivery_id=delivery_id, age_s=round(_age_s(existing), 3))
            return _response(
                200,
                WebhookResponse(
                    status="duplicate",
                    detail="同一个投递正在处理中（in flight）",
                    delivery_id=delivery_id,
                ),
            )

        # 超过窗口还没结算 → 上一次真的死在中途了。**接管它**：
        # 只认主键不看状态的话，一次崩溃就能让那批投递被永久跳过
        # （账本上写「已受理」，而那个 PR 永远不会被审）。
        log.warning("webhook.delivery_taken_over", delivery_id=delivery_id, attempt="resumed")

    # -- 4. 构造 bootstrap → 查重 → 投递 ------------------------------------ #
    try:
        msg, ignored_reason = bootstrap_from_payload(
            payload,
            event=event,
            task_id=new_task_id(),
            max_patch_chars=settings.per_file_patch_chars,
        )
    except PayloadError as exc:
        # 这一条走的是**已经认领过**的路径（第 3 步），所以直接结算
        await deps.store.finish_delivery(delivery_id, DeliveryStatus.REJECTED, reason=str(exc))
        log.warning("webhook.payload_rejected", delivery_id=delivery_id, reason=str(exc))
        return _response(400, WebhookResponse(status="rejected", detail=str(exc), delivery_id=delivery_id))

    if msg is None:
        # 「不关心」不是错误：GitHub 的 Recent Deliveries 里必须显示 200，
        # 否则每次 push 到别的分支都留一个红 ✗，人就开始忽略那个页面了。
        await deps.store.finish_delivery(delivery_id, DeliveryStatus.IGNORED, reason=ignored_reason)
        # 注意别写成 ``event=event``：structlog 的第一个位置参数就叫 ``event``
        # （它是消息本身），同名 kwarg 会直接抛 TypeError。
        log.info("webhook.ignored", delivery_id=delivery_id, gh_event=event, reason=ignored_reason)
        return _response(
            200, WebhookResponse(status="ignored", detail=ignored_reason, delivery_id=delivery_id)
        )

    bind_task(msg.task_id)

    # 幂等键已经在库里 → 同一个提交审过了。这是**告知**，不是保证：
    # 并发投递时两边都可能查到「没有」，而那时唯一能定胜负的是
    # review_runs.idempotency_key 的唯一约束（编排器的 create_run 认它）。
    existing_run = await deps.store.get_run_by_key(msg.idempotency_key)
    if existing_run is not None:
        await deps.store.finish_delivery(
            delivery_id,
            DeliveryStatus.DUPLICATE,
            task_id=existing_run.task_id,
            reason=f"同一个提交已经审过（run {existing_run.task_id}，状态 {existing_run.status.value}）",
        )
        log.info(
            "webhook.run_duplicate",
            delivery_id=delivery_id,
            idempotency_key=msg.idempotency_key,
            task_id=existing_run.task_id,
        )
        return _response(
            200,
            WebhookResponse(
                status="duplicate",
                detail=f"同一个提交已经审过（{existing_run.status.value}）",
                delivery_id=delivery_id,
                task_id=existing_run.task_id,
            ),
        )

    if deps.queue is None:  # pragma: no cover —— 两种拓扑的 factory 都会给一个队列
        return _response(503, WebhookResponse(status="rejected", detail="队列未初始化"))

    try:
        stream_id = await deps.queue.publish_bootstrap(msg)
    except Exception:
        # **释放这次认领。** 账本记的是**处置结果**，而这次处置没有发生 ——
        # 留一行永远停在 ``received`` 的记录，下一次排查时会被读成
        # 「处理过但没结果」。释放之后，重投就是一次全新的认领，
        # 不需要等接管窗口（见 ``INFLIGHT_WINDOW_S`` 那段）。
        #
        # 回 5xx 而不是 200：回 200 等于把一个丢失伪装成成功。
        await deps.store.release_delivery(delivery_id)
        log.exception("webhook.publish_failed", delivery_id=delivery_id, task_id=msg.task_id)
        return _response(
            503,
            WebhookResponse(status="rejected", detail="队列不可用，请重投（这次投递没有被认领）"),
        )

    await deps.store.finish_delivery(delivery_id, DeliveryStatus.ACCEPTED, task_id=msg.task_id)
    log.info(
        "webhook.accepted",
        delivery_id=delivery_id,
        gh_event=event,
        task_id=msg.task_id,
        pr=f"{msg.repo_id}#{msg.pr_number}",
        files=len(msg.file_patches),
        stream_id=stream_id,
    )
    return _response(
        202,
        WebhookResponse(
            status="accepted",
            detail=f"已投递，审查 {len(msg.file_patches)} 个文件",
            delivery_id=delivery_id,
            task_id=msg.task_id,
        ),
    )


@router.get("/deliveries", response_model=DeliveryListResponse)
async def list_deliveries(
    deps: Annotated[Dependencies, Depends(require_deps)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DeliveryListResponse:
    """最近的 webhook 投递。**「我的 PR 为什么没被审」的第一个查询。**

    每一次投递都在这里留一行，包括被忽略（动作不关心）和被拒（载荷坏了）的 ——
    只记成功的账本，在「GitHub 说投递成功但我们什么也没发生」时答不出任何问题。
    """
    deliveries = await deps.store.list_deliveries(limit=limit)
    return DeliveryListResponse(deliveries=deliveries, count=len(deliveries))
