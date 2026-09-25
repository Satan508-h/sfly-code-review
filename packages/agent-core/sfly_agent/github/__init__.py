"""GitHub 客户端与它的异常。

导入面只有两个名字：客户端本身，和那棵异常树的根。**不 re-export 每一个
子类** —— 调用方（``publish`` 节点）只做一件事：``except GitHubError``
把它翻译成 ``publish_failed``。要区分具体是哪一种失败的场合，
细节在异常对象上（``retryable`` / ``status_code``），不在 import 列表里。
"""

from __future__ import annotations

from sfly_agent.github.client import GitHubClient, ReviewEvent
from sfly_agent.github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubUnavailableError,
    GitHubValidationError,
)

__all__ = [
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "GitHubValidationError",
    "ReviewEvent",
]
