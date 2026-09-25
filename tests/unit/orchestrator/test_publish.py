"""``publish`` 节点 —— 三道闸、阶梯降级、以及失败时**不能真的失败**。

这个节点是全项目分支最多的地方，而每一个分支都对应一件在真实 GitHub 上
会发生的事（重放、写库失败、给自己的 PR 请求修改、行号不在 diff 里、
权限不足）。它们全都有一个共同的坏性质：**判错了不会报错**。

* 少一道闸 → 重复评论（用户可见的噪音）
* 多退一格 → 行内评论静默消失（看起来像「只发现了这么多」）
* 失败时抛异常 → run 停在 ``aggregating``，而扫描器只看 ``dispatched``/``waiting``，
  **没有任何东西能唤醒它**

跑真 GitHub 要密钥、要真仓库、还会在别人的 PR 上留评论，所以这一层用的是
手写的假客户端 —— 它**故意不继承** ``GitHubClient``：继承会把真实 HTTP 逻辑
一起带进来，那样测的就不是「节点怎么处理各种回答」，而是「httpx 怎么发请求」。
真实 HTTP 那一层由 ``tests/unit/agent/test_github_client.py`` 对着桩负责。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from factories import DEFAULT_TASK_ID, aggregated, bootstrap, report, run_row
from sfly_agent.aggregate.render import marker_for, render_comment
from sfly_agent.github import GitHubClient, GitHubError, GitHubValidationError
from sfly_agent.state import ReviewState
from sfly_bus.base import RunStore, TaskQueue
from sfly_orchestrator.context import NodeContext
from sfly_orchestrator.nodes.publish import MAX_INLINE, publish
from sfly_shared.contracts import AggregatedFinding, RunRow, RunStatus, Severity

pytestmark = pytest.mark.unit

PR_AUTHOR = "contributor"


# --------------------------------------------------------------------------- #
# 假的依赖
# --------------------------------------------------------------------------- #


@dataclass
class _Call:
    """一次调用。``inline`` 是条数而不是内容 —— 断言「带没带行内评论」就够了，
    内容由单独的一条测试盯。"""

    kind: str
    label: str


class _FakeGitHub:
    """假的 GitHub 客户端。

    三个开关对应阶梯上三种真实的拒绝，而它们的组合覆盖了全部降级路径：

    * ``reject_inline`` —— 行号不在 diff 里（``Line must be part of the diff``）
    * ``reject_request_changes`` —— 给自己的 PR 请求修改
    * ``dead_end`` —— review 端点整个不能用（权限配错、企业策略）
    """

    def __init__(
        self,
        *,
        login: str = "sfly-bot",
        existing: tuple[str, int] | None = None,
        marker_error: Exception | None = None,
        reject_inline: bool = False,
        reject_request_changes: bool = False,
        dead_end: bool = False,
        error: Exception | None = None,
        review_id: int | None = 4242,
    ) -> None:
        self.login = login
        self.existing = existing
        self.marker_error = marker_error
        self.reject_inline = reject_inline
        self.reject_request_changes = reject_request_changes
        self.dead_end = dead_end
        #: 每一次投递都抛这个（403 / 500 那类**不该**往下走的失败）。
        self.error = error
        self.review_id = review_id
        self.calls: list[_Call] = []
        self.bodies: list[str] = []
        self.inline_bodies: list[list[str]] = []
        #: 真的发出去了的那几次（只有没抛异常的路径会走到这里）。
        self.succeeded: list[str] = []

    async def whoami(self) -> str:
        self.calls.append(_Call("whoami", "whoami"))
        return self.login

    async def find_marker(self, repo: str, pr_number: int, marker: str) -> tuple[str, int] | None:
        self.calls.append(_Call("find_marker", "find_marker"))
        if self.marker_error is not None:
            raise self.marker_error
        return self.existing

    async def create_review(
        self,
        repo: str,
        pr_number: int,
        *,
        body: str,
        event: str,
        comments: Any = (),
    ) -> dict[str, Any]:
        # 先记后抛：调用被记下来，「试了几次」才有东西可断言。
        self.calls.append(_Call("create_review", f"review:{event.lower()}" + ("+inline" if comments else "")))
        self.bodies.append(body)
        self.inline_bodies.append([str(c.get("body", "")) for c in comments])

        if self.error is not None:
            raise self.error
        if self.dead_end:
            raise _rejected({"message": "Validation Failed"})
        if comments and self.reject_inline:
            raise _rejected({"resource": "PullRequestReviewComment", "field": "line", "code": "invalid"})
        if event == "REQUEST_CHANGES" and self.reject_request_changes:
            raise _rejected({"message": "Can not request changes on your own pull request"})
        self.succeeded.append(self.labels[-1])
        return {"id": self.review_id} if self.review_id is not None else {}

    async def create_issue_comment(self, repo: str, pr_number: int, *, body: str) -> dict[str, Any]:
        self.calls.append(_Call("create_issue_comment", "comment"))
        self.bodies.append(body)
        self.inline_bodies.append([])
        if self.error is not None:
            raise self.error
        self.succeeded.append("comment")
        return {"id": 99}

    # -- 断言用的视图 ------------------------------------------------------ #

    @property
    def labels(self) -> list[str]:
        """**尝试**过的投递，按顺序。``whoami`` / ``find_marker`` 不算。

        记的是「试过」而不是「成功」—— 「403 只试了一次」这条断言要的正是它
        （那次尝试什么也没留下，只有这里能看到它发生过）。
        """
        return [c.label for c in self.calls if c.kind in ("create_review", "create_issue_comment")]

    @property
    def posts(self) -> int:
        """真的发出去了几次。"""
        return len(self.succeeded)

    @property
    def inline(self) -> list[str]:
        """最后一次**带行内**的 review 的行内正文。"""
        for bodies in reversed(self.inline_bodies):
            if bodies:
                return bodies
        return []


def _rejected(errors: Any) -> GitHubValidationError:
    return GitHubValidationError("返回 HTTP 422", status_code=422, errors=[errors])


class _NoQueue:
    """publish 不该碰队列。

    传一个会在**被访问时**就炸掉的对象，而不是 ``None``：``None`` 只有在真被
    用到时才变成 ``AttributeError: 'NoneType' has no attribute ...``，
    而那个报错指不到「publish 越界了」这件事。
    """

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"publish 不该用队列（访问了 queue.{name}）")


class _FakeStore:
    def __init__(self, run: RunRow | None) -> None:
        self.run = run
        self.statuses: list[RunStatus] = []
        self.published: list[int] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def get_run(self, task_id: str) -> RunRow | None:
        return self.run

    async def mark_published(self, task_id: str, comment_id: int) -> None:
        self.published.append(comment_id)

    async def set_status(self, task_id: str, status: RunStatus, **kwargs: Any) -> None:
        self.statuses.append(status)

    async def append_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> int:
        self.events.append((kind, payload))
        return len(self.events)

    # -- 断言用的视图 ------------------------------------------------------ #

    @property
    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def payload(self, kind: str) -> dict[str, Any]:
        return next(payload for k, payload in self.events if k == kind)


def _state(
    *,
    findings: list[AggregatedFinding] | None = None,
    changed: dict[str, list[int]] | None = None,
    block_merge: bool = False,
    pr_author: str = PR_AUTHOR,
) -> ReviewState:
    """一份能走到 publish 的图状态。

    **``bootstrap`` 里的补丁和 ``file_patches`` 是故意不一样的**：publish 读的
    必须是后者（``plan`` 节点排序截断之后的产物），读错了会拿到一份没排过序、
    也没截断的补丁列表 —— 而它在多数情况下「也能用」，只是行号集合不同。
    """
    patches = {"app/db.py": [12]} if changed is None else changed
    rep = report(
        findings=[aggregated()] if findings is None else findings,
        block_merge=block_merge,
    )
    return ReviewState(
        task_id=DEFAULT_TASK_ID,
        report=rep.model_dump(mode="json"),
        bootstrap=bootstrap(pr_author=pr_author).model_dump(mode="json"),
        file_patches=[
            {"path": path, "patch": "@@ -1 +1 @@\n-x\n+y\n", "changed_lines": lines}
            for path, lines in patches.items()
        ],
    )


def _ctx(store: _FakeStore, github: _FakeGitHub | None) -> NodeContext:
    # cast 是必要的：假客户端**故意**不是 GitHubClient 的子类（见模块文档），
    # 而 NodeContext 的类型要的是真货。
    return NodeContext(
        store=cast(RunStore, store),
        queue=cast(TaskQueue, _NoQueue()),
        github=cast(GitHubClient, github),
    )


def _run() -> dict[str, Any]:
    """一份 ``review_runs`` 的行数据。默认**没有** comment id（闸 1 的那个字段）。"""
    return run_row().model_dump()


# --------------------------------------------------------------------------- #
# 三道闸
# --------------------------------------------------------------------------- #


async def test_a_replay_does_not_post_twice() -> None:
    """闸 1：数据库里已经有 comment id。

    LangGraph 重放节点是**正常路径**（``interrupt()`` 的语义），所以这条不是
    「异常情况的兜底」，是每次恢复都会走一遍的路。
    """
    store = _FakeStore(run=RunRow.model_validate({**_run(), "github_comment_id": 777}))
    github = _FakeGitHub()

    await publish(_state(), _ctx(store, github))

    assert github.calls == [], "已经有 comment id 了，一次请求都不该发"
    assert store.published == [777], "id 要原样带回去，状态才推得对"
    assert store.statuses[-1] is RunStatus.PUBLISHED
    assert store.payload("publish.done")["form"] == "already"


async def test_a_body_already_on_the_pull_request_is_adopted() -> None:
    """闸 2：PR 上已经有带隐藏标记的正文 —— 上次发出去了，写库失败了。

    这时数据库里的 ``github_comment_id`` 是空的，唯一能认出它的就是正文里的标记。
    没有这道闸，每次重放都会多一条评论，而重复评论是**用户可见**的噪音。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(existing=("review", 888))

    await publish(_state(), _ctx(store, github))

    assert github.posts == 0, "已经认出来了就不该再发"
    assert store.published == [888], "要把认出来的那个 id 记回去，下次才走闸 1"
    assert store.payload("publish.done")["form"] == "adopted:review"


