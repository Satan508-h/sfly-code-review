"""webhook 载荷 → ``BootstrapMessage``。

这个文件里最值钱的一条是 ``test_the_recorded_fixture_matches_the_real_diff``：
它拿**提交进仓库的那份 fixture** 走一遍完整转换，再和直接解析
``fixtures/security_demo.diff`` 的结果逐字段比对。

为什么它值钱：``/pulls/{n}/files`` 的 ``patch`` 字段没有文件头，而
``parse_unified_diff`` 需要那三行 —— 少了它会**静默返回空**（M1 踩过：
Mock 报出「0 条发现」，测试不失败，只是什么也没验证到）。这条测试让
「fixture 的形态」和「转换函数的假设」互为对方的证据：
改坏任何一边，比对就会失败。
"""

from __future__ import annotations

import json

import pytest

from factories import DEMO_DIFF, FIXTURES_DIR, api_files_from_diff, webhook_payload
from sfly_api.github_payload import (
    REVIEW_ACTIONS,
    PayloadError,
    bootstrap_from_payload,
    delivery_context,
    patches_from_files,
)
from sfly_shared.contracts import BootstrapMessage, WorkerType
from sfly_shared.diff import parse_unified_diff

pytestmark = pytest.mark.unit

TASK_ID = "01JTESTRUN0000000000000000"

#: 待测的那份「录制载荷」。**读文件而不是用工厂现造一份** ——
#: 仓库里那份是被 `scripts/replay_webhook.py` 真正发出去的字节，
#: 用工厂造等于测了个和线上无关的对象。
RECORDED = json.loads((FIXTURES_DIR / "webhook_pr.json").read_text(encoding="utf-8"))


def _bootstrap(
    payload: dict[str, object], *, event: str = "pull_request"
) -> tuple[BootstrapMessage | None, str]:
    return bootstrap_from_payload(payload, event=event, task_id=TASK_ID, max_patch_chars=8_000)


# --------------------------------------------------------------------------- #
# 录制载荷 ↔ 真实 diff
# --------------------------------------------------------------------------- #


def test_the_recorded_fixture_matches_the_real_diff() -> None:
    """fixture 里那个 ``files`` 数组就是 ``security_demo.diff`` 的另一种写法。"""
    msg, reason = _bootstrap(RECORDED)
    assert reason == ""
    assert msg is not None

    expected = parse_unified_diff((FIXTURES_DIR / DEMO_DIFF).read_text(encoding="utf-8")).patches
    got = msg.file_patches

    assert [p.path for p in got] == [p.path for p in expected]
    for actual, want in zip(got, expected, strict=True):
        assert actual.additions == want.additions
        assert actual.deletions == want.deletions
        # **变更行集合必须逐字一致** —— inline 评论只能锚定在它们上面
        assert actual.changed_lines == want.changed_lines
        assert actual.language == want.language
        assert actual.is_new_file == want.is_new_file


def test_the_round_trip_survives_a_hunk_only_patch() -> None:
    """反向转换（diff → API 形状）真的把文件头去掉了。

    如果 ``api_files_from_diff`` 顺手把 ``diff --git`` 那几行也留下了，
    上面那条比对**同样会通过** —— 于是它就验证不了「缺头能不能补回来」
    这件事。这条断言把那一层单独钉住。
    """
    files = api_files_from_diff()
    assert files, "演示 diff 里应该有文件"
    for entry in files:
        assert entry["patch"].startswith("@@"), f"{entry['filename']} 的 patch 带着文件头"
        assert "diff --git" not in str(entry["patch"])


# --------------------------------------------------------------------------- #
# 字段映射
# --------------------------------------------------------------------------- #


def test_the_payload_fields_land_where_they_should() -> None:
    msg, _ = _bootstrap(RECORDED)
    assert msg is not None
    assert msg.repo_id == "demo/sfly-playground"
    assert msg.repo_node_id == "R_kgDOAbcdef"
    assert msg.pr_number == 42
    assert msg.pr_author == "contributor"
    assert msg.pr_title == "重构用户接口并加上备份入口"
    assert msg.installation_id == 987654
    assert msg.base_sha == "1" * 40
    # 空表示「全部」—— GitHub 不知道我们有几种 Worker（见 plan 节点）
    assert msg.requested_workers == []
    assert msg.file_patches[0].changed_lines, "变更行集合不该是空的"


