"""M6 的验收：**同一份 webhook 投 3 次，只产生 1 个 run** —— 对着真 Postgres + 真 Redis。

单测那一层（``tests/unit/api/test_api_routes.py``）用的是手写的假仓储，
它证明的是「路由的判断对不对」；这里证明的是**那些判断落在真实的约束上
仍然成立**。两者的分工是刻意的：

* 假仓储里 ``record_delivery`` 的「已存在」是我自己写的一个 dict 查询，
  而真实实现靠的是 ``PRIMARY KEY (delivery_id)`` + ``ON CONFLICT DO NOTHING``
  ——**只有在这里才能验证那个约束真的存在、真的生效**（迁移文件写错一个字母，
  单测全绿）。
* 队列那一侧同理：单测里 ``publish_bootstrap`` 是个往 list 里 append 的假函数，
  这里是真的 ``XADD``。投了三次还是只有一条消息，是 Redis 说的，不是我说的。

### 全是 async，没有 TestClient

``TestClient`` 是同步的，而 ``store`` 是异步 fixture —— 两者必须跑在**同一个
事件循环**上。同步测试里用 ``asyncio.run`` 会另起一个循环，然后拿到一个
「绑定在别的循环上」的连接池（报 ``attached to a different loop``，
而且只在某些测试里随机出现）。所以这里统一用 ``httpx.AsyncClient`` +
``ASGITransport``：整个文件都在 pytest-asyncio 那个循环里。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest

import sfly_api.sse as sse
from factories import bootstrap as make_bootstrap
from factories import webhook_payload
from postgres_support import raw_conn
from redis_support import raw_client, redis_test_url
from sfly_api import routes as api_routes
from sfly_api.main import create_app, get_deps
from sfly_api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign
from sfly_bus.base import STREAMS
from sfly_bus.postgres import PostgresRunStore
from sfly_bus.redis_streams import RedisStreamsQueue
from sfly_shared.config import Settings
from sfly_shared.contracts import BootstrapMessage, DeliveryRow, DeliveryStatus, RunStatus

pytestmark = pytest.mark.integration

SECRET = "integration-secret"


# --------------------------------------------------------------------------- #
# 装配
# --------------------------------------------------------------------------- #


@pytest.fixture
async def queue() -> AsyncIterator[RedisStreamsQueue]:
    q = RedisStreamsQueue(redis_test_url(), client_name="sfly-test-api")
    await q.start()
    try:
        yield q
    finally:
        await q.close()


def _settings() -> Settings:
    """开发机上有 ``.env``（``MODE`` / 密钥都可能被改过），不隔离的话
    测试结果会随本机配置变化。
    """
    return Settings(mode="full", github_webhook_secret=SECRET, per_file_patch_chars=8_000)


@pytest.fixture
async def client(
    store: PostgresRunStore, queue: RedisStreamsQueue, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    from sfly_bus.factory import Dependencies

    monkeypatch.setattr(api_routes.webhook, "get_settings", _settings)
    app = create_app()
    app.dependency_overrides[get_deps] = lambda: Dependencies(
        postgres=cast("Any", None), store=cast("Any", store), queue=cast("Any", queue)
    )
    # 传真实路由（而不是 base_url）—— ASGITransport 直接调 ASGI app，
    # 不起 socket，所以这一层测的还是 HTTP 语义（头、状态码、流）。
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        # 超时给死，免得一条收不住的流把整个测试会话吊住
        ac.timeout = httpx.Timeout(connect=5.0, read=15.0, write=5.0, pool=5.0)
        yield ac


async def _post(
    client: httpx.AsyncClient,
    *,
    delivery: str,
    payload: dict[str, Any] | None = None,
    event_name: str = "pull_request",
) -> httpx.Response:
    body = json.dumps(webhook_payload() if payload is None else payload).encode("utf-8")
    return await client.post(
        "/api/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            EVENT_HEADER: event_name,
            DELIVERY_HEADER: delivery,
            SIGNATURE_HEADER: sign(body, SECRET),
        },
    )


def _bootstrap_len() -> int:
    """``review_bootstrap`` 流里有几条消息。**直接问 Redis** ——
    这是唯一一个不受应用层实现影响的证据。
    """
    return int(raw_client().xlen(STREAMS["bootstrap"]))


# --------------------------------------------------------------------------- #
# 投递层去重
# --------------------------------------------------------------------------- #


async def test_the_same_delivery_three_times_produces_one_bootstrap(
    client: httpx.AsyncClient, store: PostgresRunStore
) -> None:
    """**M6 的验收标准。**"""
    responses = [await _post(client, delivery="same-id") for _ in range(3)]

    assert [r.status_code for r in responses] == [202, 200, 200]
    assert [r.json()["status"] for r in responses] == ["accepted", "duplicate", "duplicate"]
    assert _bootstrap_len() == 1, "三次投递应该只在流上留下一条消息"
    # 三次给出的 task_id 是同一个：重复的那两次也要能指向同一个 run
    assert len({r.json()["task_id"] for r in responses}) == 1


async def test_concurrent_identical_deliveries_still_produce_one_bootstrap(
    client: httpx.AsyncClient,
) -> None:
    """**并发**投递同一个 delivery id：仍然只有一条 bootstrap。

    这条是主键去重真正要防的场景 —— 先查后写的实现在这里会几条都通过
    （每个都读到「不存在」），而那正是「GitHub 超时重投 + 我们重试」
    叠加时会发生的事。
    """
    responses = await asyncio.gather(*(_post(client, delivery="race-id") for _ in range(5)))

    statuses = sorted(r.json()["status"] for r in responses)
    assert statuses == ["accepted", "duplicate", "duplicate", "duplicate", "duplicate"]
    assert _bootstrap_len() == 1
    # 只有一次是 202，其余都是 200 —— 状态码也是「谁干了活」的证据
    assert sorted(r.status_code for r in responses) == [200, 200, 200, 200, 202]


# --------------------------------------------------------------------------- #
# run 层去重
# --------------------------------------------------------------------------- #


async def test_a_different_delivery_for_the_same_commit_is_reported_as_duplicate(
    client: httpx.AsyncClient, store: PostgresRunStore
) -> None:
    """投递是新的，但提交没变 —— 由 ``review_runs.idempotency_key`` 的唯一约束兜住。

    run 是**编排器**建的。这里从流里把那条 bootstrap 读出来、手工喂给
    ``create_run``，正好走的是编排器 ``ingest`` 节点那一步 —— 于是
    「API 投递 → 编排器建 run → 第二次投递被判重复」这条链路是真的。
    """
    first = (await _post(client, delivery="d-1")).json()
    assert _bootstrap_len() == 1

    entries = raw_client().xrange(STREAMS["bootstrap"], "-", "+")
    assert entries, "流里应该有一条 bootstrap"
    _, fields = entries[0]
    assert fields is not None
    run = await store.create_run(BootstrapMessage.model_validate_json(fields["payload"]))
    assert run.task_id == first["task_id"]

    r = await _post(client, delivery="d-2")
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert r.json()["task_id"] == first["task_id"]
    assert _bootstrap_len() == 1, "第二个投递不该再往流里放消息"


# --------------------------------------------------------------------------- #
# 账本
# --------------------------------------------------------------------------- #


async def test_the_ledger_survives_a_round_trip(client: httpx.AsyncClient, store: PostgresRunStore) -> None:
    """``webhook_deliveries`` 的读写走真表 —— 包括那些可空列。"""
    await _post(client, delivery="d-1")
    await _post(client, delivery="d-1")  # 重复：不该新增行
    await _post(client, delivery="d-2", event_name="ping", payload={})

    rows: list[DeliveryRow] = await store.list_deliveries()
    by_id = {r.delivery_id: r for r in rows}

    assert set(by_id) == {"d-1", "d-2"}, "重复投递不该在账本里留下第二条记录"
    assert by_id["d-1"].status is DeliveryStatus.ACCEPTED
    assert by_id["d-1"].repo_id == "demo/sfly-playground"
    assert by_id["d-1"].pr_number == 42
    assert by_id["d-1"].task_id
    assert by_id["d-1"].finished_at is not None
    assert by_id["d-2"].status is DeliveryStatus.IGNORED
    assert by_id["d-2"].reason
    assert by_id["d-2"].pr_number is None, "ping 载荷里没有 PR 信息"


async def test_an_in_flight_delivery_is_not_taken_over(
    client: httpx.AsyncClient, store: PostgresRunStore
) -> None:
    """刚认领、还没结算 → 有另一个请求正在处理它 → **不重复投递**。

    时间是区分「正在处理」和「死在中途」的唯一依据，所以这里靠**真的等**
    来构造两者：认领之后立刻投一次（落在窗口内），再把 ``received_at``
    改到很久以前投一次（落在窗口外）。
    """
    assert await store.record_delivery("slow-id", event="pull_request") is True

    r = await _post(client, delivery="slow-id")
    assert r.status_code == 200
    assert "in flight" in r.json()["detail"]
    assert _bootstrap_len() == 0

    # 把它做成「上一次死在中途」的样子：只有时间戳能表达这件事
    with raw_conn() as conn:
        conn.execute(
            "UPDATE webhook_deliveries SET received_at = now() - interval '1 hour' WHERE delivery_id = %s",
            ["slow-id"],
        )

    r2 = await _post(client, delivery="slow-id")
    assert r2.status_code == 202
    assert r2.json()["status"] == "accepted"
    assert _bootstrap_len() == 1


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


async def _seed_finished_run(store: PostgresRunStore) -> str:
    """一条已经跑完的 run + 三条事件。"""
    run = await store.create_run(make_bootstrap())
    for kind in ("run.created", "aggregate.done", "run.finished"):
        await store.append_event(run.task_id, kind, {"node": kind})
    await store.set_status(run.task_id, RunStatus.PUBLISHED)
    return run.task_id


async def test_sse_replays_the_backlog_and_closes(
    client: httpx.AsyncClient, store: PostgresRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """终态的 run：一次性把历史事件发完就收流。

    宽限期与轮询间隔在测试里被调小 —— 它们是要验证的性质之外的等待时间。
    """
    monkeypatch.setattr(sse, "TERMINAL_GRACE_S", 0.05)
    monkeypatch.setattr(sse, "POLL_INTERVAL_S", 0.02)
    task_id = await _seed_finished_run(store)

    events: list[dict[str, Any]] = []
    async with client.stream("GET", f"/api/runs/{task_id}/events") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        # 这一个是给 nginx 的：它默认缓冲响应体，于是客户端一直收不到数据，
        # **直到连接关闭才一次性收到全部**（表现是「进度条不动，刷新一下全出来了」）
        assert response.headers["x-accel-buffering"] == "no"
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))

    assert [e["kind"] for e in events] == ["run.created", "aggregate.done", "run.finished"]
    assert [e["seq"] for e in events] == sorted(e["seq"] for e in events)


async def test_sse_with_last_event_id_resumes_from_the_cursor(
    client: httpx.AsyncClient, store: PostgresRunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Last-Event-ID 补齐**：断线重连时只补客户端缺的那些。

    这是浏览器重连时唯一会带的东西，也是「断流不留缺口」这句话的实现。
    这里刻意**不带** ``?after=``，走的就是重连那条路径。
    """
    monkeypatch.setattr(sse, "TERMINAL_GRACE_S", 0.05)
    monkeypatch.setattr(sse, "POLL_INTERVAL_S", 0.02)
    task_id = await _seed_finished_run(store)
    all_events = await store.events_since(task_id, 0)
    cursor = all_events[0].seq  # 客户端说：我收到了第一条

    seen: list[int] = []
    async with client.stream(
        "GET", f"/api/runs/{task_id}/events", headers={"Last-Event-ID": str(cursor)}
    ) as response:
        assert response.status_code == 200
        async for line in response.aiter_lines():
            if line.startswith("id: "):
                seen.append(int(line[4:]))

    assert seen == [e.seq for e in all_events[1:]], "应该只补第一条之后的事件"


async def test_sse_on_an_unknown_run_is_404(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/runs/01JNOPE/events")
    assert r.status_code == 404
