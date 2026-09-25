"""集成测试用的 Redis 铺路石。

**测试跑在 db 15，不是 db 0。** 每个测试开始前会 ``FLUSHDB`` 一次 ——
而这个动作本身足以说明为什么库号要单独拎出来：开发机上很可能同时跑着一套
``docker compose up`` 的栈，对着 db 0 清一次就等于把正在跑的 run 全清了。

所以下面每一条会写数据的函数都先过一遍 :func:`assert_test_db`：只有确认
URL 指向的库号**正好**是 :data:`TEST_DB` 才动手。一道写错的配置不该能删掉
线上的数据 —— 这类保护的成本是几行代码，收益是「不可能发生」。
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import redis

from sfly_shared.config import get_settings

#: 集成测试专用的库号。Redis 默认有 16 个库（0-15）。
TEST_DB = 15

#: 探测用的短超时。连不上就立刻报错，不要让人等 redis-py 的重试。
_CONNECT_TIMEOUT_S = 2.0


def redis_test_url() -> str:
    """``REDIS_URL``，但库号换成 :data:`TEST_DB`。

    从 ``Settings`` 取而不是直接读环境变量：开发机的 ``.env`` 把宿主机端口
    改成了 56379（5432/6379 被另一个项目占着），而 CI 里没有 ``.env``、
    用默认的 6379。两条路径都由 ``Settings`` 负责，这里不重复一遍。
    """
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{TEST_DB}", "", ""))


def _db_of(url: str) -> int:
    path = urlsplit(url).path.lstrip("/")
    return int(path) if path else 0


def assert_test_db(url: str) -> None:
    if _db_of(url) != TEST_DB:
        raise RuntimeError(
            f"拒绝在 db {_db_of(url)} 上做集成测试的清理动作 —— 只允许 db {TEST_DB}。（url={url}）"
        )


def _client(*, timeout: float = _CONNECT_TIMEOUT_S) -> redis.Redis:
    """**同步**客户端。

    刻意不用异步的：这些是「测试开始前扫一下地」的活，而 pytest-asyncio 的
    异步 fixture 要绑定到正确的事件循环上（会话级 fixture 绑到函数级循环是它
    最经典的坑）。同步客户端不参与事件循环，两边都不欠。
    """
    return redis.Redis.from_url(
        redis_test_url(),
        decode_responses=True,
        socket_connect_timeout=timeout,
        socket_timeout=timeout,
    )


def probe() -> tuple[bool, str]:
    """Redis 可达吗？返回 ``(可达, 说明)``。

    不抛异常：调用它的是「整个会话该不该跑」的判断，而失败时最需要的是
    **一句能读的原因**，不是一串回溯。
    """
    client = _client()
    try:
        info = client.info("server")
        return True, f"Redis {info.get('redis_version', '?')} @ {redis_test_url()}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        client.close()


def flush_test_db() -> None:
    """清空测试库。**只在库号确实是 TEST_DB 时动手。**"""
    url = redis_test_url()
    assert_test_db(url)
    client = _client()
    try:
        client.flushdb()
    finally:
        client.close()


def raw_client() -> redis.Redis:
    """给「只能用原生命令才能构造出来的输入」用（比如手工 ``XADD`` 一条坏 payload）。

    正常路径一律走 ``RedisStreamsQueue`` 自己 —— 伸手进原生客户端的测试必须
    说清楚为什么，否则它慢慢就变成了「绕过实现测实现」。
    """
    return _client()
