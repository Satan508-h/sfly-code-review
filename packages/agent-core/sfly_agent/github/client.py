"""GitHub REST 客户端 —— 只有 ``publish`` 真正用到的那几个调用。

### 为什么用 ``httpx``，而不是 openai 自带的那一份

``agent-core`` 的 pyproject 里留着一句「M7 的 GitHub 客户端再决定用哪个」。
现在是那个决定：**用 ``httpx``（0.28），并把它声明成显式依赖。**

openai 3.x 带的是 ``httpx2``（包名不同，所以两者能在同一个环境里共存，
不会互相顶掉）。选 ``httpx`` 的理由不是它更好，而是**它和 ``apps/api``、
``scripts/``、``tests/`` 用的是同一个**：整个仓库因此只有一种超时语义、
一种 ``Timeout`` 类、一种 ``HTTPStatusError``。把 httpx2 引进 agent-core
会在同一个进程里造出两个同名不同源的 ``Timeout`` —— 传错不会报错，
只会在某个超时真的触发时才现形（这句警告在 ``llm/openai_compat.py`` 里
已经写过一次，那次踩的就是它）。

### 为什么重试循环是手写的，而不是 ``tenacity``

``agent-core`` 已经依赖 tenacity，而这里的重试**不能**表达成
「第 N 次尝试等多久」的函数：等多久是 GitHub 在响应头里说的
（``retry-after``，或者一级限流的 ``x-ratelimit-reset`` 时间戳），
而且同一个 ``403`` 有两种完全不同的含义，要靠 ``x-ratelimit-remaining``
区分「限流」和「没权限」。把这些塞进 tenacity 的 ``wait`` 里会变成一个
比循环更难读的东西 —— 而循环只有二十行，每一行都在解释一件具体的事。

### 分页：``/pulls/{n}/files`` 是唯一需要翻页的调用

GitHub 每页最多 100 条，一个 PR 最多 3000 个文件。**必须取全**，不能只取
第一页：这个接口按**文件名**排序，不是按改动量 —— 只取前 100 个文件意味着
一个 200 文件的 PR 里，排序靠后的那一半永远不会进入 ``plan`` 节点的风险排序，
于是「按风险取前 40」实际上是「按文件名字母序取前 40」。那是一种没有报错的
降级：审出来的东西看起来完全正常。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import httpx

from sfly_agent.github.errors import (
    BODY_SNIPPET,
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    GitHubValidationError,
)
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 锁死 API 版本。GitHub 对不带这个头的请求使用**默认版本**，而默认版本
#: 会随新版本发布而变（2026-09 已经是第三个默认版本了）。一个会静默改变的
#: 响应形状，比一个写死的日期危险得多。
API_VERSION = "2022-11-28"

ACCEPT = "application/vnd.github+json"

#: GitHub **要求**每个请求带 User-Agent，缺了直接 403。httpx 默认会带
#: ``python-httpx/0.28.1``，所以「不写也能跑」—— 但那是 httpx 的实现细节，
#: 哪天它不带了，症状是「换个版本之后全都 403」。显式写死。
USER_AGENT = "sfly-code-review-bot"

#: 每页条数。GitHub 的默认值是 30，显式拉到上限 —— 200 个文件的 PR
#: 因此从 7 次请求变成 2 次。
PER_PAGE = 100

#: 单次请求最多翻多少页。100 × 30 = 3000 = GitHub 自己对这个接口的上限。
MAX_PAGES = 30

#: 退避的基数（秒）。第 N 次重试等 ``基数 * 2**N``。
#: **测试里会把它改成 0** —— 「等多久」由 :func:`retry_delay_s` 的纯函数
#: 测试负责，让真请求也等一遍只会让套件慢两秒，什么也没多验证。
BACKOFF_BASE_S = 0.5

#: 退避的上限（秒）。不限上限的话，一次长时间抖动会让 publish 节点
#: 睡到 run 的 deadline 之后 —— 那时扫描器已经在别处做它的事了，
#: 而这条 run 的状态还停在 ``aggregating``。
BACKOFF_CAP_S = 8.0

#: 每个状态码配一句人话。状态码本身不解释处置方式 —— 403 既可能是限流
#: 也可能是权限不足，这个区别决定了「等一会儿再点重新发布」还是
#: 「去改 token」。和 ``llm/openai_compat.py`` 里那张表同一个理由。
_STATUS_HINT: dict[int, str] = {
    401: "token 无效或已过期（GITHUB_TOKEN 写错了，或者 token 被撤销了）",
    403: "权限不足，或触发了限流（看 x-ratelimit-remaining 区分）",
    404: "仓库 / PR / 评论不存在，或者 token 看不到它（私有仓库需要 repo 权限）",
    422: "GitHub 看懂了请求但拒绝了它（行号不在 diff 里，或者给自己的 PR 请求修改）",
    429: "触发二级限流",
}

#: ``publish`` 允许发的两种事件。**``APPROVE`` 不在里面，而且是类型层面的**：
#: 机器人审批人类 PR 是策略漏洞（一个被绕过的模型会拿到一个和人工审批长得
#: 一模一样的绿色标记）。把它写成一个 Literal 而不是靠自觉，
#: 意味着任何一次「顺手加个 APPROVE」都会在 mypy 那一关失败。
ReviewEvent = Literal["COMMENT", "REQUEST_CHANGES"]


# --------------------------------------------------------------------------- #
# 重试策略（纯函数，是这一层唯一值得逐条单测的东西）
# --------------------------------------------------------------------------- #


def retry_delay_s(
    status: int,
    headers: Mapping[str, str],
    attempt: int,
    *,
    now: float,
) -> float | None:
    """这次失败该等多久再试。``None`` 表示**不该重试**。

    四个分支各自对应一件真实发生过的事：

    * ``429`` —— 二级限流。``retry-after`` 说的是秒数，而它是**唯一权威**：
      自己拍一个退避比它短就是继续撞墙，比它长就是白等。
    * ``403 + x-ratelimit-remaining: 0`` —— 一级限流用完了。GitHub 在这种
      情况给的是 ``403`` 而不是 429，并且用 ``x-ratelimit-reset``（**epoch
      秒**，不是「还有几秒」）说什么时候恢复。看错这个单位会得到
      「等 1758 万年」或者「等 0 秒」—— 两种都不会报错。
    * ``403`` 其它情况 —— 权限问题。重试一万次也一样，而且**每一次都会
      消耗一次配额**。
    * ``5xx`` —— GitHub 自己挂了。退回指数退避 + 抖动。

    ``attempt`` 从 0 开始，所以第一次重试等的是 ``_backoff(0)``。
    """
    # 两个 ``or`` 不是笔误：``_reset_wait_s`` 在「重置时刻已经过去」时返回
    # ``0.0``，而 ``0.0`` 是假值，于是这里退回退避。**这是刻意的** ——
    # 0 秒等待意味着立刻重发，而如果 GitHub 的回答还是同一句话，
    # 三次重试会在几毫秒里烧完（时钟偏差、以及中间层缓存了限流响应，
    # 都会让「已经过去了」这个判断出错）。宁可多等半秒。
    if status == 429:
        wait = _retry_after_s(headers)
        if wait is not None:
            return wait
        return _reset_wait_s(headers, now) or _backoff(attempt)

    if status == 403:
        if _header(headers, "x-ratelimit-remaining") == "0":
            return _reset_wait_s(headers, now) or _backoff(attempt)
        return None

    if status >= 500:
        return _backoff(attempt)

    return None


def _backoff(attempt: int) -> float:
    """指数退避 + 抖动。

    抖动不是为了好看：多个 Worker 同时撞上限流时，不加抖动它们会
    **同时**重试，然后同时再被拒 —— 限流窗口里最不需要的就是这种整齐。
    """
    # S311（不要用非加密随机数）：这里要的正是「不可预测、但不需要安全」的随机数。
    # 换成 SystemRandom 只会让退避更难复现，拿不到任何东西 —— 它不是密钥、
    # 不是 token，猜中它的人得不到任何好处。
    # 写成 ``2.0 ** attempt`` 而不是 ``2 ** attempt``：int 的幂在 mypy 里是
    # ``Any``（负指数会得到 float/complex），于是整行表达式跟着变成 Any，
    # 报出来的是 ``Returning Any from function declared to return "float"`` ——
    # 一个指向 return 而不是指向 ``**`` 的错误。
    return min(BACKOFF_BASE_S * 2.0**attempt, BACKOFF_CAP_S) + random.uniform(0, 0.25)  # noqa: S311


def _retry_after_s(headers: Mapping[str, str]) -> float | None:
    """``retry-after`` 头，单位秒。

    规范允许它写成 HTTP 日期，GitHub 不发那种 —— 解析不出来就返回 ``None``
    （调用方退回退避），而不是猜一个值：猜出来的等待时间会**看起来**很合理。
    """
    raw = _header(headers, "retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _reset_wait_s(headers: Mapping[str, str], now: float) -> float | None:
    """``x-ratelimit-reset`` 距现在还有几秒。它给的是 epoch 秒。"""
    raw = _header(headers, "x-ratelimit-reset")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw) - now)
    except ValueError:
        return None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """大小写不敏感地取一个响应头。

    ``httpx.Headers`` 本身就是大小写不敏感的，但**不能依赖这一点**：
    这个函数的入参类型是 ``Mapping[str, str]``，而测试、脚本、
    以及将来某个换掉的 HTTP 层传进来的都是普通 dict。
    """
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #


class GitHubClient:
    """``publish`` 需要的全部 GitHub 调用。

    **没有 token 就不构造它**（见 publish 节点）：一个没有 token 的客户端
    只会在第一次请求时把 401 变成一次无谓的重试。没有 token 意味着
    「dry-run」，那是调用方的决定，不是客户端的状态。
    """

    def __init__(
        self,
        *,
        token: str,
        base_url: str = "https://api.github.com",
        max_retries: int = 3,
        max_wait_s: float = 60.0,
        connect_timeout_s: float = 10.0,
        read_timeout_s: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._max_wait_s = max_wait_s
        #: 连接超时和读超时必须分开：连不上该在 10 秒内失败，而
        #: 一个 25 条评论的 review 请求花几秒钟是正常的。
        #: 一个笼统的 30 秒超时会让「DNS 挂了」表现为「GitHub 很慢」。
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ACCEPT,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
            timeout=httpx.Timeout(read_timeout_s, connect=connect_timeout_s),
            # **重试只在这一层做。** httpx 的默认 transport 是
            # ``AsyncHTTPTransport(retries=0)``，也就是它自己不重试 ——
            # 这里什么都没有设，是因为默认值已经是我们想要的那个。
            # 别去给 transport 开 ``retries``：两层重试会得到 3×3 次调用
            # 和一份对不上的日志，而 attempt 计数是死信判定的依据。
        )
        self._login: str | None = None

    async def aclose(self) -> None:
        """关连接池。不关的话每个客户端漏一组 socket。"""
        await self._client.aclose()

    # -- 读 ---------------------------------------------------------------- #

    async def whoami(self) -> str:
        """token 对应的登录名。**结果会记住** —— publish 节点每次运行都会问一次。

        拿不到（403/网络）就返回空字符串：这个值的唯一用途是「判断我是不是
        这个 PR 的作者」，而**拿不到时走的是「照发 REQUEST_CHANGES、被拒再降级」
        那条路**，不是「不发」。所以它不值得让整个发布失败。
        """
        if self._login is None:
            try:
                data = await self._json("GET", "/user")
            except GitHubError as exc:
                log.warning("github.whoami_failed", error=str(exc), note="按「不知道我是谁」继续")
                return ""
            login = data.get("login")
            self._login = str(login) if login else ""
        return self._login

    async def pull_files(self, repo: str, pr_number: int) -> list[dict[str, Any]]:
        """``GET /repos/{repo}/pulls/{n}/files``，翻页取全。

        返回值**原样**是 GitHub 的响应形状（``filename`` / ``status`` /
        ``patch`` / ``additions``…），交给
        :func:`sfly_api.github_payload.patches_from_files` 转换 ——
        回放脚本喂的是同一种形状，两条路径因此共用同一个转换器。

        **判据是「这一页不满」，不是 ``Link`` 头。** GitHub 会在
        ``Link: <...>; rel="next"`` 里说下一頁在哪，那看起来是更「正确」的做法，
        但它有一个不可接受的失效方式：头没解析出来（格式变了、被中间层剥掉、
        正则差一个字符）的表现是**翻到第一页就停下**，于是 200 个文件的 PR
        只审了前 100 个，而报告看起来完全正常。用「不满一页就结束」的话，
        失效方向反过来 —— 最坏是多发一次请求。

        代价是**总数正好是 100 的整数倍时会多问一次**（第 101 个文件那页返回
        空数组）。一次空请求换一个不会静默少审文件的分页逻辑，这个交换很划算。
        """
        files: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            batch = await self._json(
                "GET",
                f"/repos/{repo}/pulls/{pr_number}/files",
                params={"per_page": PER_PAGE, "page": page},
            )
            if not isinstance(batch, list):
                raise GitHubError(f"pulls/files 返回的不是数组：{type(batch).__name__}")
            files.extend(item for item in batch if isinstance(item, dict))
            if len(batch) < PER_PAGE:
                break
        else:
            # 走满 30 页说明这是一个超过 3000 文件的 PR。**说清楚被截断了**，
            # 而不是让下游以为这就是全部。plan 节点会按风险取前若干个，
            # 但「因为太大而没看全」和「看全了、只有这些」是两件事。
            log.warning("github.files_capped", repo=repo, pr=pr_number, pages=MAX_PAGES)
        return files

    async def find_marker(self, repo: str, pr_number: int, marker: str) -> tuple[str, int] | None:
        """在**已经存在**的评论里找隐藏标记，返回 ``(kind, id)``。

        这是防重复评论的第二道闸（第一道是 ``review_runs.github_comment_id``）。
        它挡的是第一道挡不住的那种情况：**评论发出去了，但写库失败**。
        那时数据库里没有 comment id，而 PR 上已经有一条了 ——
        两处都查一遍才发现得了。

        ``kind`` 是 ``"review"`` 或 ``"comment"``：正文的形式决定了它是哪一类，
        而这个值要写进事件里给 UI 看（「这条是怎么发出去的」）。顺序上先查
        review —— 那是主路径，命中的概率高得多。
        """
        reviews = await self._json(
            "GET", f"/repos/{repo}/pulls/{pr_number}/reviews", params={"per_page": PER_PAGE}
        )
        for item in _dicts(reviews):
            if marker in str(item.get("body") or ""):
                return "review", int(item.get("id") or 0)

        comments = await self._json(
            "GET", f"/repos/{repo}/issues/{pr_number}/comments", params={"per_page": PER_PAGE}
        )
        for item in _dicts(comments):
            if marker in str(item.get("body") or ""):
                return "comment", int(item.get("id") or 0)

        return None

    # -- 写 ---------------------------------------------------------------- #

    async def create_review(
        self,
        repo: str,
        pr_number: int,
        *,
        body: str,
        event: ReviewEvent,
        comments: Sequence[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """``POST /repos/{repo}/pulls/{n}/reviews`` —— 汇总正文 + 行内评论。

        **一次请求发完所有东西**，这是选这条接口而不是「一条普通评论 +
        N 条行内评论」的理由：N+1 次请求会在中途失败时留下
        「汇总发出去了、行内只发了一半」的状态，而那种状态既不能重发
        （会重复）也不能不管（报告不完整）。

        ``comments`` 里每项的形状是 ``{"path", "line", "side", "body"}``，
        其中 ``line`` **必须落在 diff 的变更行上** —— GitHub 会拒绝未变更的
        行号（422），而一次拒绝会让整个 review 都不成立（包括正文）。
        所以调用方必须先自己筛一遍（见 ``FilePatch`` 的文档），
        再容忍 422 作为最后一道兜底。
        """
        payload: dict[str, Any] = {"body": body, "event": event}
        if comments:
            payload["comments"] = [dict(c) for c in comments]
        return await self._json_object("POST", f"/repos/{repo}/pulls/{pr_number}/reviews", json=payload)

    async def create_issue_comment(self, repo: str, pr_number: int, *, body: str) -> dict[str, Any]:
        """``POST /repos/{repo}/issues/{n}/comments`` —— 只发一条汇总。

        行内评论那条路全废时的兜底：它的失败模式最少（没有行号可以不对），
        所以只要 GitHub 还能说话，它就能成功。
        """
        return await self._json_object(
            "POST", f"/repos/{repo}/issues/{pr_number}/comments", json={"body": body}
        )

    # -- HTTP 本身 --------------------------------------------------------- #

    async def _json_object(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """要一个 JSON **对象**的调用。

        单独包一层是因为这个项目里所有「会返回对象」的接口，调用方拿到的都是
        ``dict``，而 ``resp.json()`` 的静态类型是 ``Any`` —— 直接返回它，
        mypy 会因为 ``no-any-return`` 拒绝（strict 模式），绕过去的办法
        （``cast``）会让「GitHub 返回了一个数组」这件事变成一次
        ``TypeError: string indices must be integers``，出现在离这里很远的地方。
        返回空 dict 比崩溃好：id 取不到时调用方的行为是明确的（记不到
        comment id，下次重发前会靠隐藏标记认出来）。
        """
        data = await self._json(method, path, json=json, params=params)
        return dict(data) if isinstance(data, Mapping) else {}

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> Any:
        resp = await self._request(method, path, json=json, params=params)
        try:
            return resp.json()
        except ValueError as exc:
            raise GitHubError(
                f"{method} {path} 返回的不是 JSON（HTTP {resp.status_code}）：{_snippet(resp.text)}"
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        """发一次请求，按 :func:`retry_delay_s` 的结论决定要不要重来。"""
        attempt = 0
        while True:
            delay: float | None
            status: int | None
            try:
                resp = await self._client.request(method, path, json=json, params=params)
            except httpx.HTTPError as exc:
                # 网络层失败：连不上、读超时、连接被掐断。**必须和「有响应」
                # 那条路分开** —— 这里没有状态码，混进去会得到一个
                # 「None >= 500 为假」的静默错误判断。
                if attempt >= self._max_retries:
                    raise GitHubUnavailableError(
                        f"{method} {path} 连不上 GitHub（{self._base_url}）：{exc}"
                    ) from exc
                delay = _backoff(attempt)
                status = None
            else:
                if resp.is_success:
                    return resp
                status = resp.status_code
                delay = retry_delay_s(status, resp.headers, attempt, now=time.time())
                if delay is None or attempt >= self._max_retries:
                    raise self._error(method, path, resp)
                if delay > self._max_wait_s:
                    # GitHub 说「一小时后再来」。在 publish 节点里睡一小时比
                    # 直接放弃更糟：**图会停在那儿**，而报告早就落库了、
                    # 重新发布随时可以点。让这次失败发生，把决定权交给人。
                    raise GitHubRateLimitError(
                        f"{method} {path} 被限流，要求等 {delay:.0f} 秒"
                        f"（超过 github_max_wait_s={self._max_wait_s:.0f}，不等了）",
                        status_code=status,
                        body=_snippet(resp.text),
                    )
            log.warning(
                "github.retry",
                method=method,
                path=path,
                attempt=attempt + 1,
                status=status,
                wait_s=round(delay, 2),
            )
            await asyncio.sleep(delay)
            attempt += 1

    def _error(self, method: str, path: str, resp: httpx.Response) -> GitHubError:
        """把一次失败响应翻译成带处置建议的异常。"""
        status = resp.status_code
        hint = _STATUS_HINT.get(status, "")
        message = f"{method} {path} 返回 HTTP {status}" + (f" —— {hint}" if hint else "")
        message += f"；响应体：{_snippet(resp.text)}"
        body = _snippet(resp.text)

        if status in (401, 403):
            # 走到这里说明限流那两种已经被 retry_delay_s 拦掉了
            # （429 / 403+remaining=0 都会先重试、重试完再抛限流）。剩下的 403
            # 就是权限问题 —— 处置方式是改 token，不是等。
            return GitHubAuthError(message, status_code=status, body=body)
        if status == 404:
            return GitHubNotFoundError(message, status_code=status, body=body)
        if status == 422:
            return GitHubValidationError(message, status_code=status, body=body, errors=_errors_of(resp))
        if status == 429:
            return GitHubRateLimitError(message, status_code=status, body=body)
        if status >= 500:
            return GitHubUnavailableError(message, status_code=status, body=body)
        return GitHubError(message, status_code=status, body=body)


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _snippet(text: str) -> str:
    return text[:BODY_SNIPPET]


def _errors_of(resp: httpx.Response) -> list[Any]:
    """422 响应里的 ``errors[]``。

    读不出来就返回空列表 —— **不能因此盖掉原始错误**：调用方靠
    :meth:`GitHubValidationError.mentions` 判断这是什么 422，
    而「判断不出来」的后果只是走安全的那条路径（去掉行内评论重发）。
    """
    try:
        data = resp.json()
    except ValueError:
        return []
    if not isinstance(data, Mapping):
        return []
    errors = data.get("errors")
    return list(errors) if isinstance(errors, list) else []


def _dicts(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
