"""全局测试配置。

**这个文件的第一段代码是整个单测套件能在 Windows 上跑起来的前提**，
所以放在了最前面。
"""

from __future__ import annotations

from sfly_shared.aio import install_loop_policy

# 必须在这里、在**任何事件循环被创建之前**执行。
#
# Windows 上 asyncio 默认用 ProactorEventLoop，而 psycopg v3 的异步模式
# 依赖 add_reader —— Proactor 不支持它。不换循环的话，任何碰 Postgres 的
# 测试都会在连接那一刻抛 "Psycopg cannot use the 'ProactorEventLoop'"，
# 看起来像是测试或代码写错了。
#
# conftest.py 是 pytest 进程里最早的执行点（早于收集测试、早于
# pytest-asyncio 建循环），所以放在这里。详见 sfly_shared/aio.py。
install_loop_policy()
