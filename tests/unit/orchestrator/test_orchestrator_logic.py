"""编排层里**不需要图、不需要数据库**的那部分判断。

跑一条完整的图要 Redis + Postgres（那些在 ``tests/integration/orchestrator/``）。
但有两个判断是纯的、而且错了很难发现，所以它们在这里单独钉住：
「这次要跑哪几个 Worker」和「plan 之后走哪条边」。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from factories import DEMO_DIFF, FIXTURES_DIR, bootstrap, demo_patches
from sfly_orchestrator.graph import NODES, route_after_plan
from sfly_orchestrator.nodes.plan import planned_workers
from sfly_shared.contracts import WorkerType

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# 派发哪些 Worker
# --------------------------------------------------------------------------- #


def test_an_empty_request_means_all_workers() -> None:
    """``requested_workers`` 为空是**常态** —— GitHub 的 webhook 不知道我们有
    几种 Worker。空表示「全部」，而且是从 ``WorkerType`` 枚举推导出来的：
    加第四个 Worker 时这里自动跟上，不需要有人记得改。

    （这正是 ``base.py`` 里 ``WORKER_TYPES`` 那段注释说的同一件事。）
    """
    assert planned_workers(bootstrap(requested_workers=[])) == list(WorkerType)


def test_an_explicit_request_is_respected() -> None:
    assert planned_workers(bootstrap(requested_workers=[WorkerType.STYLE])) == [WorkerType.STYLE]


def test_the_order_follows_the_enum_not_the_request() -> None:
    """请求里的顺序不决定派发顺序。

    派发顺序会影响 ``worker.dispatched`` 事件在时间线上的排列，
    从而影响截图和评测的可复现性 —— 而请求方（webhook / 前端）没有任何理由
    关心这件事。按枚举固定下来。
    """
    requested = [WorkerType.STYLE, WorkerType.SECURITY]
    assert planned_workers(bootstrap(requested_workers=requested)) == [
        WorkerType.SECURITY,
        WorkerType.STYLE,
    ]


def test_duplicate_requests_do_not_dispatch_twice() -> None:
    """同一个 Worker 被请求两次只派发一次 —— 否则两条任务会走同一个消费者组，
    而 ``worker_results`` 的主键会把第二条结果吸收掉：看起来一切正常，
    只是白烧了一次模型调用。"""
    requested = [WorkerType.SECURITY, WorkerType.SECURITY]
    assert planned_workers(bootstrap(requested_workers=requested)) == [WorkerType.SECURITY]


# --------------------------------------------------------------------------- #
# plan 之后走哪条边
# --------------------------------------------------------------------------- #


def test_plan_routes_to_dispatch_when_there_is_work() -> None:
    assert route_after_plan({"planned_workers": ["security"]}) == "dispatch"


def test_plan_ends_the_run_when_there_is_nothing_to_review() -> None:
    """没有可审文件时提前结束，**不产出一份「0 条发现」的报告** ——
    后者会被读成「审查通过」，而实际上什么都没看。
    """
    assert route_after_plan({"planned_workers": []}) == "end"
    assert route_after_plan({}) == "end"


def test_the_pipeline_order_is_declared_once() -> None:
    """节点顺序只此一份 —— 日志、README、图本身都读它。

    两处定义的话，某天有人在图里插了一个节点却忘了改文档，
    README 上的架构图就从「介绍」变成了「误导」。
    """
    assert NODES == ("ingest", "plan", "dispatch", "wait", "aggregate", "finalize", "publish")


# --------------------------------------------------------------------------- #
# 命令行入口：从 JSON / diff 构造一条 bootstrap
#
# 这两个函数是 CLI 的输入边界，**错了不会有堆栈**：一个字段漏了会变成
# 「图里某个东西永远是空的」，一个键算错了会被契约层拒绝而报错信息很长。
# 所以它们值得在单测层被钉住（不需要数据库 —— 正因为如此才放在这里）。
# --------------------------------------------------------------------------- #


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "task": None,
        "diff": None,
        "pr": 42,
        "repo": "demo/sfly-playground",
        "new": False,
        "timeout": None,
        "quiet": True,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_a_recorded_payload_can_be_replayed(tmp_path: Path) -> None:
    """``--task`` 读一份记录下来的 payload 并**逐字段校验**。

    这条路径的用途是重放：M6 的 ``replay_webhook.py`` 会用它把一个真实的
    webhook 载荷原样喂给编排器，从而在不碰 GitHub 的情况下复现一次线上问题。
    """
    from sfly_orchestrator.__main__ import EXIT_OK, _load_task

    original = bootstrap(file_patches=demo_patches())
    path = tmp_path / "payload.json"
    path.write_text(json.dumps(original.model_dump(mode="json")), encoding="utf-8")

    msg, code = _load_task(_args(task=str(path)))

    assert code == EXIT_OK
    assert msg is not None
    assert msg.idempotency_key == original.idempotency_key
    assert msg.file_patches == original.file_patches


def test_a_tampered_payload_is_refused_with_a_readable_message(tmp_path: Path) -> None:
    """幂等键对不上时**必须拒绝**，而不是「悄悄用算出来的那个」。

    键不匹配意味着上游某处算错了，而错误的幂等键两个方向都很糟：算宽了会让
    不同的提交被误判成同一个 run（漏审），算窄了会让同一个 PR 被反复审查
    （重复评论 + 重复花钱）。两者都是静默的。
    """
    from sfly_orchestrator.__main__ import EXIT_BAD_INPUT, _load_task

    payload = bootstrap().model_dump(mode="json")
    payload["idempotency_key"] = "someone-elses-key"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    msg, code = _load_task(_args(task=str(path)))
    assert msg is None
    assert code == EXIT_BAD_INPUT


def test_a_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    from sfly_orchestrator.__main__ import EXIT_BAD_INPUT, _load_task

    msg, code = _load_task(_args(task=str(tmp_path / "nope.json")))
    assert msg is None
    assert code == EXIT_BAD_INPUT


def test_a_diff_becomes_a_bootstrap_with_a_content_addressed_sha() -> None:
    """``--diff`` 模式下 ``head_sha`` = diff 内容的哈希。

    于是「**同一份 diff 只投一次**」（幂等键 = repo:pr:head_sha），
    改了 diff 就是一次新的审查 —— 这正是 GitHub 上 ``head_sha`` 的行为，
    只是这里没有 git 可以问。
    """
    from sfly_orchestrator.__main__ import EXIT_OK, _load_task

    msg, code = _load_task(_args(diff=str(FIXTURES_DIR / DEMO_DIFF)))

    assert code == EXIT_OK
    assert msg is not None
    assert len(msg.head_sha) == 12
    assert msg.file_patches, "解析出来的补丁不该是空的"
    assert msg.pr_number == 42
    assert msg.repo_id == "demo/sfly-playground"


def test_the_same_diff_always_gets_the_same_key() -> None:
    from sfly_orchestrator.__main__ import _load_task

    first, _ = _load_task(_args(diff=str(FIXTURES_DIR / DEMO_DIFF)))
    second, _ = _load_task(_args(diff=str(FIXTURES_DIR / DEMO_DIFF)))

    assert first is not None and second is not None
    assert first.idempotency_key == second.idempotency_key
    assert first.task_id != second.task_id, "每次调用给一个新的 run id，幂等键才是那个「同一个」"


def test_new_delivery_changes_the_key_but_keeps_everything_else() -> None:
    """``--new`` 只能通过换 ``head_sha`` 来表达「这是一次新投递」——
    幂等键由契约强制等于 ``repo:pr:head_sha``，所以没有别的入口。
    """
    from sfly_orchestrator.__main__ import _as_new_delivery

    original = bootstrap()
    fresh = _as_new_delivery(original)

    assert fresh.task_id != original.task_id
    assert fresh.head_sha != original.head_sha
    assert fresh.head_sha.startswith(original.head_sha)
    assert fresh.idempotency_key != original.idempotency_key
    # 校验器没被绕过：``head_sha`` 变了而键没跟着变的话，这里会直接抛。
    assert fresh.pr_number == original.pr_number
    assert fresh.file_patches == original.file_patches


def test_a_diff_with_nothing_reviewable_is_refused(tmp_path: Path) -> None:
    """二进制 / 没有 hunk 的 diff 要**在入口就拒绝**，而不是投一条空任务进去。

    空任务走完整条链路会得到一份「0 条发现」的报告 —— 那会被读成
    「审查通过」，而实际上什么都没看。
    """
    from sfly_orchestrator.__main__ import EXIT_BAD_INPUT, _load_task

    path = tmp_path / "binary.diff"
    path.write_text(
        "diff --git a/logo.png b/logo.png\n"
        "index 0000000..1111111 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n",
        encoding="utf-8",
    )
    msg, code = _load_task(_args(diff=str(path)))

    assert msg is None
    assert code == EXIT_BAD_INPUT