async def test_the_marker_is_the_one_the_renderer_writes() -> None:
    """签发（render）和识别（publish）必须是同一段字符串。

    它们曾经是两处字面量 —— 那种重复的失效方式很安静：格式一改，
    新标记照常写进正文，而查找的那一边永远匹配不上，于是闸 2 变成空话，
    而「它不工作」和「没有重复可防」看起来一模一样。
    """
    body = render_comment(report())
    assert marker_for(report().task_id) in body


async def test_a_failed_marker_lookup_does_not_block_publishing() -> None:
    """查不动就当作没有，继续发。

    两种失败方向的代价不对等：查询失败说明写请求（同一个 API、同一个 token）
    几乎必然也会失败，真出现「读失败写成功」时下一次运行还有闸 1 兜着；
    而「查不动就不发」会让那个 PR **永远**拿不到评论。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(marker_error=GitHubError("连不上 GitHub"))

    await publish(_state(), _ctx(store, github))

    assert github.labels == ["review:comment+inline"]
    assert store.statuses[-1] is RunStatus.PUBLISHED


# --------------------------------------------------------------------------- #
# REQUEST_CHANGES 还是 COMMENT
# --------------------------------------------------------------------------- #


async def test_block_merge_requests_changes() -> None:
    """结论是「建议修改」时，GitHub 层面上也要说出来。"""
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(login="sfly-bot")

    await publish(_state(block_merge=True, pr_author="someone-else"), _ctx(store, github))

    assert github.labels == ["review:request_changes+inline"]


async def test_being_the_pull_request_author_downgrades_before_asking() -> None:
    """机器人账号 == PR 作者时，**先**降级，不去撞那个 422。

    GitHub 禁止对自己的 PR 请求修改。问一次「我是谁」比「发出去被拒再重发」
    少一次注定失败的请求 —— 而这一次询问的结果客户端会记住。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(login=PR_AUTHOR)

    await publish(_state(block_merge=True, pr_author=PR_AUTHOR), _ctx(store, github))

    assert github.labels == ["review:comment+inline"], "一次就该成功，不该有降级痕迹"
    assert [c.label for c in github.calls if c.kind == "whoami"] == ["whoami"]


