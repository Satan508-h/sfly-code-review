"""``plan`` 节点的「代码从哪来」—— 线上那条路必须自己去拉。

GitHub 的 ``pull_request`` 事件**不带任何代码**（只有元数据），所以线上
``msg.file_patches`` 永远是空的，文件得自己调一次 ``/pulls/{n}/files``。
本地一直没暴露这件事，是因为 fixture 是**录出来的** —— 录制脚本把那次 API 的
响应一起塞进了载荷，回放时看着像一切正常。

这个文件钉的是那条分界线，三条各自对应一种「看起来正常但在骗人」：

* **载荷里有文件 → 一次网络调用都不发。** 本地、CI、回放那条路完全不受影响，
  而这是最容易不小心改坏的（顺手把下拉取写成无条件）。
* **载荷里没有 → 拉回来什么就审什么。**
* **拉不到 ≠ 没有可审的文件。** 前者抛（``GraphRunner`` 重试，用完判 failed），
  后者标 ``skipped``。把前者归到后者，等于把「我们根本没看到代码」说成
  「代码没有问题」—— 一份看起来完全正常的空报告，本项目最贵的一类 bug。
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from factories import api_files_from_diff, bootstrap, demo_patches
from sfly_agent.state import initial_state
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.nodes.plan import plan
from sfly_shared.config import Settings
from sfly_shared.contracts import RunStatus, WorkerType

pytestmark = pytest.mark.unit


class _FakeStore:
    """``plan`` 用到的那四个方法。多写一个假方法就是多一处会漂移的地方。"""

    def __init__(self) -> None:
        self.statuses: list[tuple[str, RunStatus]] = []
        self.plans: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def set_status(self, task_id: str, status: RunStatus, **_over: Any) -> None:
        self.statuses.append((task_id, status))

    async def set_plan(self, task_id: str, workers: list[WorkerType], **over: Any) -> None:
        self.plans.append({"task_id": task_id, "workers": workers, **over})

    async def append_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> int:
        self.events.append((kind, payload))
        return len(self.events)


class _FakeGitHub:
    """只实现 ``pull_files``。``calls`` 用来断言「该不该发这一枪」。"""

    def __init__(self, *, files: list[dict[str, Any]] | None = None, error: Exception | None = None) -> None:
        self.files = files or []
        self.error = error
        self.calls = 0

    async def pull_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.files


def _ctx(*, github: _FakeGitHub | None) -> NodeContext:
    return NodeContext(
        store=cast("Any", _FakeStore()),
        queue=cast("Any", None),
        github=cast("Any", github),
        settings=Settings(pr_max_files=40, per_file_patch_chars=8_000),
    )


def _store(ctx: NodeContext) -> _FakeStore:
    return cast("_FakeStore", ctx.store)


async def test_a_payload_that_already_has_files_never_touches_github() -> None:
    """回放/录制那条路一次网络调用都不发。

    这条是防「顺手把拉取写成无条件」的：那会让每一次本地演示和 CI 里
    的图测试都去连 GitHub，而症状是「测试变慢、偶尔因为限流红」——
    没人会把它和这里联系起来。
    """
    exploding = _FakeGitHub(error=AssertionError("载荷里已经有文件了，不该调 GitHub"))
    ctx = _ctx(github=exploding)
    msg = bootstrap(file_patches=demo_patches())

    out = await plan(initial_state(msg), ctx)

    assert exploding.calls == 0
    assert out["files_reviewed"] > 0


async def test_an_empty_payload_fetches_the_files_from_github() -> None:
    """线上那条路：载荷里没有文件，自己拉。

    ``api_files_from_diff`` 把 ``fixtures/`` 里那份 diff 反过来变成
    ``/pulls/{n}/files`` 的响应形状（去掉三行头、带上 status），
    所以这一次往返之后应该审到同一批文件。
    """
    gh = _FakeGitHub(files=api_files_from_diff())
    ctx = _ctx(github=gh)
    msg = bootstrap(file_patches=[])

    out = await plan(initial_state(msg), ctx)

    assert gh.calls == 1
    assert out["files_reviewed"] == len(demo_patches())
    assert out["file_patches"], "拉回来的补丁要进状态，dispatch 读的是它"
    assert _store(ctx).plans[0]["files_total"] == len(demo_patches())


async def test_no_token_and_no_files_is_a_failure_not_an_empty_report() -> None:
    """没配 token 时**抛**，而不是标 ``skipped``。

    两者的含义正好相反：``skipped`` 是「看过了，这个 PR 没有问题」，
    而这里的事实是「我们根本没看到代码」。归错的话访客看到的是一份
    完全正常的空报告。
    """
    ctx = _ctx(github=None)
    msg = bootstrap(file_patches=[])

    with pytest.raises(RuntimeError, match="GITHUB_TOKEN"):
        await plan(initial_state(msg), ctx)

    assert _store(ctx).statuses == [], "不该标任何终态 —— 重试是消费循环的事"
    kinds = [kind for kind, _payload in _store(ctx).events]
    assert kinds == ["plan.fetch_failed"]


async def test_a_failed_fetch_keeps_the_reason_in_the_timeline() -> None:
    """拉取失败要**先写事件再抛**。

    抛上去之后 ``GraphRunner`` 会重试到上限、然后把 run 标成 ``failed`` ——
    那条 run 在 UI 上只有状态没有原因。原因只能由这条事件说。
    """
    ctx = _ctx(github=_FakeGitHub(error=RuntimeError("rate limited")))
    msg = bootstrap(file_patches=[])

    with pytest.raises(RuntimeError, match="rate limited"):
        await plan(initial_state(msg), ctx)

    kinds = [kind for kind, _payload in _store(ctx).events]
    assert kinds == ["plan.fetch_failed"]
    payload = _store(ctx).events[0][1]
    assert "RuntimeError" in str(payload["reason"])
    assert "rate limited" in str(payload["error"])


async def test_files_that_arrived_but_cannot_be_reviewed_are_still_skipped() -> None:
    """反面：**真的**没有可审的东西时，``skipped`` 这条路一点都不该变。

    GitHub 对二进制文件不返回 ``patch`` 字段 —— 那是「拉到了，但没法审」，
    和「拉不到」是两件事。前者标 ``skipped`` 是对的（既省钱又诚实），
    后者必须抛。
    """
    ctx = _ctx(github=_FakeGitHub(files=[{"filename": "logo.png", "status": "modified"}]))
    msg = bootstrap(file_patches=[])

    out = await plan(initial_state(msg), ctx)

    assert out["planned_workers"] == []
    assert [status for _task, status in _store(ctx).statuses] == [RunStatus.SKIPPED]
