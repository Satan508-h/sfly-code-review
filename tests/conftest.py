"""全局测试配置。

**这个文件的第一段代码是整个单测套件能在 Windows 上跑起来的前提**，
所以放在了最前面。
"""

from __future__ import annotations

import sys
from pathlib import Path

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

# --- 让 tests/ 成为可导入的根 ------------------------------------------------ #
#
# ``tests/contracts/`` 里的契约模块被 ``tests/unit`` 和 ``tests/integration``
# 同时导入，而 pytest 默认只把**每个测试文件自己所在的目录**塞进 sys.path
# （靠「往上找到第一个没有 __init__.py 的目录」来定这个目录）。
# 于是 ``from contracts.queue_contract import ...`` 在 unit 下能跑、在
# integration 下就报 ModuleNotFoundError —— 而这条 import 恰恰是
# 「一套代码、两种拓扑」的证据本身，不该被目录结构卡住。
#
# 显式加一行，而不是依赖 pytest 顺带把 conftest 所在目录插进来：那个行为是
# 实现细节，而且哪天有人给某个目录补上 __init__.py 就会悄悄改变。
sys.path.insert(0, str(Path(__file__).resolve().parent))