async def test_the_author_check_is_case_insensitive() -> None:
    """GitHub 的用户名大小写不敏感（``Satan508-h`` 和 ``satan508-h`` 是同一个人）。

    比错了的后果是「预检没拦住 → 发出去被 422 → 降级」——结果还是对的，
    只是每次都多一次注定失败的请求，而日志里会出现一条吓人的 422。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(login="Satan508-H")

    await publish(_state(block_merge=True, pr_author="satan508-h"), _ctx(store, github))

    assert github.labels == ["review:comment+inline"]


async def test_a_comment_only_conclusion_never_asks_who_i_am() -> None:
    """不必发 ``REQUEST_CHANGES`` 时不要多花一次请求。

    ``whoami`` 是一次真实的 API 调用，而 ``block_merge=False`` 是大多数 run 的情况。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub()

    await publish(_state(block_merge=False), _ctx(store, github))

    assert "whoami" not in [c.label for c in github.calls]


# --------------------------------------------------------------------------- #
# 阶梯降级
# --------------------------------------------------------------------------- #


async def test_a_rejected_inline_comment_falls_back_to_the_summary() -> None:
    """行号被拒 → 去掉行内重发，**汇总正文照发**。

    这条是阶梯存在的理由：一次 422 会让**整个** review 不成立（连正文一起），
    而那些行号是按 ``changed_lines`` 筛过的 —— PR 在审查期间又推了新提交时，
    GitHub 那边的 diff 和我们手里的补丁就不是同一份了。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(reject_inline=True)

    await publish(_state(), _ctx(store, github))

    assert github.labels == ["review:comment+inline", "review:comment"]
    assert store.statuses[-1] is RunStatus.PUBLISHED
    done = store.payload("publish.done")
    assert done["form"] == "review:comment"
    assert done["inline_sent"] == 0
    assert "422" in done["reason"]


async def test_a_rejected_request_changes_falls_back() -> None:
    """别的 422（比如预检没拦住的自有 PR）也走同一条阶梯。"""
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(login="", reject_request_changes=True)

    await publish(_state(block_merge=True, pr_author="someone-else"), _ctx(store, github))

    # whoami 拿不到身份 → 照发 REQUEST_CHANGES → 被拒 → 去掉行内 → 还是被拒 → 改 COMMENT
    assert github.labels == [
        "review:request_changes+inline",
        "review:request_changes",
        "review:comment",
    ]


async def test_a_dead_review_endpoint_falls_back_to_a_plain_comment() -> None:
    """阶梯的最后一格换个端点：普通评论没有行号可以不对，失败面最小。"""
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(dead_end=True)

    await publish(_state(), _ctx(store, github))

    assert github.labels[-1] == "comment"
    assert store.payload("publish.done")["form"] == "comment"
    assert store.statuses[-1] is RunStatus.PUBLISHED


async def test_a_permission_failure_is_not_walked_down_the_ladder() -> None:
    """403 是「这次不行」，不是「这样发不行」——**只试一次**。

    退到最后一格也一样不行（普通评论要的是同一类权限），所以爬完整条阶梯
    只是几次白费的请求 + 一个更难读的日志。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(error=GitHubError("403 权限不足"))

    await publish(_state(), _ctx(store, github))

    assert github.labels == ["review:comment+inline"], "只该试第一次"
    assert github.posts == 0
    assert store.statuses[-1] is RunStatus.PUBLISH_FAILED


