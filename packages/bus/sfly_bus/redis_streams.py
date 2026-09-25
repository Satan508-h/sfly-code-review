"""Redis Streams 队列 —— 完整模式的 ``TaskQueue`` 与 ``Lock`` 实现。

    review_bootstrap   api → orchestrator
    review_tasks       orchestrator → worker（按 worker_type 分消费者组）
    review_results     worker → orchestrator
    dead_letter        任何一方 → 运维

**M0 只实现连接生命周期与健康探测。** 四条流的读写
（``XADD`` / ``XREADGROUP`` / ``XACK`` / ``XAUTOCLAIM`` / ``MAXLEN`` / 死信 /
重试计数）在 M3 补进这个文件 —— 方法会加到 ``RedisStreamsQueue`` 上，
``factory.py`` 与调用方都不需要改。之所以先把连接部分单独交付，是因为
「连不上时报得清楚」和「读得到消息」是两个独立的失败模式，
混在一起调试会分不清是哪一种。

### 为什么需要**两个**客户端

长驻的那个给队列用：没有 ``socket_timeout``（``XREADGROUP BLOCK`` 会
主动阻塞几秒，设了会把自己读超时掉）。

``ping()`` 用**另开一个短命客户端**，理由有两条：

1. 健康探测问的是「Redis 这个依赖现在可达吗」，而不是「我这个连接还好吗」。
   新建连接测的才是前者 —— 也正是一个新 Worker 启动时会遇到的情况。
2. 有了「用完就丢」这个前提，``asyncio.wait_for`` 超时取消才是安全的：
   被取消的命令会让那条连接进入未定义状态，对长驻连接是隐患，对马上要
   销毁的连接则无所谓。这样才能给健康接口一个**硬性的**时间上限，
   而不是依赖底层的 socket 超时。
"""

from __future__ import annotations

import asyncio
import contextlib
import time

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff, NoBackoff

from sfly_bus.health import CheckResult, down, ok
from sfly_shared.logging import get_logger

log = get_logger(__name__)

# 四条流的名字与消费者组名**不在这里定义** —— 它们是两种传输实现共用的命名
# 约定，放在 ``base.py``（和 Protocol 在一起）。审校时能看到「memory 和 redis
# 用的是同一套名字」，这一点比少写一行 import 重要得多。

#: 启动探测的连接超时。故意短：它只决定「多久之后放弃记录那条连通日志」，
#: 不决定任何功能是否可用。见 ``start()``。
_STARTUP_CONNECT_TIMEOUT_S = 3.0

#: 健康探测的整体硬上限（秒）。见模块文档里关于 wait_for 的说明。
_PING_TIMEOUT_S = 5.0
#: 探测用客户端的连接超时（秒）。
#:
#: **不能设成 2。** Windows 的 ``SelectorEventLoop`` 报告一个被拒绝的连接要花
#: 约 2.05 秒（实测：``asyncio.open_connection('127.0.0.1', 1)`` → 2.048s
#: 才抛 ``ConnectionRefusedError``；Linux 上是即时的）。如果连接超时正好是 2 秒，
#: redis-py 自己的超时会先触发，把唯一有用的 ``ConnectionRefusedError``
#: 替换成没有信息量的 ``TimeoutError: Timeout connecting to server``。
#: 4 秒给平台留出余量，同时仍然远小于任何真实网络故障的等待时间。
_PING_CONNECT_TIMEOUT_S = 4.0


