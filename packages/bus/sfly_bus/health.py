"""依赖健康检查的结果模型 —— 纯数据，不做任何 IO。

谁用它：``factory.py`` 汇总各依赖的检查结果，``/api/health`` 把它序列化出去，
前端首页把它渲染成一张表。

### 三种状态，而不是布尔

``ok`` / ``down`` 两态是不够的，会误导人。精简模式下**根本没有 Redis** ——
报 ``down`` 会让每次看健康页的人都以为坏了，报 ``ok`` 又是撒谎。
``SKIPPED`` 说的是实话：这个依赖在当前拓扑下不存在。

### 为什么用 dataclass 而不是 Pydantic

``contracts.py`` 里的领域契约用 Pydantic 是应该的（要校验、要序列化进队列）。
但健康检查是基础设施层的东西：它是**输出**不是**输入**，没有任何东西需要
校验它。``sfly_bus`` 因此不必直接依赖 pydantic —— 少一个依赖方向，
少一处将来会绕回来的循环。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

#: 连接串里的凭据。psycopg / redis 的报错信息通常只带 host:port，
#: 但**不能指望**这一点 —— ``/api/health`` 在公网演示站上是匿名可访问的，
#: 一条把密码漏出去的报错就是一次真实的凭据泄露。
#:
#: 覆盖两种写法：``scheme://user:pass@host`` 和 ``password=xxx`` 形式的参数。
_CREDENTIALS_IN_URL = re.compile(r"(?P<scheme>\w+(?:\+\w+)?://)[^/\s@]*@")
_CREDENTIALS_IN_KWARG = re.compile(r"(?P<key>\b(?:password|passwd|pwd)\s*=\s*)\S+", re.IGNORECASE)


def redact(text: str) -> str:
    """抹掉错误信息里可能出现的凭据。

    只在展示路径上调用。**不要**用它去改写日志 —— 日志里需要完整信息来排查问题，
    而日志留在服务端；这里的结果是要发给浏览器的。
    """
    text = _CREDENTIALS_IN_URL.sub(r"\g<scheme>***@", text)
    return _CREDENTIALS_IN_KWARG.sub(r"\g<key>***", text)


class CheckStatus(StrEnum):
    OK = "ok"
    DOWN = "down"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """一个依赖的检查结果。"""

    name: str
    """``postgres`` / ``redis`` —— 前端按这个名字做图标和排序。"""

    status: CheckStatus
    detail: str = ""
    """给人看的一行说明。成功时是版本号，失败时是**已脱敏**的报错。"""

    latency_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """``SKIPPED`` 算健康 —— 这个依赖在当前拓扑下本就不该存在。"""
        return self.status is not CheckStatus.DOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "ok": self.ok,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 1),
        }


def ok(name: str, detail: str = "", latency_ms: float = 0.0) -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.OK, detail=detail, latency_ms=latency_ms)


def down(name: str, detail: str, latency_ms: float = 0.0) -> CheckResult:
    """失败结果。**这里统一做脱敏**，调用方不需要记得这件事。"""
    return CheckResult(
        name=name,
        status=CheckStatus.DOWN,
        detail=redact(detail),
        latency_ms=latency_ms,
    )


def skipped(name: str, reason: str) -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.SKIPPED, detail=reason)


@dataclass(frozen=True, slots=True)
class HealthReport:
    """一次探测的汇总。

    ``ok`` 的语义是「**这套部署现在能干活吗**」，不是「进程还活着吗」。
    进程存活是 ``/healthz`` 的职责，两者刻意分开：
    Docker 的 ``restart`` 策略只该看存活，依赖挂了重启 API 是纯粹的故障扩大。
    """

    checks: list[CheckResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    @property
    def down(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status is CheckStatus.DOWN]

    def as_dict(self) -> dict[str, Any]:
        """展平成 ``{name: {...}}``，方便前端按名字取。

        同时给出一个 ``ok`` 汇总，让调用方不必自己遍历判断。
        """
        return {
            "ok": self.ok,
            "checks": {c.name: c.as_dict() for c in self.checks},
        }

    def summary(self) -> str:
        """一行式摘要，给 ``tasks.py health`` 用。"""
        parts = [f"{c.name}={c.status}" for c in self.checks]
        return " ".join(parts) if parts else "无依赖检查"