# --------------------------------------------------------------------------- #
# 失败：写终态，绝不向上抛
# --------------------------------------------------------------------------- #


async def test_a_failure_writes_a_terminal_state_and_does_not_raise() -> None:
    """发不出去时 run 必须落到 ``publish_failed``。

    抛异常的表现是 run 停在 ``aggregating``，而扫描器的 ``due_runs`` 只看
    ``dispatched``/``waiting`` —— **没有任何东西能唤醒它**。
    同时要发 ``run.finished``：SSE 客户端靠它收流，少了它浏览器会一直等
    （而 ``publish_failed`` 是终态，收流是对的）。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(error=GitHubError("500 炸了"))

    await publish(_state(), _ctx(store, github))  # 不抛

    assert store.statuses[-1] is RunStatus.PUBLISH_FAILED
    assert store.kinds == ["publish.failed", "run.finished"]
    assert store.payload("run.finished")["status"] == RunStatus.PUBLISH_FAILED.value
    assert store.published == [], "没发出去就不该记 comment id"


async def test_dry_run_without_a_token_is_not_a_failure() -> None:
    """没配 token 时一行 HTTP 都不发，但**状态仍是 published**。

    「这次运行没打算发评论」和「发评论失败了」是两件事：前者该被读成
    「链路跑通了」，后者才该亮红灯。本地开发和 CI 依赖的就是这个区分。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))

    await publish(_state(), _ctx(store, github=None))

    assert store.statuses[-1] is RunStatus.PUBLISHED
    done = store.payload("publish.done")
    assert done["posted"] is False
    assert done["form"] == "dry_run"
    assert "GITHUB_TOKEN" in done["reason"]


