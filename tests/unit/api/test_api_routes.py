"""HTTP 层的路由 —— **顺序、状态码、以及「什么时候不写库」**。

这个文件测的不是「能不能收到 webhook」，而是几件错了不会报错、只会静默做错事
的规则。每一条都对应一个具体的失败模式：

* 未验签的请求**不能写库** —— 否则任何人都能用垃圾投递灌满账本，
  更糟的是用未来的 delivery id 提前占位，让真实投递被判成重复而丢掉
* 投递失败（队列不可用）时**不能结算**那条投递 —— 结算了就等于把一次丢失
  伪装成成功，而且重投再也救不回来
* 「不关心的事件」必须是 **200** 而不是 4xx
* 重复投递必须**只投一次** bootstrap

依赖用假对象替换（``dependency_overrides``），所以整个文件是纯单测：
不连 Docker、不连数据库。真实的仓储行为在
``tests/integration/api/test_webhook_dedup.py`` 里对着真 Postgres 再跑一遍 ——
两层的分工是「这里的假对象证明路由的判断对」、
「那里证明这些判断落在真实的约束上也是对的」。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from factories import DEFAULT_TASK_ID, event, run_row, webhook_payload
from sfly_api import routes as api_routes
from sfly_api.main import create_app, get_deps
from sfly_api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign
from sfly_shared.config import Settings
from sfly_shared.contracts import (
    DeliveryRow,
    DeliveryStatus,
    ReviewReport,
    RunEvent,
    RunRow,
)

pytestmark = pytest.mark.unit

SECRET = "test-secret"


# --------------------------------------------------------------------------- #
# 假依赖
# --------------------------------------------------------------------------- #


class _FakeStore:
    """路由用到的那几个方法。**没有实现的属性会直接报 AttributeError** ——
    这是刻意的：多写一个假方法就等于多一处会漂移的地方，
    路由一旦开始用新的 store 方法，这里应该立刻炸掉提醒我们去集成层补一条。
    """

    def __init__(self) -> None:
        self.deliveries: dict[str, DeliveryRow] = {}
        self.finishes: list[tuple[str, DeliveryStatus, str | None, str | None]] = []
        self.runs: dict[str, RunRow] = {}
        self.reports: dict[str, ReviewReport] = {}
        self.events: dict[str, list[RunEvent]] = {}
        #: 记录每一次写库。验签失败时它必须仍然是空的。
        self.writes = 0

    async def record_delivery(
        self, delivery_id: str, *, event: str, repo_id: str = "", pr_number: int | None = None
    ) -> bool:
        self.writes += 1
        if delivery_id in self.deliveries:
            return False
        self.deliveries[delivery_id] = DeliveryRow(
            delivery_id=delivery_id, event=event, repo_id=repo_id, pr_number=pr_number
        )
        return True

    async def get_delivery(self, delivery_id: str) -> DeliveryRow | None:
        return self.deliveries.get(delivery_id)

    async def finish_delivery(
        self,
        delivery_id: str,
        status: DeliveryStatus,
        *,
        task_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self.writes += 1
        self.finishes.append((delivery_id, status, task_id, reason))
        row = self.deliveries[delivery_id]
        self.deliveries[delivery_id] = row.model_copy(
            update={
                "status": status,
                "task_id": task_id or row.task_id,
                "reason": reason or row.reason,
                "finished_at": datetime.now(UTC),
            }
        )

    async def release_delivery(self, delivery_id: str) -> None:
        self.writes += 1
        row = self.deliveries.get(delivery_id)
        # 和真实实现同一条约束：只释放还没结算的那一行
        if row is not None and row.status is DeliveryStatus.RECEIVED:
            del self.deliveries[delivery_id]

    async def list_deliveries(self, limit: int = 50) -> list[DeliveryRow]:
        return list(self.deliveries.values())[:limit]

    async def get_run_by_key(self, idempotency_key: str) -> RunRow | None:
        return next((r for r in self.runs.values() if r.idempotency_key == idempotency_key), None)

    async def get_run(self, task_id: str) -> RunRow | None:
        return self.runs.get(task_id)

    async def get_report(self, task_id: str) -> ReviewReport | None:
        return self.reports.get(task_id)

    async def events_since(self, task_id: str, after_seq: int) -> list[RunEvent]:
        return [e for e in self.events.get(task_id, []) if e.seq > after_seq]

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[RunRow]:
        return list(self.runs.values())[offset : offset + limit]


class _FakeQueue:
    def __init__(self, *, fail: bool = False) -> None:
        self.published: list[Any] = []
        self.fail = fail

    async def publish_bootstrap(self, msg: Any) -> str:
        if self.fail:
            raise ConnectionError("Redis 不可达（假装的）")
        self.published.append(msg)
        return "1700000000000-0"


@pytest.fixture
def store() -> _FakeStore:
    return _FakeStore()


@pytest.fixture
def queue() -> _FakeQueue:
    return _FakeQueue()


def _deps(store: _FakeStore, queue: _FakeQueue | None) -> Any:
    """``Dependencies`` 是 dataclass，postgres 那一栏路由用不到 —— 给 None。

    类型上它要的是 ``PostgresPool``，所以这里必须 cast：**不想**为了测试
    把字段改成 ``| None``（那会让生产代码多一个永远不会发生的分支）。
    """
    from sfly_bus.factory import Dependencies

    return Dependencies(postgres=cast("Any", None), store=cast("Any", store), queue=cast("Any", queue))


@pytest.fixture
def client(store: _FakeStore, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    app = create_app()
    app.dependency_overrides[get_deps] = lambda: _deps(store, queue)
    # 配置也换掉：开发机上的 .env 有 MODE / GITHUB_WEBHOOK_SECRET，
    # 不隔离的话这些测试的结果取决于本机配置。
    monkeypatch.setattr(api_routes.webhook, "get_settings", _settings)
    yield TestClient(app)
    app.dependency_overrides.clear()


def _settings(*, secret: str = SECRET, mode: str = "full") -> Settings:
    return Settings(mode=mode, github_webhook_secret=secret, per_file_patch_chars=8_000)


def _post(
    client: TestClient,
    *,
    payload: dict[str, Any] | None = None,
    delivery: str = "d-1",
    event_name: str = "pull_request",
    secret: str | None = SECRET,
    raw: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    body = raw if raw is not None else json.dumps(payload or webhook_payload()).encode("utf-8")
    h = {
        "Content-Type": "application/json",
        EVENT_HEADER: event_name,
        DELIVERY_HEADER: delivery,
        **(headers or {}),
    }
    if secret is not None:
        h[SIGNATURE_HEADER] = sign(body, secret)
    return client.post("/api/webhook", content=body, headers=h)


# --------------------------------------------------------------------------- #
# 正常路径
# --------------------------------------------------------------------------- #


def test_a_signed_delivery_is_accepted_and_published(
    client: TestClient, store: _FakeStore, queue: _FakeQueue
) -> None:
    r = _post(client)
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "accepted"
    assert body["delivery_id"] == "d-1"
    assert body["run_url"] == f"/api/runs/{body['task_id']}"

    assert len(queue.published) == 1
    msg = queue.published[0]
    # **返回给调用方的 task_id 必须就是投出去的那条消息的 task_id** ——
    # 它们分叉的话，前端拿着它去查 run 会永远 404，而投递本身一切正常。
    assert msg.task_id == body["task_id"]
    assert msg.repo_id == "demo/sfly-playground"
    assert msg.pr_number == 42
    assert msg.file_patches, "录制的载荷里应该有 4 个文件的补丁"
    assert store.deliveries["d-1"].status is DeliveryStatus.ACCEPTED
    assert store.deliveries["d-1"].task_id == msg.task_id


def test_the_delivery_row_records_which_pr_it_belonged_to(client: TestClient, store: _FakeStore) -> None:
    """账本里必须能看出「这条投递是哪个 PR 的」—— 排查的第一个问题。"""
    _post(client)
    row = store.deliveries["d-1"]
    assert (row.event, row.repo_id, row.pr_number) == ("pull_request", "demo/sfly-playground", 42)


# --------------------------------------------------------------------------- #
# 第一层去重：同一个 delivery id
# --------------------------------------------------------------------------- #


def test_the_same_delivery_three_times_publishes_once(
    client: TestClient, store: _FakeStore, queue: _FakeQueue
) -> None:
    """**M6 的验收标准。** 3 次投递 → 1 次 accepted + 2 次 duplicate，
    队列上只有一条 bootstrap。
    """
    responses = [_post(client, delivery="same-id") for _ in range(3)]

    assert [r.status_code for r in responses] == [202, 200, 200]
    assert [r.json()["status"] for r in responses] == ["accepted", "duplicate", "duplicate"]
    assert len(queue.published) == 1

    # 三次的 task_id 是同一个 —— 重复的两次也要能告诉调用方「去看哪个 run」
    task_ids = {r.json()["task_id"] for r in responses}
    assert len(task_ids) == 1


def _unsettled(delivery_id: str, *, age_s: float) -> DeliveryRow:
    """一行停在 ``received`` 的投递。``age_s`` 是它「被认领多久了」。"""
    return DeliveryRow(
        delivery_id=delivery_id,
        event="pull_request",
        received_at=datetime.now(UTC) - timedelta(seconds=age_s),
    )


def test_a_stale_unsettled_delivery_is_taken_over(
    client: TestClient, store: _FakeStore, queue: _FakeQueue
) -> None:
    """停在 ``received`` 且**已经很久没动**的投递会被接管。

    这条路径真实存在：进程在「记了账」和「投出去」之间崩掉。只认主键不看状态
    的话，一次崩溃就能让那批投递被永久跳过 —— 而表现是
    「GitHub 显示 200，但那个 PR 永远不会被审」。
    """
    store.deliveries["d-1"] = _unsettled("d-1", age_s=999)
    assert store.deliveries["d-1"].is_settled is False

    r = _post(client)

    assert r.status_code == 202
    assert r.json()["status"] == "accepted"
    assert len(queue.published) == 1


def test_an_in_flight_delivery_is_not_double_published(
    client: TestClient, store: _FakeStore, queue: _FakeQueue
) -> None:
    """**刚认领、还没结算的投递不能被接管** —— 那是有另一个请求正在处理它。

    这条是实测踩出来的：并发投递同一个 delivery id 时，5 个请求里有 4 个
    撞上主键冲突、看到那一行是 ``received``，于是「接管」并各自投了一条
    bootstrap —— 5 条消息。**只认状态不看时间，就区分不了
    「正在处理」和「死在中途」**，而这两者的正确处置正好相反。
    """
    store.deliveries["d-1"] = _unsettled("d-1", age_s=0.0)

    r = _post(client)

    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert "in flight" in r.json()["detail"]
    assert queue.published == []
    # 那个「正在处理」的记录没有被改写 —— 它还等着真正的处理者去结算它
    assert store.deliveries["d-1"].status is DeliveryStatus.RECEIVED


@pytest.mark.parametrize(
    "settled", [DeliveryStatus.ACCEPTED, DeliveryStatus.IGNORED, DeliveryStatus.REJECTED]
)
def test_a_settled_delivery_is_reported_and_not_reprocessed(
    client: TestClient, store: _FakeStore, queue: _FakeQueue, settled: DeliveryStatus
) -> None:
    store.deliveries["d-1"] = DeliveryRow(
        delivery_id="d-1", event="pull_request", status=settled, task_id="01JOLD"
    )
    r = _post(client)
    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert r.json()["task_id"] == "01JOLD"
    assert queue.published == []


# --------------------------------------------------------------------------- #
# 第二层去重：同一个提交
# --------------------------------------------------------------------------- #


def test_a_new_delivery_for_an_existing_run_is_reported_as_duplicate(
    client: TestClient, store: _FakeStore, queue: _FakeQueue
) -> None:
    """投递是新的，但幂等键撞车 —— 这一层由 ``review_runs`` 的唯一约束保证。

    真实场景：``push`` 事件和 ``pull_request`` 事件同时到达，
    两个不同的 delivery id、同一个提交。
    """
    first = _post(client, delivery="d-1").json()
    message_id = queue.published[0].idempotency_key
    store.runs[first["task_id"]] = run_row(
        task_id=first["task_id"], idempotency_key=message_id, status="waiting"
    )

    r = _post(client, delivery="d-2")

    assert r.status_code == 200
    assert r.json()["status"] == "duplicate"
    assert r.json()["task_id"] == first["task_id"]
    assert "同一个提交" in r.json()["detail"]
    assert len(queue.published) == 1, "第二次不该再投一条 bootstrap"
    assert store.deliveries["d-2"].status is DeliveryStatus.DUPLICATE


# --------------------------------------------------------------------------- #
# 不写库的那些请求
# --------------------------------------------------------------------------- #


def test_a_bad_signature_is_rejected_without_touching_the_database(
    client: TestClient, store: _FakeStore, queue: _FakeQueue
) -> None:
    """**整个文件里最要紧的一条。**

    未验签的请求能写库的话，任何人都能用垃圾投递灌满账本 ——
    更糟的是用**未来的** delivery id 提前占位，让真实投递到达时被判成重复
    而永久丢掉（把去重机制反过来当攻击面用）。
    """
    r = _post(client, secret="另一个密钥")
    assert r.status_code == 401
    assert r.json()["status"] == "rejected"
    assert store.writes == 0, "验签失败时一个字节都不该写进数据库"
    assert queue.published == []


def test_a_missing_signature_is_rejected(client: TestClient, store: _FakeStore) -> None:
    body = json.dumps(webhook_payload()).encode("utf-8")
    r = client.post(
        "/api/webhook",
        content=body,
        headers={"Content-Type": "application/json", DELIVERY_HEADER: "d-1"},
    )
    assert r.status_code == 401
    assert store.writes == 0


def test_a_body_that_does_not_match_the_signature_is_rejected(client: TestClient, store: _FakeStore) -> None:
    """签名必须盖在**发出去的那串字节**上，换一个字节就不认。"""
    body = json.dumps(webhook_payload()).encode("utf-8")
    r = client.post(
        "/api/webhook",
        content=body + b"\n",
        headers={DELIVERY_HEADER: "d-1", SIGNATURE_HEADER: sign(body, SECRET)},
    )
    assert r.status_code == 401
    assert store.writes == 0


def test_a_missing_delivery_header_is_refused(
    client: TestClient, store: _FakeStore, queue: _FakeQueue
) -> None:
    """没有 delivery id 就没法去重，而「不去重的 webhook 入口」等于把
    重复审查交给运气 —— 宁可在门口拒掉。
    """
    body = json.dumps(webhook_payload()).encode("utf-8")
    r = client.post(
        "/api/webhook",
        content=body,
        headers={SIGNATURE_HEADER: sign(body, SECRET), EVENT_HEADER: "pull_request"},
    )
    assert r.status_code == 400
    assert "X-GitHub-Delivery" in r.json()["detail"]
    assert queue.published == []


def test_a_broken_json_body_is_rejected_and_recorded(client: TestClient, store: _FakeStore) -> None:
    """签名是对的、delivery 也有，但载荷不是 JSON —— 我们自己的接线问题。

    要记账：否则「GitHub 说投递成功，我们说什么也没发生」就没有任何线索。
    """
    r = _post(client, raw=b"{not json", delivery="d-1")
    assert r.status_code == 400
    assert store.deliveries["d-1"].status is DeliveryStatus.REJECTED
    assert store.deliveries["d-1"].is_settled


def test_a_payload_missing_fields_is_rejected_with_the_field_path(
    client: TestClient, store: _FakeStore
) -> None:
    payload = webhook_payload()
    del payload["repository"]
    r = _post(client, payload=payload)
    assert r.status_code == 400
    assert "repository.full_name" in r.json()["detail"]
    assert store.deliveries["d-1"].status is DeliveryStatus.REJECTED


# --------------------------------------------------------------------------- #
# 不关心的事件
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("event_name", "payload"),
    [
        ("ping", None),
        ("push", None),
        ("pull_request", webhook_payload(action="closed")),
    ],
)
def test_irrelevant_events_are_ignored_with_200(
    client: TestClient, store: _FakeStore, queue: _FakeQueue, event_name: str, payload: dict[str, Any] | None
) -> None:
    """**必须是 200。** 回 4xx 会在 GitHub 的 Recent Deliveries 里留下红叉，
    而人看到红叉就会开始忽略那个页面 —— 那是排查线上问题时最有用的一页。
    """
    r = _post(client, payload=payload, event_name=event_name)
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"
    assert queue.published == []
    assert store.deliveries["d-1"].status is DeliveryStatus.IGNORED
    assert store.deliveries["d-1"].reason


# --------------------------------------------------------------------------- #
# 投递失败
# --------------------------------------------------------------------------- #


def test_a_queue_failure_is_reported_and_the_claim_is_released(
    client: TestClient, store: _FakeStore, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """队列不可用时回 503，而且**释放这次认领**，让重投能立刻重来一次。

    两种做法都「不算丢」，但只有这一种能立刻恢复：

    * 留着那行 ``received`` → 重投要等 ``INFLIGHT_WINDOW_S`` 才被接管，
      而它其实早就死了（没有任何请求在处理它）。
    * **释放它** → 重投就是一次全新的认领，马上就能跑。

    留一行永远停在 ``received`` 的记录还有个更糟的地方：下一次排查时会
    被读成「处理过但没结果」—— 一个查不下去的状态。
    """
    queue.fail = True
    r = _post(client)
    assert r.status_code == 503
    assert "d-1" not in store.deliveries, "失败的投递不该在账本里留下半条记录"

    # 重投：这次队列好了，必须真的跑起来
    queue.fail = False
    r2 = _post(client)
    assert r2.status_code == 202
    assert r2.json()["status"] == "accepted"
    assert len(queue.published) == 1
    assert store.deliveries["d-1"].status is DeliveryStatus.ACCEPTED


# --------------------------------------------------------------------------- #
# 密钥策略
# --------------------------------------------------------------------------- #


def test_an_unconfigured_secret_is_refused_in_lite_mode(
    client: TestClient, store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """公网（lite）没配密钥 = 503。

    那里的防线是 ``demo_access_key`` 和每日成本上限，而那是**花钱**的闸，
    不是**身份**的闸：不验签的端点意味着谁都能替你花掉那笔预算。
    """
    monkeypatch.setattr(api_routes.webhook, "get_settings", lambda: _settings(secret="", mode="lite"))
    r = _post(client, secret=None)
    assert r.status_code == 503
    assert store.writes == 0


def test_an_unconfigured_secret_is_allowed_locally(
    client: TestClient, store: _FakeStore, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    """本地（full，端口只绑本机）放行 —— 这个项目的默认是零密钥可跑通全链路。

    代价是**每一个请求都记一条 warning**，且 ``/api/health`` 里回显
    ``webhook_secret: missing``：不验签这件事必须停在一个看得见的地方。
    """
    monkeypatch.setattr(api_routes.webhook, "get_settings", lambda: _settings(secret="", mode="full"))
    r = _post(client, secret=None)
    assert r.status_code == 202
    assert len(queue.published) == 1


# --------------------------------------------------------------------------- #
# 读接口
# --------------------------------------------------------------------------- #


def test_an_unknown_run_is_404_with_a_retry_hint(client: TestClient) -> None:
    """run 由**编排器**创建，所以刚投递完的那一小段窗口里它还不存在。

    404 和空流的区别就是「重试」和「永远等下去」的区别。
    """
    r = client.get("/api/runs/01JNOPE")
    assert r.status_code == 404
    assert "重试" in r.json()["detail"]


def test_run_detail_carries_the_report_and_the_timeline(client: TestClient, store: _FakeStore) -> None:
    store.runs[DEFAULT_TASK_ID] = run_row()
    store.events[DEFAULT_TASK_ID] = [event(1, "run.created")]
    r = client.get(f"/api/runs/{DEFAULT_TASK_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["run"]["task_id"] == DEFAULT_TASK_ID
    assert body["report"] is None, "还没跑到 finalize"
    assert [e["kind"] for e in body["events"]] == ["run.created"]


def test_the_delivery_log_is_readable(client: TestClient, store: _FakeStore) -> None:
    """账本回答的是「这个 delivery id 后来怎么样了」，所以重投**不改写**那一行。

    投递层的重复（同一个 delivery id）不会在账本里留下第二条记录 ——
    它连第二次写库都没有（冲突就返回了）。账本里的 ``duplicate`` 是**另一层**
    的意思：投递是新的，但那个提交已经审过了。
    """
    assert _post(client, delivery="d-1").json()["status"] == "accepted"
    assert _post(client, delivery="d-1").json()["status"] == "duplicate"

    r = client.get("/api/deliveries")
    assert r.status_code == 200
    assert r.json()["count"] == 1
    assert r.json()["deliveries"][0]["status"] == "accepted"
