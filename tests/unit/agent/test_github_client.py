"""GitHub 客户端 —— 重试策略、分页、以及状态码到处置方式的翻译。

**这个文件里最值钱的是 ``retry_delay_s`` 的那组纯函数测试。** 循环本身只有
二十行，难的是判断：哪个状态码该重试、等多久。判错一次的症状是——
限流被当成权限问题（报告永远发不出去，而日志说「token 权限不足」），
或者权限问题被当成限流（白等三轮退避，每次都必然失败）。两种都不会在本地
冒出来，因为本地既没有限流也没有权限问题。

**测试跑的是真 socket**（``tests/github_stub.py`` 在 127.0.0.1 上监听一个
随机端口）。这一层不需要 Docker 也不需要密钥，所以它属于单测档；
用它而不是 ``httpx.MockTransport`` 的理由是：重试、``retry-after``、
连接复用这些事**只在真 HTTP 上才成立**，把传输层换成假的就等于把要测的
东西一起换掉了。

### 关于 Windows 上每条测试的 0.6 秒

那条固定的开销**几乎全在 ``httpx.AsyncClient()`` 的构造里**（本机实测 0.7s，
其中约 0.25s 是加载 certifi 的 CA 包，其余是 Windows 的代理探测）。
它和 CLAUDE.md 里那条「探测类测试连 127.0.0.1:1，Windows 要等两秒」是同一类
平台差异，不是被测逻辑慢 —— CI 跑在 ubuntu-latest，那边不这样。

刻意**不**把预先建好的 client 注入进去省这几百毫秒：那样测试跑的就是一条
生产代码不走的构造路径，而「请求头到底设成了什么」只能靠另一条测试补回来。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any

import httpx
import pytest

import sfly_agent.github.client as client_mod
from github_stub import GitHubStub, recorded_repo
from sfly_agent.github import GitHubClient
from sfly_agent.github.client import _header, _reset_wait_s, _retry_after_s, retry_delay_s
from sfly_agent.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    GitHubValidationError,
)
from sfly_api.github_payload import patches_from_files
from sfly_shared.contracts import ErrorClass

pytestmark = pytest.mark.unit

#: 桩只认录制载荷里的那个仓库名。**不写死成字符串** —— 它和 fixture 里
#: 的值必须是同一个，抄一遍就等于给「哪天换了 fixture」留了一个静默的坑。
REPO = recorded_repo()
PR = 1


@pytest.fixture
def stub() -> Iterator[GitHubStub]:
    with GitHubStub() as running:
        yield running


@pytest.fixture
def fast_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """把退避基数改成 0。

    「等多久」由 :func:`retry_delay_s` 的纯函数测试逐条负责；让真请求也把
    那半秒钟睡一遍，只会让套件慢两秒，什么也没多验证。
    （和 ``tests/unit/api/test_sse.py`` 把轮询间隔改小是同一个套路。）
    """
    monkeypatch.setattr(client_mod, "BACKOFF_BASE_S", 0.0)


@pytest.fixture
async def make_client(stub: GitHubStub) -> AsyncIterator[Callable[..., GitHubClient]]:
    """造客户端，并在测试结束时把它们的连接池都关掉。

    不关的话每跑一个测试就漏一组 socket，几百个测试之后开始报
    「too many open files」—— 一个和被测逻辑毫无关系的失败。
    """
    created: list[GitHubClient] = []

    def _make(**kwargs: Any) -> GitHubClient:
        kwargs.setdefault("base_url", stub.base_url)
        kwargs.setdefault("token", "test-token")
        client = GitHubClient(**kwargs)
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


# --------------------------------------------------------------------------- #
# retry_delay_s —— 纯函数，逐条分支
# --------------------------------------------------------------------------- #


def test_the_two_kinds_of_rate_limit_are_both_recognized() -> None:
    """429 和「403 + ``x-ratelimit-remaining: 0``」是**同一件事的两种表现**。

    GitHub 的二级限流给 429，一级限流（配额用完）给的是 **403** —— 和
    「你没权限」用的是同一个状态码。只看状态码的实现会把一级限流判成权限问题，
    于是那个 run 永远不会重试，而日志里写着「token 权限不足」。
    """
    assert retry_delay_s(429, {"retry-after": "12"}, 0, now=1000.0) == 12
    assert (
        retry_delay_s(403, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1120"}, 0, now=1000.0) == 120
    )


def test_a_plain_403_is_never_retried() -> None:
    """没有 ``x-ratelimit-remaining: 0`` 的 403 是权限问题。

    重试它不只是白等 —— 每一次都会消耗一次配额，而配额是有限的。
    """
    assert retry_delay_s(403, {"x-ratelimit-remaining": "42"}, 0, now=1000.0) is None
    assert retry_delay_s(403, {}, 0, now=1000.0) is None


def test_client_errors_are_not_retried() -> None:
    """401 / 404 / 422 —— 请求本身有问题，重发一次得到同样的答复。"""
    for status in (400, 401, 404, 422):
        assert retry_delay_s(status, {}, 0, now=0.0) is None, status


def test_the_rate_limit_reset_header_is_an_epoch_not_a_delta() -> None:
    """``x-ratelimit-reset`` 是 **epoch 秒**，不是「还有几秒」。

    读成 delta 的症状最隐蔽：它总是返回一个正数、总是「合理地」小
    （``1758...`` 减出来的差值），于是退避看起来在工作 —— 只是等的时间
    和 GitHub 说的毫无关系。这里把它算成绝对时间戳，看结果对不对。
    """
    now = 1_000_000.0
    assert _reset_wait_s({"x-ratelimit-reset": str(int(now + 30))}, now) == 30

    # 已经过去的时间戳 → 0（不是负数：负数会被 asyncio.sleep 当成「立刻」，
    # 看起来一样，但传进日志里是个解释不通的 -3600）
    assert _reset_wait_s({"x-ratelimit-reset": str(int(now - 3600))}, now) == 0.0
    assert _reset_wait_s({}, now) is None


def test_a_server_error_falls_back_to_backoff() -> None:
    """5xx 没有头可以看，只能退避。抖动意味着只能断言区间。"""
    delay = retry_delay_s(503, {}, 0, now=0.0)
    assert delay is not None
    assert 0.5 <= delay <= 0.75

    # 指数增长，但**有上限** —— 没上限的话连续抖动会让 publish 睡到
    # run 的 deadline 之后，而扫描器只管 dispatched/waiting，叫不醒它
    later = retry_delay_s(503, {}, 6, now=0.0)
    assert later is not None
    assert later <= 8.25


def test_an_unparseable_retry_after_degrades_to_backoff() -> None:
    """``retry-after`` 允许写成 HTTP 日期。

    GitHub 不发那种，但**解析失败不能变成崩溃**，也不能变成一个猜出来的值 ——
    猜出来的等待时间看起来完全合理，而它会一直错下去。
    """
    delay = retry_delay_s(429, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, 0, now=0.0)
    assert delay is not None
    assert 0.5 <= delay <= 0.75

    assert _retry_after_s({"retry-after": "abc"}) is None
    assert _retry_after_s({"Retry-After": "3"}) == 3


def test_header_lookup_is_case_insensitive() -> None:
    """``httpx.Headers`` 本来就大小写不敏感，但入参类型是 ``Mapping[str, str]``。

    测试和脚本传进来的是普通 dict —— 依赖「调用方恰好是 httpx」会让这个函数
    在某天换掉 HTTP 层之后静默失效（取不到头 → 退回退避 → 不报错，
    只是等的时间不对）。
    """
    assert _header({"Retry-After": "5"}, "retry-after") == "5"
    assert _header({"retry-after": "5"}, "Retry-After") == "5"
    assert _header({}, "retry-after") is None


# --------------------------------------------------------------------------- #
# 异常树：这张表就是设计本身
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("cls", "error_class", "retryable"),
    [
        (GitHubAuthError, ErrorClass.AUTH_REVOKED, False),
        (GitHubNotFoundError, ErrorClass.REPO_NOT_FOUND, False),
        (GitHubValidationError, ErrorClass.TRANSIENT, False),
        (GitHubRateLimitError, ErrorClass.TRANSIENT, True),
        (GitHubUnavailableError, ErrorClass.TRANSIENT, True),
    ],
)
def test_each_error_says_what_to_do_about_it(
    cls: type[GitHubError], error_class: ErrorClass, retryable: bool
) -> None:
    """``retryable`` 的含义是「**重新发布那个按钮值不值得点**」，不是自动重试。

    所以限流和 5xx 是 True（过一会儿再点就好了），权限和 422 是 False
    （再点一次只会再红一次）。这个区别直接决定 UI 上那个按钮是显示还是禁用。
    """
    assert cls.error_class is error_class
    assert cls.retryable is retryable
    assert issubclass(cls, GitHubError)


def test_every_github_error_is_retryable_by_default_only_if_declared() -> None:
    """基类默认 ``retryable = False``。

    新增子类时忘了声明，得到的是「不重试」—— 失败方向安全的那一个：
    报告已经落库，``publish_failed`` 是可见的、可重新发布的。
    反过来默认 True 会让一个没人看懂的失败白等三轮退避。
    """
    assert GitHubError.retryable is False


def test_a_422_about_your_own_pull_request_is_recognizable() -> None:
    """降级成 ``COMMENT`` 的依据就是这句话。

    真实响应里它可能在 ``message``，也可能在 ``errors[].message`` ——
    所以 ``mentions`` 把两边拼起来一起找。找不到时调用方走的是
    **安全的那条路**（去掉行内评论重发），所以这里不需要穷尽。
    """
    exc = GitHubValidationError(
        "POST /repos/a/b/pulls/1/reviews 返回 HTTP 422",
        errors=[{"message": "Can not request changes on your own pull request"}],
    )
    assert exc.mentions("own pull request")
    assert not exc.mentions("something else entirely")


# --------------------------------------------------------------------------- #
# 对着桩跑真请求
# --------------------------------------------------------------------------- #


async def test_a_rate_limited_publish_retries_and_then_succeeds(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """被限流两次之后成功 —— **而且请求真的发了三次**。

    桩给 ``retry-after: 0``，所以这里不会真的等：退避算得对不对由上面那组
    纯函数负责，这条只验证「它确实重来了」。两件事分开测，是因为
    「等多久」和「要不要等」是两个会各自出错的判断。
    """
    stub.state.rate_limit_times = 2
    client = make_client()

    review = await client.create_review(REPO, PR, body="报告正文", event="COMMENT")

    assert review["id"] > 0
    assert _posts(stub) == 3
    assert len(stub.state.reviews) == 1


async def test_a_403_rate_limit_is_retried_too(
    stub: GitHubStub, make_client: Callable[..., GitHubClient], fast_backoff: None
) -> None:
    """一级限流走的是 403 —— 这条和上面那条**必须分开测**。

    把它们合成一条「限流都会被重试」的测试，会让「403 一律不重试」的
    实现照样通过：桩默认给的是 429。
    """
    stub.state.rate_limit_times = 1
    stub.state.rate_limit_status = 403
    stub.state.ratelimit_reset_in_s = 0
    client = make_client()

    await client.create_review(REPO, PR, body="报告正文", event="COMMENT")

    assert _posts(stub) == 2
    assert len(stub.state.reviews) == 1


async def test_a_permission_403_fails_on_the_first_try(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """权限 403 只发一次请求。

    它和上面那条 403 **唯一的区别是响应头**。桩因此必须能分别造出这两种 ——
    一个只会发 ``x-ratelimit-remaining: 0`` 的桩，会让「403 一律不重试」的
    实现照样通过测试，而那个实现在线上会让限流的 run 永远发不出评论。
    """
    stub.state.permission_denied = True
    client = make_client()

    with pytest.raises(GitHubAuthError) as excinfo:
        await client.create_review(REPO, PR, body="x", event="COMMENT")

    assert excinfo.value.status_code == 403
    assert not excinfo.value.retryable  # 重新发布那个按钮点了也没用
    assert _posts(stub) == 1


async def test_a_long_retry_after_is_not_waited_out(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """限流说「一小时后再来」时**立刻失败**，不等。

    在 publish 节点里睡一小时比直接失败更糟：图会停在那儿（扫描器只看
    ``dispatched``/``waiting``，叫不醒一个正在 sleep 的节点），而报告早就
    落库了、重新发布随时可以点。所以这条断言的重点是
    **请求只发了一次** —— 它没有偷偷地等。
    """
    stub.state.rate_limit_times = 5
    stub.state.retry_after = "3600"
    client = make_client(max_wait_s=60)

    with pytest.raises(GitHubRateLimitError) as excinfo:
        await client.create_review(REPO, PR, body="x", event="COMMENT")

    assert "3600" in str(excinfo.value)
    assert excinfo.value.retryable is True  # 过一会儿再点那个按钮就好了
    assert _posts(stub) == 1


async def test_a_server_error_is_retried_then_reported(
    stub: GitHubStub, make_client: Callable[..., GitHubClient], fast_backoff: None
) -> None:
    """403 那条同理：桩默认给 429，5xx 必须单独钉一条。"""
    stub.state.unavailable_times = 99
    client = make_client(max_retries=2)

    with pytest.raises(GitHubUnavailableError) as excinfo:
        await client.create_review(REPO, PR, body="x", event="COMMENT")

    assert excinfo.value.status_code == 503
    assert _posts(stub) == 3  # 首发 + 两次重试


async def test_a_network_failure_becomes_an_unavailable_error() -> None:
    """连不上时没有响应头可看 —— 这条路径**必须和有响应的那条分开**。

    混在一起写会得到 ``None >= 500`` 这种恒为假的判断，于是网络错误
    既不被重试也不被正确归类。这里注入一个只会抛异常的假客户端，
    免得在 Windows 上为了拿一个 ECONNREFUSED 等两秒。
    """

    class _Exploding(httpx.AsyncClient):
        async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
            raise httpx.ConnectError("boom")

    client = GitHubClient(token="t", base_url="http://127.0.0.1:1", max_retries=1, client=_Exploding())
    with pytest.raises(GitHubUnavailableError) as excinfo:
        await client.create_review(REPO, PR, body="x", event="COMMENT")
    assert "连不上" in str(excinfo.value)
    await client.aclose()


async def test_the_error_taxonomy_matches_the_status_codes(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """状态码 → 异常类。``except GitHubError`` 接得住全部，但分类要准。"""
    client = make_client()

    with pytest.raises(GitHubNotFoundError):
        await client.pull_files("demo/does-not-exist", 1)


async def test_inline_comments_on_unchanged_lines_are_refused(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """行号不在 diff 变更行上 → 422。

    这是 ``Line must be part of the diff`` 那类真实拒绝。桩会**严格**校验
    行号（比真 GitHub 还严），因为它挡的是这个假设：
    「反正 GitHub 会忽略非法行号」。它不会 —— 它拒绝**整个** review，
    连汇总正文一起。
    """
    client = make_client()
    with pytest.raises(GitHubValidationError) as excinfo:
        await client.create_review(
            REPO,
            PR,
            body="正文",
            event="COMMENT",
            comments=[{"path": "src/app.py", "line": 999999, "side": "RIGHT", "body": "这里"}],
        )
    assert not excinfo.value.mentions("own pull request")  # 是另一种 422 → 走另一条降级路径


async def test_pull_files_follows_pagination(
    stub: GitHubStub, make_client: Callable[..., GitHubClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    """翻页取全。

    只取第一页**不会报错**，只是少了一半文件 —— 于是风险排序实际上变成了
    「按文件名字母序」，而审出来的东西看起来完全正常。所以这条断言两件事：
    返回了全部文件，以及它确实翻页了（请求次数对得上）。
    """
    monkeypatch.setattr(client_mod, "PER_PAGE", 2)
    total = len(stub.state.files)
    assert total > 2, "fixture 里的文件太少，翻页测不出来"

    files = await make_client().pull_files(REPO, PR)

    assert len(files) == total
    # 请求次数永远是「页数 + 1」：不满一页才停，所以整页收尾时多一次空请求
    # （客户端里有解释 —— 用 Link 头省这次请求的代价是「头解析失败就静默少审一半」）。
    pages = len([r for r in stub.state.requests if r.endswith("/files")])
    assert pages == total // 2 + 1


async def test_whoami_asks_once_and_remembers(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """``whoami`` 的结果会被记住：publish 节点每次运行都会问一次，
    而它问的目的是「我是不是这个 PR 的作者」，答案在一次运行里不会变。"""
    client = make_client()
    assert await client.whoami() == "sfly-bot"
    assert await client.whoami() == "sfly-bot"
    assert stub.state.requests.count("GET /user") == 1


async def test_whoami_survives_a_server_that_says_no(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """拿不到身份**不是**发布失败的理由。

    它只影响一个判断（要不要发 REQUEST_CHANGES），而拿不到时走的是
    「照发、被拒再降级」那条路 —— 比「干脆不发」好得多。
    """
    stub.state.permission_denied = True
    client = make_client(max_retries=0)

    assert await client.whoami() == ""
    # **失败不写进缓存。** 第二次问还是去问 —— 上面那次是「没问出来」，
    # 不是「答案是空」。把两者混在一起，一次网络抖动就会让这个 run
    # 后面的所有判断都基于「我不知道我是谁」。
    assert await client.whoami() == ""
    assert stub.state.requests.count("GET /user") == 2


async def test_find_marker_looks_in_both_reviews_and_comments(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """隐藏标记的查找要覆盖两种正文形式。

    它是防重复评论的第二道闸，挡的是「评论发出去了、但写库失败」——
    那时 ``github_comment_id`` 是空的，唯一能认出来的东西就是正文里的标记。
    只查一边的实现会让另一种形式在重试时变成一条重复评论。
    """
    client = make_client()
    marker = "<!-- sfly:run:01J0000000000000000000000 -->"

    assert await client.find_marker(REPO, PR, marker) is None

    await client.create_review(REPO, PR, body=f"{marker}\n正文", event="COMMENT")
    assert await client.find_marker(REPO, PR, marker) == ("review", stub.state.reviews[0]["id"])

    other = "<!-- sfly:run:01J0000000000000000000001 -->"
    await client.create_issue_comment(REPO, PR, body=f"{other}\n正文")
    assert await client.find_marker(REPO, PR, other) == ("comment", stub.state.comments[0]["id"])

    # 顺序：先查 review。命中哪个是哪个，但返回值要说清楚是哪种。
    assert await client.find_marker(REPO, PR, "没有这个标记") is None


async def test_a_review_request_carries_the_inline_comments(
    stub: GitHubStub, make_client: Callable[..., GitHubClient]
) -> None:
    """一次请求带全部行内评论 —— 这是选 review 接口而不是「一条条发」的理由。"""
    client = make_client()
    await client.create_review(
        REPO, PR, body="正文", event="REQUEST_CHANGES", comments=[_comment_on_a_changed_line(stub)]
    )
    assert len(stub.state.reviews) == 1
    assert stub.state.reviews[0]["state"] == "CHANGES_REQUESTED"
    assert stub.state.reviews[0]["comments"] == 1


def _comment_on_a_changed_line(stub: GitHubStub) -> dict[str, Any]:
    """一条**合法**的行内评论：路径和行号都取自真实的变更行。

    不写死 ``src/app.py:1`` —— 那个位置在录制载荷里不存在，而桩（像真 GitHub
    一样）会因此拒绝**整个** review。从这里取值的做法本身就是发布节点
    以后要做的事：行号只能来自 ``FilePatch.changed_lines``。
    """
    patches, _skipped = patches_from_files(stub.state.files, max_patch_chars=1_000_000)
    patch = patches[0]
    return {"path": patch.path, "line": patch.changed_lines[0], "side": "RIGHT", "body": "这里"}


def _posts(stub: GitHubStub) -> int:
    """发往 ``/reviews`` 的 POST 次数 —— 也就是「重试了几次」的答案。"""
    return len([r for r in stub.state.requests if r.startswith("POST ") and r.endswith("/reviews")])
