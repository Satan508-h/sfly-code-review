"""GitHub 客户端的异常层次。

### 为什么这棵树不从 ``sfly_shared.errors`` 里那几个同名的类继承

共享层已经声明了 ``AuthRevokedError`` / ``RepoNotFoundError`` / ``PublishError``，
而它们的文档字符串本来就是为 M7 写的（「GitHub token 被撤销或权限不足」）。
让 ``GitHubAuthError`` 同时继承 ``GitHubError`` 和 ``AuthRevokedError`` 是能跑通的，
但 ``error_class`` 会变成**靠 MRO 解析出来的值**：

    PublishError 自己没有 error_class（它继承 SflyError 的 TRANSIENT）
    → 属性查找沿着 MRO 继续走到 AuthRevokedError，才拿到 AUTH_REVOKED

那是对的 —— 但它对的路径经过三个类，任何一次继承顺序调整都会让它**静默地**
退回 ``transient``，而 ``transient`` 在 Worker 那一侧的语义是「可重试」：
于是 401 会被重试三次，每一次都必然失败。这种错误不会有任何东西报错。

所以这里是一棵独立的树，**每个类都把自己的 ``error_class`` 写出来**，
取值仍然来自同一套 ``ErrorClass`` 枚举。代价是重复七行声明，换来的是
「这个异常是什么类别」在一屏之内可见。

### ``retryable`` 在这里的含义与 Worker 里不同

Worker 的 ``retryable`` 决定「要不要重新投递这条消息」。这里是**给人和 UI 看的**：

* ``GitHubRateLimitError`` / ``GitHubUnavailableError`` —— ``retryable=True``：
  这次发布失败是暂时的，界面上那个「重新发布」按钮值得点。
* 其余的 ``retryable=False``：重试一万次也是同样的结果，按钮点了只会再红一次。

客户端自己**不会**用这个字段决定重试 —— 它用的是 :func:`sfly_agent.github.client.retry_delay_s`
（那要看响应头才知道）。两件事分开写，是因为一个来自策略、一个来自这次的响应。
"""

from __future__ import annotations

from typing import Any

from sfly_shared.contracts import ErrorClass
from sfly_shared.errors import SflyError

#: 响应体在异常消息里保留多少字符。够看出「哪个 token 权限不对」和
#: 「哪一行的行号不对」的区别就行 —— 和 ``llm/openai_compat.py`` 同一个理由。
BODY_SNIPPET = 300


class GitHubError(SflyError):
    """客户端抛出的**全部**异常。

    节点里因此只需要一句 ``except GitHubError`` 就能接住所有情况 ——
    这正是把它做成单一根节点的理由：publish 的失败处理只有一种
    （记 ``publish_failed``、报告留在库里、等重新发布），
    在 ``except`` 里再分叉只会分叉出没人测过的分支。

    ``retryable`` 默认 **False**：新增一个子类时忘了写，
    得到的行为是「不重试」—— 而失败方向是安全的那个（报告已经落库，
    ``publish_failed`` 是可见的、可重发的）。反过来默认 True 则会让
    一个没人看懂的失败白等三轮退避。
    """

    retryable = False
    error_class: ErrorClass = ErrorClass.TRANSIENT

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body[:BODY_SNIPPET]


class GitHubAuthError(GitHubError):
    """401 / 403（且不是限流）。

    403 的两种身份见 :func:`sfly_agent.github.client.retry_delay_s` ——
    按键头 ``x-ratelimit-remaining`` 区分，这里是「不是限流」的那一支。
    """

    error_class = ErrorClass.AUTH_REVOKED


class GitHubNotFoundError(GitHubError):
    """404：仓库、PR 或评论不存在。"""

    error_class = ErrorClass.REPO_NOT_FOUND


class GitHubValidationError(GitHubError):
    """422：GitHub 看懂了请求，但拒绝执行。

    两种最常见的来源，**处置方式完全不同**（调用方靠
    :attr:`errors` 里的 message 区分）：

    * ``Can not request changes on your own pull request`` —— 机器人账号
      和开 PR 的是同一个人。降级成 ``COMMENT`` 重发。
    * 行号不在 diff 里（``path``/``line`` 指向未变更的行）—— 去掉行内评论、
      只发汇总。
    """

    error_class = ErrorClass.TRANSIENT

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        body: str = "",
        errors: list[Any] | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, body=body)
        self.errors: list[Any] = list(errors or [])

    def mentions(self, needle: str) -> bool:
        """GitHub 有没有在这条 422 里提到某个短语。

        **刻意只做子串匹配，而不是解析结构化字段**：``errors[]`` 的形状在
        不同端点之间并不一致（有的是 ``{resource, field, code}``，有的只有
        ``{message}``），而我们要问的问题只有一个 —— 「它是不是在说
        『不能给自己请求修改』」。把这句话当成一个字符串常量去比，
        比去猜字段名稳。匹配不上时调用方走的是**安全的那条**路径
        （去掉行内评论重发），所以这里不需要穷尽。
        """
        haystack = " ".join([str(self), *(str(e) for e in self.errors)]).lower()
        return needle.lower() in haystack


class GitHubRateLimitError(GitHubError):
    """429，或 ``403 + x-ratelimit-remaining: 0``。

    两种都出现过，而且 GitHub 用同一个状态码表示两类限流
    （二级限流的响应头是 ``retry-after``，一级限流给的是
    ``x-ratelimit-reset``）—— 见 :func:`retry_delay_s`。
    """

    retryable = True
    error_class = ErrorClass.TRANSIENT


class GitHubUnavailableError(GitHubError):
    """5xx，或者压根没连上（DNS、超时、TLS）。"""

    retryable = True
    error_class = ErrorClass.TRANSIENT