def test_the_idempotency_key_is_computed_not_taken_from_the_payload() -> None:
    """载荷里就算带了 ``idempotency_key`` 也不看它。

    用它等于把「同一个提交只审一次」这件事交给上游 —— 而算错的方向
    两个都很糟（算宽了漏审、算窄了重复花钱），且都是静默的。
    """
    payload = dict(RECORDED)
    payload["idempotency_key"] = "1111111:9:deadbeef"
    msg, _ = _bootstrap(payload)
    assert msg is not None
    assert msg.idempotency_key == f"demo/sfly-playground:42:{msg.head_sha}"


def test_a_renamed_file_keeps_the_new_path() -> None:
    """重命名时 GitHub 给 ``filename``（新名）+ ``previous_filename``（旧名）。

    评论必须锚在新路径上 —— 拿旧路径去调 GitHub 的评论接口会被拒绝
    （那个文件在 diff 里不存在了）。这里构造的就是那种最容易被忽略的输入：
    两个名字都在载荷里，取错一个不会有任何报错。
    """
    files = [
        {
            "filename": "app/new_name.py",
            "previous_filename": "app/old_name.py",
            "status": "renamed",
            "additions": 1,
            "deletions": 1,
            "patch": "@@ -1,2 +1,2 @@\n import os\n-x = 1\n+x = 2",
        }
    ]
    patches, skipped = patches_from_files(files, max_patch_chars=8_000)
    assert skipped == {}
    assert [p.path for p in patches] == ["app/new_name.py"]


# --------------------------------------------------------------------------- #
# 不触发审查的那些情况（都必须**不是错误**）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("action", ["closed", "labeled", "edited", "converted_to_draft", "assigned"])
def test_actions_we_do_not_care_about_are_ignored_not_rejected(action: str) -> None:
    msg, reason = _bootstrap(webhook_payload(action=action))
    assert msg is None
    assert action in reason


def test_a_ping_is_answered_with_200() -> None:
    """GitHub 建 webhook 时会发一次 ``ping``。

    回 4xx 的话，配置页会显示一个红色的 ✗，而人会以为是接线错了 ——
    实际上我们只是不需要对它做任何事。
    """
    msg, reason = _bootstrap(RECORDED, event="ping")
    assert msg is None
    assert "ping" in reason


def test_a_draft_pr_waits() -> None:
    """草稿 PR 是「还没写完」。等 ``ready_for_review`` 再审 ——
    提前审既花钱，又会在作者还没准备好时留下评论。
    """
    msg, reason = _bootstrap(webhook_payload(draft=True))
    assert msg is None
    assert "草稿" in reason


def test_every_review_action_is_accepted() -> None:
    """反向断言：``REVIEW_ACTIONS`` 里的每一个都必须真的能触发。"""
    for action in REVIEW_ACTIONS:
        msg, reason = _bootstrap(webhook_payload(action=action))
        assert msg is not None, f"{action} 被拒了：{reason}"


# --------------------------------------------------------------------------- #
# 坏载荷
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("payload", "missing"),
    [
        ({"action": "opened", "files": []}, "repository.full_name"),
        (
            {"action": "opened", "repository": {"full_name": "a/b"}, "files": []},
            "pull_request.head.sha",
        ),
        (
            {
                "action": "opened",
                "repository": {"full_name": "a/b"},
                "pull_request": {"head": {"sha": "x"}},
            },
            "pull_request.number",
        ),
    ],
)
def test_a_broken_payload_says_which_field_is_missing(payload: dict[str, object], missing: str) -> None:
    """错误信息里必须带上**字段路径**。

    这条消息是「GitHub 说投递成功但我们什么也没发生」的唯一线索，
    而「invalid payload」帮不上任何忙。
    """
    with pytest.raises(PayloadError) as excinfo:
        _bootstrap(payload)
    assert missing in str(excinfo.value)


