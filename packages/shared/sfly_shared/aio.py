"""跨平台的事件循环安装 —— 一个不处理就会让整个项目在 Windows 上跑不起来的坑。

### 问题

Windows 上 asyncio 有**两种**事件循环实现，而默认那个不能用：

* ``ProactorEventLoop`` —— Windows 的默认值。基于 IOCP，支持子进程，
  **不支持 ``add_reader`` / ``add_writer``**。
* ``SelectorEventLoop`` —— 基于 ``select()``，支持 ``add_reader`` / ``add_writer``，
  不支持子进程。

psycopg v3 的异步模式是靠**文件描述符可读可写事件**实现的，也就是
``add_reader``。在 Proactor 上它连不上任何数据库，报：

    Psycopg cannot use the 'ProactorEventLoop' to run in async mode.

这个错误坑在它只在**真正去连数据库**的那一刻出现 —— 进程能起来、日志正常、
不碰数据库的健康检查也能过，然后在第一次查询时炸掉。本地开发时它看起来
像是「代码写错了」，而不是「平台的默认值不对」。

### 为什么不是「Windows 上就别用异步」

容器里跑的是 Linux，两种循环的实现差异在生产环境里根本不存在。
为了让 Windows 的开发体验一致而去掉异步，等于让开发和生产跑两套代码。
换循环是一行的事，改架构不是。

### 顺序要求

``install_loop_policy()`` 改的是**进程级**设置，必须在**创建任何事件循环之前**
调用。放在 ``conftest.py`` 和各个 ``__main__.py`` 的顶部 —— 那是各自进程里
最早的执行点。

### 3.14 的迁移路径

``set_event_loop_policy`` 从 3.14 起被弃用，3.16 移除，替代品是
``asyncio.run(..., loop_factory=...)``（3.12 起可用）。所以 ``run()`` 优先用
``loop_factory``；``install_loop_policy()`` 只在**不得不改进程级策略**时使用
（uvicorn 的场景，见下）。
"""

from __future__ import annotations

import asyncio
import sys
import warnings
from collections.abc import Coroutine
from typing import Any

#: 只有这个平台需要换循环。写成具名常量而不是到处散落
#: ``sys.platform == "win32"``，是为了让 grep 一次找全所有相关位置。
NEEDS_SELECTOR_LOOP = sys.platform == "win32"


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """给 ``asyncio.run(..., loop_factory=...)`` 用。

    不用 ``asyncio.new_event_loop()``：那个走的是当前策略，而当前策略
    在 Windows 上正是我们要绕开的 Proactor。
    """
    return asyncio.SelectorEventLoop()


def install_loop_policy() -> None:
    """把当前进程的事件循环策略换成 Selector（仅 Windows）。

    已经是 Selector 时是 no-op，重复调用安全。非 Windows 上直接返回。

    **为什么需要这个而不只是用 ``run()``**：uvicorn 在 Windows 上把循环工厂
    **写死**成了 ProactorEventLoop（``uvicorn/loops/asyncio.py``，为了让
    ``--reload`` / ``--workers`` 能用上子进程）。它不读全局策略 ——
    它给 ``asyncio.run`` 显式传 ``loop_factory``，把策略整个绕过去了。
    所以想让 uvicorn 跑在 Selector 上，唯一的办法是在它之前改策略。
    """
    if not NEEDS_SELECTOR_LOOP:
        return
    policy_cls = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy_cls is None:  # pragma: no cover —— 3.16 移除后的分支
        return
    if isinstance(asyncio.get_event_loop_policy(), policy_cls):
        return
    with warnings.catch_warnings():
        # 3.14 起这里会发 DeprecationWarning。压制它是因为迁移需要先改
        # uvicorn 的启动方式；让每次启动都刷一行警告，只会训练人们忽略警告。
        warnings.simplefilter("ignore", DeprecationWarning)
        asyncio.set_event_loop_policy(policy_cls())


def run[T](coro: Coroutine[Any, Any, T], *, debug: bool = False) -> T:
    """``asyncio.run`` 的跨平台版本。所有 ``__main__.py`` 都该用它。"""
    if NEEDS_SELECTOR_LOOP:
        return asyncio.run(coro, debug=debug, loop_factory=selector_loop_factory)
    return asyncio.run(coro, debug=debug)
