"""集成测试的公共设施：确认 Redis 可达，然后每个测试前扫一次地。

### 依赖不可达时是**失败**，不是 skip

``pytest.skip`` 在这里是错的：跑 ``python tasks.py test-int`` 的人明确要求了
「需要外部依赖」的那一层测试。把依赖缺失报成 skip，结果是
``30 skipped`` 配一个绿色的退出码 —— 而这是**假装有保护**最典型的样子，
CLAUDE.md 里那句「声明了却在别处不执行的门槛比没有门槛更糟」说的就是这个。

所以这里用 ``pytest.exit`` 直接停下整个会话，给一条能照做的提示。它比
「每个测试各失败一次」好：后者会在屏幕上刷 30 遍同一句话，真正的原因反而被埋掉。
"""

from __future__ import annotations

import pytest

# 空行不是随手加的：``redis_support`` 住在 ``tests/`` 下，而 ``tests`` 是
# ruff 配置里的一个 src 根，所以它被算作**本仓库的模块**、要和第三方分开。
from redis_support import flush_test_db, probe


@pytest.fixture(scope="session", autouse=True)
def _require_redis() -> None:
    ok, detail = probe()
    if not ok:
        pytest.exit(
            "Redis 不可达，集成测试无法运行：\n"
            f"  {detail}\n"
            "  先起依赖：python tasks.py up redis\n"
            "  （测试跑在 db 15，与开发用的 db 0 分开）",
            returncode=1,
        )


@pytest.fixture(autouse=True)
def _clean_redis() -> None:
    """每个测试开始前清空测试库。

    每个测试自带干净状态，测试之间就不会通过数据库互相串联 —— 那种耦合的表现是
    「单跑通过、全跑失败」，而定位它要花的时间远超过这里每次多一次 FLUSHDB。
    """
    flush_test_db()
