"""GitHub API 的桩：一个 stdlib HTTP server，只在**我们真正用到的那几条路径上**
模仿真实 GitHub —— 并且把「真实的坏行为」做成开关。

### 为什么它住在 ``tests/`` 而不是 ``infra/``

CLAUDE.md 里那条判据在这里同样适用：**一个模块该住在哪，看它的消费者有没有
权利依赖那个包**。这里的消费者只有两处 —— 单测，和手工演示时的
``python tests/github_stub.py``。它不进任何容器的构建，也不该被读成部署的一部分。
放在 ``tests/`` 还有一个直接好处：``tests/`` 已经在 mypy 的覆盖范围里
（和 ``postgres_support.py`` / ``redis_support.py`` 同一类东西）。

### 桩是模型，不是真相

README 的「已知限制」里写着这句话，这里再说一次，因为它决定了这个文件的边界：
桩能证明的是「我们的重试/降级逻辑在**我们以为的** GitHub 行为下是对的」。
真实的二级限流什么时候触发、``retry-after`` 给多少、422 的 ``errors[]`` 长什么样，
只有真发一次才知道。所以这里的原则是 **宁可把行为做得比真 GitHub 更苛刻**
（比如严格校验行内评论的行号），而不是更宽松 —— 宽松的桩会让调用方带着
一个真环境里必炸的假设通过测试。

### 几个开关对应各自的一条真实路径

============================ ==========================================
``rate_limit_times=N``       前 N 次发布被限流（429 或 403）
``retry_after``              ``retry-after`` 头。真实值是整数秒，测试里给 "0"
``unavailable_times=N``      前 N 次 503
``reject_own_pr``            REQUEST_CHANGES 给自己的 PR → 422
``reject_inline``            带 ``comments[]`` 的 review → 422
``verify_lines``             行号不在 diff 变更行上 → 422（默认开）
============================ ==========================================
"""

from __future__ import annotations

import argparse
import json
import re
import socketserver
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self
from urllib.parse import parse_qs, urlsplit

from sfly_api.github_payload import patches_from_files

#: 录制的 ``/pulls/{n}/files`` 响应 —— 和 ``scripts/replay_webhook.py`` 用的是同一份。
#: 它不是「随便造几条」：里面的 path/line 是真实文件里的真实位置，
#: 所以按它算出来的「可评论行号」也是真的。
FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "webhook_pr.json"

#: 解析补丁时的字符上限。桩不关心截断（那是 plan 节点的事），给一个大值。
_MAX_PATCH_CHARS = 1_000_000

_FILES_RE = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/(\d+)/files$")
_REVIEWS_RE = re.compile(r"^/repos/([^/]+/[^/]+)/pulls/(\d+)/reviews$")
_COMMENTS_RE = re.compile(r"^/repos/([^/]+/[^/]+)/issues/(\d+)/comments$")


def _fixture() -> dict[str, Any]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def recorded_files() -> list[dict[str, Any]]:
    """``fixtures/webhook_pr.json`` 里那份录制的文件列表。"""
    files = _fixture().get("files")
    return list(files) if isinstance(files, list) else []


def recorded_repo() -> str:
    """录制载荷里的 ``repository.full_name``。

    桩只认这一个仓库，别的都回 404 —— 真实的 404 就是这么来的
    （仓库不存在，或者 token 看不到它），而这条路径要有东西能触发它。
    """
    repo = _fixture().get("repository")
    full_name = repo.get("full_name") if isinstance(repo, dict) else None
    return str(full_name) if full_name else "demo/sfly-playground"


@dataclass
class StubState:
    """桩的全部状态。**测试直接读它做断言** —— 比解析日志可靠。"""

    login: str = "sfly-bot"
    repo: str = field(default_factory=recorded_repo)
    files: list[dict[str, Any]] = field(default_factory=recorded_files)

    #: 还要失败几次。每次失败都从它里面减一。
    rate_limit_times: int = 0
    #: 限流用哪个状态码。**两个都要能模拟**：429 是二级限流，
    #: 403 是一级限流用完（靠 ``x-ratelimit-remaining: 0`` 认出来），
    #: 而客户端的判断逻辑对这两条是不同的分支。
    rate_limit_status: int = 429
    #: ``retry-after`` 的值。测试里给 "0" 就不会真的等 —— 退避的正确性
    #: 由 ``retry_delay_s`` 的纯函数单测负责，这里只验证「它真的重试了」。
    retry_after: str | None = "0"
    #: 一级限流的 ``x-ratelimit-reset``（**距现在多少秒**，不是 epoch）。
    ratelimit_reset_in_s: float | None = None

    unavailable_times: int = 0
    #: 一律回 403 **且不带 ``x-ratelimit-remaining: 0``** —— 也就是真实的
    #: 「这个 token 没权限」。它和限流共用状态码，所以桩里也必须能单独造出来，
    #: 否则「403 权限问题不重试」这条只能靠一个不真实的响应去测。
    permission_denied: bool = False

    reject_own_pr: bool = False
    reject_inline: bool = False
    verify_lines: bool = True

    reviews: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    #: ``"POST /repos/a/b/pulls/1/reviews"`` 这样的字符串，按顺序。
    #: 「重试了几次」这个问题只有它能回答。
    requests: list[str] = field(default_factory=list)
    next_id: int = 1000

    def bump_id(self) -> int:
        self.next_id += 1
        return self.next_id

    def reset_calls(self) -> None:
        self.requests.clear()


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    state: StubState

    def server_bind(self) -> None:
        """跳过 ``HTTPServer.server_bind`` 里的 ``socket.getfqdn(host)``。

        那是一次反向 DNS 查询。**本机实测只要 6 毫秒**，所以它不是这里最慢的
        一环（真正的那个是下面 ``serve_forever`` 的 ``poll_interval``）——
        但在没有反向 DNS 的环境里（CI 容器、公司网络、没有 DNS 的机器）
        它会阻塞几百毫秒，而这段代码每个测试都要跑一遍。

        跳过它是安全的：``server_name`` 只出现在 ``BaseHTTPRequestHandler``
        的默认错误页里，而这个桩一次都不用它。
        """
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host if isinstance(host, str) else ""
        self.server_port = port