def test_an_irrelevant_action_is_ignored_before_the_payload_is_validated() -> None:
    """顺序是：先看事件类型和动作，再看载荷长得对不对。

    这不是随手定的。GitHub 会为**很多**我们不关心的动作发载荷
    （``labeled`` / ``assigned`` / ``edited``...），它们的字段形状各不相同 ——
    在动作之前校验的话，一次 ``labeled`` 就可能因为某个我们读的字段
    在这个动作里不存在而被判成「载荷坏了」，账本上留下一片红色的 rejected，
    而实际上什么也没坏。
    """
    msg, reason = _bootstrap({"action": "labeled"})  # 什么字段都没有
    assert msg is None
    assert "动作" in reason


def test_a_non_array_files_field_is_refused() -> None:
    """``files`` 是个字符串时必须报错，而不是被当成序列去迭代。

    这里是 ``/pulls/{n}/files`` 的响应，而那个接口返回的是数组 ——
    真写成字符串的话，迭代它会拿到一堆单字符，然后静默地审 0 个文件。
    """
    payload = webhook_payload()
    payload["files"] = "app/db.py"
    with pytest.raises(PayloadError):
        _bootstrap(payload)


def test_files_without_a_patch_are_skipped_with_a_reason() -> None:
    """GitHub 对二进制、以及改动过大的文件**不返回 patch 字段**。

    跳过可以，但要留下原因 —— 「审了 3 个文件」和「有 5 个文件、2 个审不了」
    是两件不同的事，前者会被读成「这个 PR 很干净」。
    """
    files = [
        {"filename": "logo.png", "status": "modified", "additions": 0, "deletions": 0},
        {
            "filename": "app/db.py",
            "status": "modified",
            "additions": 1,
            "deletions": 1,
            "patch": "@@ -1,2 +1,2 @@\n import os\n-x = 1\n+x = 2",
        },
    ]
    patches, skipped = patches_from_files(files, max_patch_chars=8_000)
    assert [p.path for p in patches] == ["app/db.py"]
    assert "logo.png" in skipped
    assert "补丁" in skipped["logo.png"]


def test_a_payload_with_only_unreviewable_files_yields_no_patches() -> None:
    """全是二进制文件 → 补丁为空。

    那会让 ``plan`` 把 run 标成 ``skipped`` 而不是产出一份「0 条发现」的报告 ——
    后者会被读成「审查通过」，而实际上什么都没看。
    """
    msg, _ = _bootstrap(webhook_payload(files=[{"filename": "a.png", "status": "added"}]))
    assert msg is not None
    assert msg.file_patches == []


# --------------------------------------------------------------------------- #
# 账本上下文
# --------------------------------------------------------------------------- #


def test_the_delivery_context_survives_a_broken_payload() -> None:
    """**不做校验**：被拒的投递也要能记下自己属于哪个 PR。

    「哪条投递被拒了」正是排查时第一个问题，而带着字段路径的 400 说明了
    「为什么拒」，这一条补充「拒的是谁」。
    """
    repo_id, pr_number = delivery_context({"repository": {"full_name": "a/b"}, "number": 7})
    assert (repo_id, pr_number) == ("a/b", 7)
    assert delivery_context({}) == ("", None)


def test_worker_types_are_not_invented_by_the_payload() -> None:
    """载荷说什么 Worker 都无关 —— 派发哪些是 ``plan`` 节点的事。

    （这条断言存在的意义是防止有人「顺手」把载荷里的某个字段接到
    ``requested_workers`` 上：那会让审查范围变成外部可控。）
    """
    payload = webhook_payload()
    payload["requested_workers"] = ["style"]
    msg, _ = _bootstrap(payload)
    assert msg is not None
    assert msg.requested_workers == []
    assert set(WorkerType) == {WorkerType.SECURITY, WorkerType.PERFORMANCE, WorkerType.STYLE}