async def test_a_missing_comment_id_still_counts_as_posted() -> None:
    """评论发出去了但响应里没有 id：仍是成功，只是闸 1 失效。

    这时下一次重放要靠正文里的标记认出来（闸 2）—— 那条路是通的，
    所以这不是失败；但 ``comment_id`` 会是 None，那是排查时的线索。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub(review_id=None)

    await publish(_state(), _ctx(store, github))

    assert github.posts == 1
    assert store.published == [], "没有 id 可记"
    assert store.statuses[-1] is RunStatus.PUBLISHED
    assert store.payload("publish.done")["comment_id"] is None


# --------------------------------------------------------------------------- #
# 行内评论：只锚在变更行上，且有条数上限
# --------------------------------------------------------------------------- #


async def test_inline_comments_only_land_on_changed_lines() -> None:
    """行号不在 ``changed_lines`` 里就**不做行内**，但整条发现仍然在正文里。

    赌一把的代价是整个 review 被拒（连汇总正文一起），而一次被拒的 review
    意味着这条 PR 上什么都没留下。
    """
    findings = [
        aggregated(file="app/db.py", line=12),
        aggregated(file="app/db.py", line=99),  # 不在变更行上
        aggregated(file="app/other.py", line=1),  # 文件根本不在补丁里
    ]
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub()

    await publish(_state(findings=findings, changed={"app/db.py": [12, 40]}), _ctx(store, github))

    done = store.payload("publish.done")
    assert done["inline_sent"] == 1
    assert done["inline_skipped"] == 2, "被跳过的条数要说出来 —— 静默丢掉会让「行内只有 1 条」像个 bug"


async def test_inline_comments_are_capped_and_critical_ones_win() -> None:
    """超过上限时，被丢掉的必须是**列表尾部**。

    行内评论按严重度排过序，所以「丢掉尾部」等于「丢掉最不重要的那些」。
    不排序的话丢的是随机的 —— 而一条被丢掉的 critical 发现不会有任何报错。
    """
    findings = [
        aggregated(file="app/db.py", line=12, severity=Severity.LOW),
        *[aggregated(file="app/db.py", line=12, severity=Severity.CRITICAL) for _ in range(MAX_INLINE)],
    ]
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub()

    await publish(_state(findings=findings, changed={"app/db.py": [12]}), _ctx(store, github))

    done = store.payload("publish.done")
    assert done["inline_sent"] == MAX_INLINE
    assert done["inline_skipped"] == 1

    inline = github.inline
    assert len(inline) == MAX_INLINE
    assert all(body.startswith("🔴") for body in inline), "最严重的排在最前面，被丢掉的才是 LOW 那条"


# --------------------------------------------------------------------------- #
# 正文里的事实
# --------------------------------------------------------------------------- #


async def test_the_summary_body_is_exactly_what_finalize_rendered() -> None:
    """publish **不重新生成正文** —— 它投的就是 ``finalize`` 存下来的那一份。

    重新渲染会让「重新发布」变成一次重新聚合：报告已经落库了，
    重算一遍只会再花一次钱，而且两次的结果可能不同。
    """
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub()
    rep = report()
    state = _state()
    state["report"] = rep.model_dump(mode="json")

    await publish(state, _ctx(store, github))

    assert github.bodies[0] == rep.comment_body


async def test_the_finished_event_carries_what_the_ui_needs() -> None:
    """时间线的最后一条要能独立回答「这条评论在哪个 PR 上」。"""
    store = _FakeStore(run=RunRow.model_validate(_run()))
    github = _FakeGitHub()

    await publish(_state(), _ctx(store, github))

    done = store.payload("publish.done")
    assert done["pr_url"] == f"https://github.com/{report().repo_id}/pull/{report().pr_number}"
    assert done["comment_id"] == 4242