class _Handler(BaseHTTPRequestHandler):
    #: 1.1 才能让 httpx 复用连接。**Content-Length 因此是必须的** ——
    #: 少了它客户端会一直等一个永远不来的结束标记。
    protocol_version = "HTTP/1.1"
    server: _Server

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    # 默认实现往 stderr 打一行访问日志。测试的输出里混进这些是噪音，
    # 而且这个项目的 stdout 是留给程序输出的（见 CLAUDE.md）。
    def log_message(self, *args: Any) -> None:
        return

    # -- 路由 -------------------------------------------------------------- #

    def _dispatch(self, method: str) -> None:
        state = self.server.state
        path = urlsplit(self.path).path
        state.requests.append(f"{method} {path}")

        if state.permission_denied:
            self._json(403, {"message": "Resource not accessible by personal access token"})
            return

        if method == "GET" and path == "/user":
            self._json(200, {"login": state.login})
            return
        if method == "GET" and path == "/__state":
            self._json(200, _snapshot(state))
            return

        if match := _FILES_RE.match(path):
            if self._unknown_repo(match, state):
                return
            self._files(state, match.group(2))
            return
        if match := _REVIEWS_RE.match(path):
            if self._unknown_repo(match, state):
                return
            if method == "POST":
                self._post_review(state)
            else:
                self._json(200, state.reviews)
            return
        if match := _COMMENTS_RE.match(path):
            if self._unknown_repo(match, state):
                return
            if method == "POST":
                self._post_comment(state)
            else:
                self._json(200, state.comments)
            return

        self._json(404, {"message": "Not Found"})

    def _unknown_repo(self, match: re.Match[str], state: StubState) -> bool:
        """仓库名不是 ``state.repo`` → 404（真实 GitHub 对无权访问的仓库也这么答）。"""
        if match.group(1) == state.repo:
            return False
        self._json(404, {"message": "Not Found"})
        return True

    def _files(self, state: StubState, pr: str) -> None:
        """真的按 ``per_page``/``page`` 翻页 —— 客户端的分页逻辑才有东西可测。"""
        query = parse_qs(urlsplit(self.path).query)
        per_page = max(1, int(query.get("per_page", ["30"])[0]))
        page = max(1, int(query.get("page", ["1"])[0]))
        start = (page - 1) * per_page
        self._json(200, state.files[start : start + per_page])

    def _post_review(self, state: StubState) -> None:
        payload = self._read_json()

        if state.unavailable_times > 0:
            state.unavailable_times -= 1
            self._json(503, {"message": "Server Error"})
            return

        if state.rate_limit_times > 0:
            state.rate_limit_times -= 1
            headers: dict[str, str] = {}
            if state.retry_after is not None:
                headers["retry-after"] = state.retry_after
            if state.rate_limit_status == 403:
                headers["x-ratelimit-remaining"] = "0"
                if state.ratelimit_reset_in_s is not None:
                    headers["x-ratelimit-reset"] = str(int(_now() + state.ratelimit_reset_in_s))
            self._json(state.rate_limit_status, {"message": "rate limited"}, headers)
            return

        if state.reject_own_pr and payload.get("event") == "REQUEST_CHANGES":
            # 真实 GitHub 的原话。客户端靠这句话决定降级成 COMMENT。
            self._json(
                422,
                {
                    "message": "Validation Failed",
                    "errors": [{"message": "Can not request changes on your own pull request"}],
                },
            )
            return

        inline = payload.get("comments") or []
        if inline and state.reject_inline:
            self._json(422, {"message": "Validation Failed", "errors": _LINE_ERROR})
            return

        if inline and state.verify_lines:
            bad = _invalid_line(state, inline)
            if bad is not None:
                self._json(422, {"message": "Validation Failed", "errors": [_LINE_ERROR[0] | bad]})
                return

        review = {
            "id": state.bump_id(),
            "body": payload.get("body") or "",
            "state": "COMMENTED" if payload.get("event") == "COMMENT" else "CHANGES_REQUESTED",
            "comments": len(inline),
        }
        state.reviews.append(review)
        self._json(201, review)

    def _post_comment(self, state: StubState) -> None:
        payload = self._read_json()
        comment = {"id": state.bump_id(), "body": payload.get("body") or ""}
        state.comments.append(comment)
        self._json(201, comment)

    # -- 读写 -------------------------------------------------------------- #

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def _json(self, status: int, payload: Any, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)