class RedisStreamsQueue:
    """长驻的 Redis 客户端 + 四条流的读写。

    这个类同时充当 ``TaskQueue`` 和 ``Lock`` 的实现（锁用的也是同一条连接，
    而 ``SET NX PX`` 本来就不需要单独的连接池）。
    """

    def __init__(
        self,
        url: str,
        *,
        client_name: str = "sfly",
    ) -> None:
        self.url = url
        self.client_name = client_name
        self._client: aioredis.Redis | None = None

    # -- 生命周期 ---------------------------------------------------------- #

    async def start(self) -> None:
        """建连。

        连接是惰性的（``from_url`` 不产生网络 IO），所以这里第一次
        ``PING`` 只是为了在启动日志里留下一条**明确的**连通记录 ——
        否则一个连不上 Redis 的 Worker 会安安静静地起在那里，
        直到有人发现它什么都没干。

        **这里刻意用 ``retries=0``，和长驻客户端不一样。** 长驻客户端的重试是给
        运行期的网络抖动用的（那时重试有意义：消息还在流里等你）；启动这次探测
        只是要一条日志，重试它没有任何收益，代价却是实打实的：
        3 次重试 × 5 秒连接超时 = Redis 不可达时**每个服务卡在启动里 15 秒以上**。
        API 卡在 lifespan 里就意味着 Docker 的 healthcheck 迟迟不绿、
        ``tasks.py up --wait`` 一直不返回 —— 而这时候真正该发生的是
        「快点起来，然后在 ``/api/health`` 里说清楚 Redis 连不上」。
        """
        self._client = self._new_client(
            connect_timeout_s=_STARTUP_CONNECT_TIMEOUT_S,
            client_name=self.client_name,
            retries=0,
        )
        try:
            await self._client.ping()
            log.info("redis.connected", url=_safe_url(self.url), client_name=self.client_name)
        except Exception as exc:
            # 刻意**不抛**：起来之后靠 /api/health 报告，而不是崩溃重启。
            # 崩溃换来的是一串没有信息量的重启日志，这里换来一条能读的警告。
            log.warning(
                "redis.connect_failed",
                url=_safe_url(self.url),
                error=str(exc),
                error_class=type(exc).__name__,
            )

    async def close(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    @property
    def client(self) -> aioredis.Redis:
        """长驻客户端。M3 的四条流读写都走它。"""
        if self._client is None:
            raise RuntimeError("RedisStreamsQueue 尚未 start() —— 在 lifespan 里漏了？")
        return self._client

    # -- 健康 -------------------------------------------------------------- #

    async def ping(self) -> CheckResult:
        """探测 Redis 可达性并回报版本。

        用独立的短命客户端，不碰长驻连接 —— 理由见模块文档。
        连不上不抛异常：调用方就是为了知道「坏没坏」才调它的。
        """
        t0 = time.perf_counter()
        client = self._new_client(
            connect_timeout_s=_PING_CONNECT_TIMEOUT_S,
            client_name=f"{self.client_name}-health",
            retries=0,  # 见 _new_client：探测不重试，否则错误信息会被重试吃掉
        )
        try:
            # 一次 wait_for 罩住两个往返，而不是各罩一次 ——
            # 后者最坏情况是 2 × 超时，健康接口的时间上限就不再是那个常量了。
            info = await asyncio.wait_for(_ping_and_info(client), timeout=_PING_TIMEOUT_S)
        # 捕获一切是这里的**目的**，不是偷懒：探测函数抛异常等于把
        # 「Redis 坏了」变成「健康接口也坏了」，调用方就再也问不出话来了。
        except Exception as exc:
            latency = (time.perf_counter() - t0) * 1000
            log.warning("redis.ping_failed", error=str(exc), error_class=type(exc).__name__)
            return down("redis", _describe(exc), latency)
        finally:
            # 探测失败时连接可能处于半开状态，aclose 自己也可能抛 —— 都不该
            # 覆盖掉上面已经准备好的检查结果。
            with contextlib.suppress(Exception):
                await client.aclose()

        latency = (time.perf_counter() - t0) * 1000
        version = (info or {}).get("redis_version", "?")
        return ok("redis", f"Redis {version}", latency)

    # -- 内部 -------------------------------------------------------------- #

    def _new_client(self, *, connect_timeout_s: float, client_name: str, retries: int) -> aioredis.Redis:
        """建一个客户端。

        ``retries`` 是这里唯一一个**长驻连接和探测连接必须不同**的参数：

        * 长驻队列连接：3 次指数退避。网络抖一下不该让一条消息失败。
        * 健康探测：**0 次**。理由有两条。其一是探测问的是「现在可达吗」，
          用重试去粉饰它，等于把「不可达」报成「慢」。其二是 redis-py 8.x 的
          默认重试是 3 次指数退避，实测把一次 connect 失败拖成了 3 秒，
          直接吃掉 ``wait_for`` 的全部预算并被取消 —— 结果是错误信息为空，
          健康页显示 ``TimeoutError:`` 后面什么都没有，恰好丢掉了唯一有用的
          ``ConnectionRefusedError``。
        """
        if retries <= 0:
            retry = Retry(NoBackoff(), 0)
            retry_on_error: list[type[Exception]] = []
        else:
            retry = Retry(ExponentialWithJitterBackoff(base=0.1, cap=1.0), retries)
            # 连接类错误值得重试（网络抖动、Redis 主从切换）；协议类错误不值得。
            retry_on_error = [ConnectionError, TimeoutError]

        return aioredis.Redis.from_url(
            self.url,
            # 队列里流转的是 JSON 字符串。不解码的话每个消费点都要自己
            # ``.decode()``，漏一处就是「看起来像字符串的 bytes」这类难查的 bug。
            decode_responses=True,
            socket_connect_timeout=connect_timeout_s,
            # **不设 socket_timeout**：XREADGROUP BLOCK 是主动阻塞，
            # 设了会把自己读超时掉，表现为「消息偶尔丢」这种最难查的故障。
            retry=retry,
            retry_on_error=retry_on_error,
            # 空闲连接定期发 PING。Worker 的消费循环大部分时间在阻塞读上，
            # 中间的网络设备会悄悄掐掉空闲 TCP 连接。
            health_check_interval=30,
            # 出现在 `redis-cli client list` 里 —— 演示 --scale 时
            # 能一眼看出三个 Worker 副本都连上了。
            client_name=client_name,
        )


async def _ping_and_info(client: aioredis.Redis) -> dict[str, object]:
    """探测的两个往返。合成一个协程是为了让 ``wait_for`` 只罩一次。"""
    await client.ping()
    info = await client.info("server")
    return dict(info or {})


def _describe(exc: BaseException) -> str:
    """把异常拼成一句能读的说明。

    ``asyncio.TimeoutError`` 的 ``str()`` 是**空字符串** —— 直接拼进健康页
    会得到 ``TimeoutError:`` 后面什么都没有，看起来像 bug。这种情况退回报出异常的
    模块和类型，至少让人知道去哪查。
    """
    text = str(exc).strip()
    if text:
        return f"{type(exc).__name__}: {text}"
    return f"{type(exc).__name__}（{type(exc).__module__}，无附加信息）"


def _safe_url(url: str) -> str:
    """``redis://:password@host:6379/0`` → ``redis://host:6379/0``。

    日志会进容器 stdout，docker compose logs 里看得到；健康检查的脱敏
    （``health.redact``）管不到这里，所以单独处理一次。
    """
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, hostpart = rest.rpartition("@")
    return f"{scheme}://{hostpart}"