#: 行号不在 diff 里时 GitHub 给的那条错误。客户端**不解析**它（只做子串匹配
#: 那句话），但桩要给出一个形状正确的 422 —— 否则「降级成功」这件事
#: 就是在对着一个不真实的响应测出来的。
_LINE_ERROR: list[dict[str, Any]] = [
    {
        "resource": "PullRequestReviewComment",
        "field": "line",
        "code": "invalid",
    }
]


def _now() -> float:
    return time.time()


def _snapshot(state: StubState) -> dict[str, Any]:
    return {
        "login": state.login,
        "requests": list(state.requests),
        "reviews": list(state.reviews),
        "comments": list(state.comments),
    }


def _invalid_line(state: StubState, inline: list[Any]) -> dict[str, Any] | None:
    """找出第一条锚定在**未变更行**上的评论。没有就返回 ``None``。

    用的是和发布方同一个解析器（``patches_from_files`` 内部的
    ``parse_unified_diff``）—— 这意味着如果解析器本身算错了变更行，
    这个检查会跟着一起错。那是**刻意**的：这里要测的是「发布方有没有按
    ``FilePatch.changed_lines`` 筛行号」，而不是「解析器对不对」，
    后者有自己的单测。
    """
    patches, _skipped = patches_from_files(state.files, max_patch_chars=_MAX_PATCH_CHARS)
    allowed = {p.path: set(p.changed_lines) for p in patches}
    for item in inline:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        line = item.get("line")
        if path not in allowed:
            return {"resource": "PullRequestReviewComment", "field": "path", "code": "invalid"}
        if not isinstance(line, int) or line not in allowed[path]:
            return {"resource": "PullRequestReviewComment", "field": "line", "code": "invalid"}
    return None


class GitHubStub:
    """跑起来的桩。线程里 serve，测试结束就关掉。

    端口用 0（让内核分配）：写死端口会在并发跑测试时撞车，
    而撞车的症状是「连接被拒绝」—— 看起来像客户端的问题。
    """

    #: ``serve_forever`` 的轮询间隔。``shutdown()`` 是靠「等这个循环注意到
    #: 标志位」来结束的，所以**默认的 0.5 秒会变成每个测试固定的半秒**
    #: （实测：27 个用桩的测试 → 整个套件多出 13 秒，而每条测试的耗时
    #: 都恰好是 +0.5s，看起来像是随机的固定开销）。
    POLL_INTERVAL_S = 0.02

    def __init__(self, **state_kwargs: Any) -> None:
        self.state = StubState(**state_kwargs)
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.state = self.state
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": self.POLL_INTERVAL_S}, daemon=True
        )

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        # AF_INET 的地址是 ``(str, int)``，但 ``server_address`` 的静态类型是个
        # 联合（Unix socket 那几支是 bytes）。断言一次而不是 cast ——
        # 上面绑的就是 "127.0.0.1"，这里不可能不成立。
        assert isinstance(host, str)
        return f"http://{host}:{port}"

    def start(self) -> Self:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()


def main(argv: list[str] | None = None) -> int:
    """手工演示用：``python tests/github_stub.py --port 8099``。

    然后让容器里的编排器指向它（``GITHUB_API_BASE=http://host.docker.internal:8099``）
    就能看到真实的退避重试——**日志里会有两条 ``github.retry``**。
    """
    parser = argparse.ArgumentParser(description="GitHub API 桩（限流、422、分页）")
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--rate-limit-times", type=int, default=2, help="前几次发布返回限流")
    parser.add_argument("--rate-limit-status", type=int, default=429, choices=[429, 403])
    parser.add_argument("--retry-after", default="1", help="retry-after 头的值（秒）")
    parser.add_argument("--unavailable-times", type=int, default=0)
    parser.add_argument("--reject-own-pr", action="store_true", help="REQUEST_CHANGES 自己的 PR 时返回 422")
    parser.add_argument("--reject-inline", action="store_true", help="带行内评论的 review 一律 422")
    parser.add_argument("--login", default="sfly-bot")
    args = parser.parse_args(argv)

    state = StubState(
        login=args.login,
        rate_limit_times=args.rate_limit_times,
        rate_limit_status=args.rate_limit_status,
        retry_after=args.retry_after,
        unavailable_times=args.unavailable_times,
        reject_own_pr=args.reject_own_pr,
        reject_inline=args.reject_inline,
    )
    server = _Server(("127.0.0.1", args.port), _Handler)
    server.state = state
    host, port = server.server_address[:2]
    assert isinstance(host, str)
    print(f"GitHub 桩已启动：http://{host}:{port}", flush=True)
    print(
        f"前 {state.rate_limit_times} 次 POST reviews 会返回 "
        f"{state.rate_limit_status}（retry-after: {state.retry_after}）",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
