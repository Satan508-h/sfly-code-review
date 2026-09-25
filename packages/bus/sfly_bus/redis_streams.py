"""Redis Streams 队列与锁 —— 完整模式的 ``TaskQueue`` / ``Lock`` 实现。

    review_bootstrap   api → orchestrator
    review_tasks       orchestrator → worker（按 worker_type 分消费者组）
    review_results     worker → orchestrator
    dead_letter        任何一方 → 运维

这个模块交付两个类：:class:`RedisStreamsQueue` 和 :class:`RedisLock`。
它们各自持有连接（锁不值得和队列共享一条连接池 —— 一次 ``SET NX`` 和一次
阻塞 500ms 的 ``XREADGROUP`` 挤在同一个池子上，排障时很难说清是谁在等谁）。

### 这套实现的正确性靠什么

* **投递顺序铁律**（CLAUDE.md 约定 #1）落在调用方，不在这里：队列只管把消息
  送到。``store → XADD → XACK`` 的顺序由 ``WorkerRunner`` 保证。
* **幂等**不靠 Redis。``worker_results`` 的主键 + ``ON CONFLICT DO NOTHING``
  才是保证；这里的重复投递（回收、重放）因此都是**安全的**，只是浪费 token。
  这个前提让 ``reclaim`` 的阈值可以实测调参，而不必先证明它是对的。
* **重试计数**用 ``INCR sfly:attempts:{stream}:{msg_id}`` 显式记，**不解析
  ``XPENDING`` 的投递次数**。后者在同一组里是准的，但回收、认领、多组扇出都会
  改动它，而我们需要的是一个「这条消息被投出去过几次」的单调计数。

### 两个容易写错的地方，写在这里省得下次再踩

**一、阻塞读必须短。** ``XREADGROUP BLOCK`` 的时长决定了两件事：关机能多快
叫醒消费者（``close()`` 之后最多等一个阻塞周期），以及 ``reclaim()`` 认领回来
的消息要多久才会被下一次消费循环取走（认领不会产生新条目，因此**不会**唤醒
阻塞中的消费者 —— 它只能等自己醒）。所以 ``_BLOCK_MS`` 取 500ms：
每秒钟多两次空转的往返，换来的是这两件事的延迟上界都是 500ms。

**二、``consume_*`` 有两种消息来源。** ``XREADGROUP`` 拿新消息，``XAUTOCLAIM``
拿回收的旧消息 —— 后者**直接把消息返回**，与 ``XREADGROUP`` 是两条不同的读路径。
所以 ``reclaim()`` 只能把认领到的消息先缓冲起来，等下一次 ``consume_*`` 时再交出
（这是 ``base.py`` 里 ``reclaim`` 「返回计数、不返回消息」那条决定的代价）。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import socket
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff, NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError

from sfly_bus.base import (
    CONSUMER_GROUPS,
    STREAMS,
    WORKER_TYPES,
    MessageHandle,
    dead_letter_fields,
    group_for,
)
from sfly_bus.health import CheckResult, down, ok
from sfly_shared.contracts import (
    BootstrapMessage,
    ErrorClass,
    TaskMessage,
    WorkerResult,
    WorkerType,
)
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

#: ``XREADGROUP BLOCK`` 的时长（毫秒）。**这是本模块最需要权衡的一个常量**，
#: 理由见模块文档「阻塞读必须短」那一段。500ms 是「每秒两次空转」和
#: 「关机/回收的延迟上界」之间的折中，而契约测试给关闭留的预算是 2 秒。
_BLOCK_MS = 500

#: ``XAUTOCLAIM`` 单次扫描的条数，以及一次 ``reclaim()`` 最多扫几批。
#: 上限是必需的：一个百万条的 PEL 会让不加限制的扫描把回收协程卡住，
#: 而回收协程卡住的表现是「消息不再被重投」—— 比慢得多更糟。
#: 剩下的部分下一轮（``RECLAIM_INTERVAL_S``，默认 30 秒）继续。
_CLAIM_BATCH = 100
_CLAIM_SCAN_LIMIT = 10

#: 投递计数器的存活时间。它只在消息还挂在 PEL 上时有用，所以给一个远大于
#: 任何合理处理时长的值就够了 —— 计数器过期后重试次数会从 1 重新数，
#: 而那只会让一条卡死的消息多试几轮，不会产出错数据。
_ATTEMPT_TTL_S = 7 * 24 * 3600

#: 释放锁的 Lua。**必须是原子的「比对再删」**：先 GET 再 DEL 会有一个窗口，
#: 让一个已经超时（很可能已被别人重新拿到）的锁被前一个持有者删掉。
_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


#: 主机名（截到 12 位 —— Docker 的容器 id 就取前 12 位）。见 ``_next_consumer``。
#:
#: 过滤成 ASCII：开发机的计算机名可以是中文，而中文消费者名在 ``XINFO``
#: 和日志里都要过一遍控制台编码 —— 编码失败抛在日志里是最不该崩的地方。
_HOST = re.sub(r"[^A-Za-z0-9-]", "", socket.gethostname())[:12] or "host"


def _attempt_key(stream_key: str, msg_id: str) -> str:
    """投递计数的键。

    带上流名，因为**不同流里的消息 id 可以相同**（``XADD`` 允许显式指定 id，
    而测试就会这么做）。不带流名的话，``review_tasks`` 里的一条 ``1-1`` 会读到
    ``review_results`` 里那条 ``1-1`` 的计数，于是 attempt 从一个莫名其妙的
    数字开始 —— 而它正是死信判定的依据。
    """
    return f"sfly:attempts:{stream_key}:{msg_id}"


def _clean_fields(fields: Any) -> dict[str, str]:
    """把一条流条目的字段规整成 ``dict[str, str]``。

    两个作用，都是必需的：

    * **``None`` 值**：条目已被裁剪/删除时，Redis 会给出 nil 值（老版本把它作为
      「已认领的条目」返回，7.0+ 才改用返回值第三段报已删 id）。转成空串之后，
      消费循环那条 ``if not fields.get("payload")`` 才接得住它。
    * **类型收窄**：``XADD`` 允许写数字，所以字段的值在类型上是 ``str | int | float``。
      契约里全是字符串，统一 str 一下，下游就不必各自防御。
    """
    return {str(k): ("" if v is None else str(v)) for k, v in dict(fields).items()}


def _redis_fields(fields: dict[str, str]) -> Any:
    """把字段字典交给 redis-py。

    它的类型标注是 ``dict[bytes | str | int | float, ...]``，而 ``dict`` 在类型上
    是**不变**的 —— 于是 ``dict[str, str]`` 传不进去（运行时毫无问题：Redis 的流
    字段本来就只接受字符串和数字）。转一次 ``Any``，而不是在每个调用点撒
    ``# type: ignore``。
    """
    return fields


@dataclass(slots=True)
class _Claimed:
    """``reclaim()`` 认领回来、等着下一次 ``consume_*`` 交出去的一条消息。"""

    msg_id: str
    fields: dict[str, str]


class _RedisHandle:
    """``MessageHandle`` 的 Redis 实现。

    ``ack()`` 必须幂等：被回收之后原消费者和新消费者都可能 ack 同一条消息，
    而 ``XACK`` 对不在 PEL 里的 id 就是返回 0。
    """

    __slots__ = ("_fields", "_group", "_msg_id", "_queue", "_stream", "attempt", "id")

    def __init__(
        self,
        queue: RedisStreamsQueue,
        stream: str,
        group: str,
        msg_id: str,
        fields: dict[str, str],
        *,
        attempt: int,
    ) -> None:
        self._queue = queue
        self._stream = stream
        self._group = group
        self._msg_id = msg_id
        self._fields = fields
        self.id = msg_id
        self.attempt = attempt

    async def ack(self) -> None:
        await self._queue.xack(self._stream, self._group, self._msg_id)

    async def to_dead_letter(self, error: str, error_class: ErrorClass) -> None:
        await self._queue.dead_letter(
            self._stream, self._group, self._msg_id, self._fields, self.attempt, error, error_class
        )
        await self.ack()


class RedisStreamsQueue:
    """``TaskQueue`` 的 Redis Streams 实现。"""

    def __init__(
        self,
        url: str,
        *,
        client_name: str = "sfly",
        claim_idle_ms: int = 180_000,
        stream_maxlen_tasks: int = 10_000,
        stream_maxlen_results: int = 10_000,
    ) -> None:
        self.url = url
        self.client_name = client_name
        #: ``XAUTOCLAIM`` 的空闲阈值。低于 60s 会偷走在跑的活；高于 300s
        #: 会让 Worker 猝死后恢复变慢。回收早了只浪费 token，不产出错数据。
        self.claim_idle_ms = claim_idle_ms
        self._maxlen_tasks = stream_maxlen_tasks
        self._maxlen_results = stream_maxlen_results
        self._client: aioredis.Redis | None = None
        #: ``(stream, group) -> 认领回来的消息``。见模块文档第二点。
        self._claimed: dict[tuple[str, str], deque[_Claimed]] = {}
        self._consumer_seq = 0
        self._started = False
        self._groups_ready = False
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------- #

    async def start(self) -> None:
        """建连、建消费者组。

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
        if self._closed:
            raise RuntimeError("这份 RedisStreamsQueue 已经 close() 过，生命周期是一次性的")
        if self._started:
            return
        self._started = True
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
        # 建组也是 best-effort：Redis 这会儿不可达时不该拦住进程启动。
        # 真正的消费路径会再试一次（``_ensure_groups``），而那时报出来的错
        # 更有用 —— 至少能看出是哪个组建不出来。
        with contextlib.suppress(Exception):
            await self._ensure_groups()

    async def close(self) -> None:
        """置关闭标志并释放连接。

        **不等待**阻塞中的消费者：``aclose()`` 会掐断正在阻塞的那次
        ``XREADGROUP``，它会以 ``ConnectionError`` 结束 —— 而消费循环看到
        关闭标志已经立起来，就把它当正常的退出信号（见 ``_consume``）。
        等一个阻塞周期（500ms）再关当然也能工作，但那意味着每次关机都慢半拍，
        而慢半拍的关机路径在容器编排里会被反复触发。
        """
        if self._closed:
            return
        self._closed = True
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None
        self._claimed.clear()
        log.info("redis_queue.closed")

    @property
    def client(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("RedisStreamsQueue 尚未 start() —— 在 lifespan 里漏了？")
        return self._client

    # -- 发布 -------------------------------------------------------------- #

    async def publish_bootstrap(self, msg: BootstrapMessage) -> str:
        # 不裁剪：裁掉一条就等于丢一个 run，而它的流量是「每个 PR 一条」。
        return await self._xadd(STREAMS["bootstrap"], msg.to_stream_fields(), None)

    async def publish_task(self, msg: TaskMessage) -> str:
        return await self._xadd(STREAMS["tasks"], msg.to_stream_fields(), self._maxlen_tasks)

    async def publish_result(self, result: WorkerResult) -> str:
        return await self._xadd(STREAMS["results"], result.to_stream_fields(), self._maxlen_results)

    # -- 消费 -------------------------------------------------------------- #

    def consume_bootstrap(self) -> AsyncIterator[tuple[MessageHandle, BootstrapMessage]]:
        return self._consume(STREAMS["bootstrap"], CONSUMER_GROUPS["bootstrap"], BootstrapMessage)

    def consume_tasks(
        self, worker_type: WorkerType | str
    ) -> AsyncIterator[tuple[MessageHandle, TaskMessage]]:
        return self._consume(
            STREAMS["tasks"],
            group_for(worker_type),
            TaskMessage,
            only_worker_type=str(worker_type),
        )

    def consume_results(self) -> AsyncIterator[tuple[MessageHandle, WorkerResult]]:
        return self._consume(STREAMS["results"], CONSUMER_GROUPS["results"], WorkerResult)

    # -- 运维 -------------------------------------------------------------- #

    async def reclaim(self, worker_type: WorkerType | str | None = None) -> int:
        """``XAUTOCLAIM`` 回收空闲超时的待确认消息，返回**重新变得可投递**的条数。

        注意返回值刻意不含「被 Redis 从 PEL 里清掉的已删条目」—— 那些消息
        已经不在流里了，报成「回收了 1 条」会让运维以为有活重新排上了队。
        它们单独计一条 warning 日志。

        认领不会唤醒阻塞中的消费者（``XAUTOCLAIM`` 不产生新条目），所以这些
        消息要等消费循环下一次醒过来才被取走 —— 上界就是 ``_BLOCK_MS``。
        """
        self._require_usable()
        await self._ensure_groups()
        # 认领需要一个消费者名。用独立的名字而不是某个消费循环的，是为了在
        # `XINFO CONSUMERS` 里一眼看出「这几条是被回收搬过来的」。
        consumer = self._next_consumer("reclaim")
        claimed = 0
        purged = 0
        for stream_key, group in self._target_groups(worker_type):
            got, gone = await self._claim(stream_key, group, consumer)
            claimed += got
            purged += gone
        if claimed or purged:
            log.info(
                "redis_queue.reclaimed",
                claimed=claimed,
                purged=purged,
                worker_type=str(worker_type or "*"),
                consumer=consumer,
            )
        return claimed

    async def lag(self, worker_type: WorkerType | str) -> int:
        """``XINFO GROUPS`` 的 ``lag``：还有多少条没投给这个组。"""
        self._require_usable()
        await self._ensure_groups()
        want = group_for(worker_type)
        for info in await self.client.xinfo_groups(STREAMS["tasks"]):
            if info.get("name") != want:
                continue
            value = info.get("lag")
            if value is None:
                # NULL 出现在组的游标指向的那条已经被裁掉的时候。
                # 兜底的算法与 lag 的口径一致：数游标之后还剩几条。
                return await self._count_after(STREAMS["tasks"], str(info.get("last-delivered-id", "0-0")))
            return int(value)
        # 组还没建出来 —— 说明什么都没发布过，积压当然是 0。
        return 0

    async def trim(self, stream_key: str, maxlen: int = 0) -> int:
        """精确裁剪（``XTRIM MAXLEN n``，不带 ``~``）。见 Protocol 的说明。"""
        self._require_usable()
        return int(await self.client.xtrim(stream_key, maxlen=maxlen, approximate=False))

    async def dead_letters(self) -> list[dict[str, str]]:
        """死信，按写入顺序。等价于 ``XRANGE dead_letter - +``。

        **只读**：不建消费者组、不推进任何游标。运维排查死信时用的就是这一条，
        而不是 ``XREADGROUP`` —— 后者会让死信从这个组的视角消失。
        """
        entries = await self.client.xrange(STREAMS["dead_letter"])
        return [_clean_fields(fields) for _msg_id, fields in (entries or [])]

    async def pending_count(self, worker_type: WorkerType | str) -> int:
        """该组的 PEL 深度。**不是** ``lag``：它数的是「投出去还没 ack 的」，
        而 ``lag`` 数的是「还没投出去的」。消费者卡住时前者在涨、后者是 0。

        与内存实现的同名方法对称（不在 Protocol 里，给测试与本机排查用）。
        """
        summary: Any = await self.client.xpending(STREAMS["tasks"], group_for(worker_type))
        # RESP2 返回 ``[count, min, max, consumers]``，RESP3 返回一个 dict。
        # 两种都接住 —— 这个形状取决于连接协商的协议版本，而协议版本取决于
        # redis-py 的版本，不该让一个排查工具因此静默返回 0。
        if isinstance(summary, dict):
            return int(summary.get("pending") or 0)
        if isinstance(summary, (list, tuple)) and summary:
            return int(summary[0] or 0)
        return 0

    # -- 健康 -------------------------------------------------------------- #

    async def ping(self) -> CheckResult:
        """探测 Redis 可达性并回报版本。

        用独立的短命客户端，不碰长驻连接 —— 探测问的是「Redis 这个依赖现在
        可达吗」，而不是「我这个连接还好吗」。也正是有了「用完就丢」这个前提，
        ``asyncio.wait_for`` 超时取消才是安全的：被取消的命令会让那条连接进入
        未定义状态，对长驻连接是隐患，对马上要销毁的连接则无所谓。
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

    # -- 内部：发布 -------------------------------------------------------- #

    async def _xadd(self, stream_key: str, fields: dict[str, str], maxlen: int | None) -> str:
        self._require_usable()
        if maxlen is None:
            msg_id = await self.client.xadd(stream_key, _redis_fields(fields), id="*")
        else:
            # ``approximate=True`` 生成 ``MAXLEN ~ n``：裁剪按宏节点整块进行，
            # 不为「精确到 n 条」而变慢。代价是实际长度会略多于 n，
            # 所以任何「第 N 条一定被裁掉」的断言都必须用 ``trim()`` 精确裁。
            msg_id = await self.client.xadd(
                stream_key, _redis_fields(fields), id="*", maxlen=maxlen, approximate=True
            )
        return str(msg_id)

    # -- 内部：消费 -------------------------------------------------------- #

    async def _consume[MsgT: (BootstrapMessage, TaskMessage, WorkerResult)](
        self,
        stream_key: str,
        group: str,
        model: type[MsgT],
        *,
        only_worker_type: str | None = None,
    ) -> AsyncIterator[tuple[MessageHandle, MsgT]]:
        self._require_usable()
        await self._ensure_groups()
        consumer = self._next_consumer(group)
        while not self._closed:
            try:
                entry = self._pop_claimed(stream_key, group)
                if entry is not None:
                    # 认领和投递之间隔着一段时间，原消费者完全可能在这段时间里
                    # ack 掉它（LLM 跑得慢、回收阈值又短时这是常态，不是边界情况）。
                    if not await self._still_pending(stream_key, group, entry.msg_id):
                        log.debug("redis_queue.stale_claim_dropped", msg_id=entry.msg_id)
                        continue
                    msg_id, fields = entry.msg_id, entry.fields
                else:
                    fresh = await self._read_new(stream_key, group, consumer)
                    if fresh is None:
                        continue
                    msg_id, fields = fresh
            except (RedisConnectionError, RedisTimeoutError) as exc:
                # close() 会关掉连接池，正在阻塞的这次读就以连接错误结束。
                # 判据是**关闭标志是不是已经立起来了**：立了就说明是我们自己关的，
                # 安静退出；没立起来就是真的网络故障，照抛不误（在抛之前留一条
                # 日志，因为消费者退出得静悄悄是这里最危险的失败模式）。
                #
                # 注意捕的是 **redis 的** ConnectionError，不是内置的那个 ——
                # 它俩没有继承关系（redis 的继承自 RedisError），写成内置的
                # 版本会让这个分支永远进不来，而症状恰好是「关机能关掉，
                # 但网络闪断时消费者静默退出」这种最难看出来的不对称。
                if self._closed:
                    return
                log.error("redis_queue.consume_failed", group=group, error=str(exc))
                raise
            except ResponseError as exc:
                if "NOGROUP" not in str(exc):
                    raise
                # 组被谁删了（运维手动 XGROUP DESTROY，或整个 Redis 被换成了一份
                # 新数据）。重建一次然后继续 —— 让消费循环因此崩掉是很难查的故障。
                self._groups_ready = False
                await self._ensure_groups()
                continue

            # 别的 worker_type 的活：判掉并 ack。组是独立游标而不是分工，所以
            # 同一条任务会被三个组各读一遍 —— 不 ack 的话这个组的 lag 会永远
            # 挂着一条它永远不会处理的消息，健康检查就一直在报假警。
            if only_worker_type is not None and fields.get("worker_type") != only_worker_type:
                await self.xack(stream_key, group, msg_id)
                continue

            if not fields.get("payload"):
                # 空 payload：这条消息已经不在流里了，只剩一个 id 挂在 PEL 上。
                # **绝不能交给调用方** —— 报出来会是一个和真因毫无关系的
                # 「缺字段」错误，而真因在传输层。
                log.warning("redis_queue.empty_payload_acked", msg_id=msg_id, group=group)
                await self.xack(stream_key, group, msg_id)
                continue

            try:
                msg = model.from_stream_fields(fields)
            except Exception as exc:
                # 生产者与消费者的 schema 对不上（比如滚动发布），或者 payload
                # 被谁改坏了。重投一万次结果也一样，所以直接进死信 —— 而且
                # **必须 ack**，否则它是一条永远卡在 PEL 里的毒药消息。
                #
                # 这里捕的是 Exception 而不是 ValidationError：Pydantic 的校验
                # 错误类型不止一种（``ValidationError`` 与 ``json.JSONDecodeError``
                # 会按 payload 的坏法轮流出场），而漏掉一种的后果是消费循环崩掉、
                # 重启、再读到同一条、再崩 —— 一个自造的崩溃循环，日志里只有一条
                # 看不出因果的信息。这里宁可捕宽一点，也不能把循环带崩。
                attempt = await self._attempt_of(stream_key, msg_id)
                await self.dead_letter(
                    stream_key,
                    group,
                    msg_id,
                    fields,
                    attempt,
                    f"payload 无法解析：{exc}",
                    ErrorClass.SCHEMA_UNRECOVERABLE,
                )
                await self.xack(stream_key, group, msg_id)
                log.error(
                    "redis_queue.poison_message",
                    msg_id=msg_id,
                    group=group,
                    task_id=fields.get("task_id", "?"),
                    error=str(exc)[:2000],
                    error_class=type(exc).__name__,
                )
                continue

            attempt = await self._bump_attempt(stream_key, msg_id)
            yield _RedisHandle(self, stream_key, group, msg_id, fields, attempt=attempt), msg

    async def _read_new(
        self, stream_key: str, group: str, consumer: str
    ) -> tuple[str, dict[str, str]] | None:
        """``XREADGROUP`` 读一条**新**消息（``>``：只要没投给这个组的）。

        ``count=1`` 而不是读一批：投递出去的那一刻起，这条消息的空闲时间就开始
        计算了。一次读 10 条、再一条条慢慢跑（每条都要调一次 LLM），后面 9 条会在
        自己还在排队时就变成「空闲超时」被别的副本回收走 —— 白白多跑几遍。
        """
        # 标成 Any：redis-py 对这条命令的返回类型是一堆 union（``list[...] | None``
        # 里套着 ``dict | str | int``），在 mypy 眼里没法直接拆包。形状是实测过的
        # （``[[stream, [(id, {field: value})]]]``），下面按它写。
        reply: Any = await self.client.xreadgroup(
            group, consumer, {stream_key: ">"}, count=1, block=_BLOCK_MS
        )
        if not reply:
            return None
        for _stream, entries in reply:
            for msg_id, fields in entries:
                return str(msg_id), _clean_fields(fields)
        return None

    # -- 内部：状态迁移 ---------------------------------------------------- #

    async def xack(self, stream_key: str, group: str, msg_id: str) -> None:
        """确认一条消息。**幂等** —— 不在 PEL 里就返回 0，不报错。"""
        await self.client.xack(stream_key, group, msg_id)

    async def dead_letter(
        self,
        stream_key: str,
        group: str,
        msg_id: str,
        fields: dict[str, str],
        attempt: int,
        error: str,
        error_class: ErrorClass,
    ) -> None:
        """把原消息复制一份进死信流。

        .. warning::
           死信**不是**完成机制。调用方必须**另外**发一条 ``status=failed`` 的结果，
           否则 ``wait`` 节点的屏障永远闭合不了（CLAUDE.md 约定 #2）。
        """
        await self.client.xadd(
            STREAMS["dead_letter"],
            _redis_fields(
                dead_letter_fields(
                    fields,
                    source_stream=stream_key,
                    source_id=msg_id,
                    group=group,
                    attempt=attempt,
                    error=error,
                    error_class=error_class,
                    failed_at=datetime.now(UTC),
                )
            ),
            # 死信永不裁剪 —— 它的全部价值就是「事后能回看」。
        )

    async def _still_pending(self, stream_key: str, group: str, msg_id: str) -> bool:
        """这条消息还挂在 PEL 上吗？

        只有「认领回来、还在缓冲里等着投递」的那条路径需要问它。内存实现能
        **免费**回答（一个本地集合），这里要多一次往返 —— 换来的是两边在
        「回收之后原消费者才 ack」这条路径上行为完全一致，而不是「差不多一致”。
        """
        found = await self.client.xpending_range(stream_key, group, min=msg_id, max=msg_id, count=1)
        return bool(found)

    async def _bump_attempt(self, stream_key: str, msg_id: str) -> int:
        """投递计数 +1，返回新的次数（首次投递得到 1）。

        计数器带 TTL，且**不在 ack 时删除**：同一条消息会被 ``review_tasks`` 上
        的三个组各读一遍，删掉它就等于让其中一个组的 ack 把另一个组的重试次数清零。
        """
        key = _attempt_key(stream_key, msg_id)
        pipe = self.client.pipeline(transaction=False)
        pipe.incr(key)
        pipe.expire(key, _ATTEMPT_TTL_S)
        result: Any = await pipe.execute()
        return int(result[0])

    async def _attempt_of(self, stream_key: str, msg_id: str) -> int:
        """只读计数器（不 +1）。给「还没投出去就发现问题」的路径用。"""
        value = await self.client.get(_attempt_key(stream_key, msg_id))
        return int(value) if value else 1

    async def _count_after(self, stream_key: str, after_id: str) -> int:
        """``lag`` 为 NULL 时的兜底：数游标之后还剩几条。"""
        entries = await self.client.xrange(stream_key, min=f"({after_id}", max="+")
        return len(entries or [])

    # -- 内部：回收 -------------------------------------------------------- #

    async def _claim(self, stream_key: str, group: str, consumer: str) -> tuple[int, int]:
        """扫一个组的 PEL。返回 ``(认领条数, 被清掉的已删条数)``。"""
        claimed = 0
        purged = 0
        cursor = "0-0"
        for _ in range(_CLAIM_SCAN_LIMIT):
            cursor, entries, gone = await self._xautoclaim(stream_key, group, consumer, cursor)
            if gone:
                purged += len(gone)
                # 这些 id 对应的消息已经不在流里了（被裁剪或删除）。Redis 7 已经
                # 把它们从 PEL 里摘掉，这里再 XACK 一次是幂等的保险 ——
                # 老版本（6.2）会以「nil payload 的条目」形式返回，那条路径见下。
                await self.client.xack(stream_key, group, *gone)
            for msg_id, fields in entries:
                self._buffer(stream_key, group).append(
                    _Claimed(
                        str(msg_id), {str(k): ("" if v is None else str(v)) for k, v in dict(fields).items()}
                    )
                )
                claimed += 1
            if cursor in ("0-0", ""):
                break
        return claimed, purged

    async def _xautoclaim(
        self, stream_key: str, group: str, consumer: str, cursor: str
    ) -> tuple[str, list[Any], list[str]]:
        reply: Any = await self.client.xautoclaim(
            stream_key,
            group,
            consumer,
            min_idle_time=self.claim_idle_ms,
            start_id=cursor,
            count=_CLAIM_BATCH,
        )
        parts = list(reply)  # reply 是 Any，见 _read_new 里的说明
        # Redis 7.0 起返回三段：新游标 / 认领到的条目 / 被清掉的已删 id。
        # 6.2 只有前两段。我们的镜像是 redis:7-alpine，但解包失败的后果是
        # 「回收突然全挂」这种很难一眼看出原因的故障，所以两段也接住。
        if len(parts) >= 3:
            return str(parts[0]), list(parts[1]), [str(x) for x in parts[2]]
        return str(parts[0]), list(parts[1]), []

    def _buffer(self, stream_key: str, group: str) -> deque[_Claimed]:
        return self._claimed.setdefault((stream_key, group), deque())

    def _pop_claimed(self, stream_key: str, group: str) -> _Claimed | None:
        """取一条认领回来的消息。**这个方法里不允许出现 ``await``**：
        「检查—修改」之间没有挂起点，单事件循环下它就是原子的。"""
        buffer = self._claimed.get((stream_key, group))
        if not buffer:
            return None
        return buffer.popleft()

    def _target_groups(self, worker_type: WorkerType | str | None) -> list[tuple[str, str]]:
        """``reclaim()`` 要扫的 ``(流, 组)``。

        ``None`` 时是**全部**组，包括 ``review_bootstrap`` —— 编排器在
        ``ingest`` 中途崩掉时，那条 bootstrap 就是靠这里被捞回来的，
        漏掉它的症状是「这个 run 再也不动了」，没有任何日志。
        """
        if worker_type is not None:
            return [(STREAMS["tasks"], group_for(worker_type))]
        return [(STREAMS[key], name) for key, name in CONSUMER_GROUPS.items()] + [
            (STREAMS["tasks"], group_for(wt)) for wt in WORKER_TYPES
        ]

    # -- 内部：建组与命名 -------------------------------------------------- #

    async def _ensure_groups(self) -> None:
        """建消费者组（幂等）。**游标设在流尾**（``$``）：历史消息不回放。

        不这么做的话，一个刚起来的 Worker 会去重跑上一个 run 的任务 ——
        而它看起来完全正常，只是在做一些早就做完的事。
        """
        if self._groups_ready or self._closed or self._client is None:
            return
        for key, name in CONSUMER_GROUPS.items():
            await self._create_group(STREAMS[key], name)
        for worker_type in WORKER_TYPES:
            await self._create_group(STREAMS["tasks"], group_for(worker_type))
        self._groups_ready = True
        overview: dict[str, list[str]] = {STREAMS[key]: [name] for key, name in CONSUMER_GROUPS.items()}
        overview[STREAMS["tasks"]] = sorted(group_for(wt) for wt in WORKER_TYPES)
        # 死信没有消费者组是**有意**的：它的正确访问方式是只读的 XRANGE，
        # 建了组就一定会有人用 XREADGROUP 去读，读完那条就从组的视角消失了。
        log.info("redis_queue.groups_ready", groups=overview, consumer_prefix=self.client_name)

    async def _create_group(self, stream_key: str, group: str) -> None:
        try:
            # MKSTREAM：流不存在就顺手建一个空的。否则第一次起来时这里会报
            # 「XGROUP 要求键存在」——而这恰好是最常见的一条路径。
            await self.client.xgroup_create(stream_key, group, id="$", mkstream=True)
            log.debug("redis_queue.group_created", stream=stream_key, group=group)
        except ResponseError as exc:
            # BUSYGROUP = 组已经在了。这是**正常路径**（每个副本启动时都会调一次），
            # 不是错误 —— 建组必须幂等，否则多副本部署会互相把对方拦在启动之外。
            if "BUSYGROUP" not in str(exc):
                raise

    def _next_consumer(self, group: str) -> str:
        """消费者名。``XINFO CONSUMERS review_tasks security-group`` 里看到的就是它。

        **必须每个副本都不同**，否则三个副本会被 Redis 当成同一个消费者、PEL 合成
        一份 —— 功能上还能跑（消息仍然只投一次），但 ``--scale`` 那个演示就废了：
        输出的是一行而不是三行，而这正是要证明的东西。

        组成：
          * ``client_name`` —— 队列/锁后端 + 模式，来自 factory
          * ``group`` —— 哪条流上的哪个角色
          * 主机名 —— **副本之间的区分全靠它**。容器里 pid 恒等于 1，
            而 Docker 的 hostname 是容器 id 前 12 位，``docker compose ps``
            能直接对上
          * pid + 序号 —— 同一个进程内开出的多个消费者不重名
        """
        self._consumer_seq += 1
        return f"{self.client_name}:{group}:{_HOST}:{os.getpid()}:{self._consumer_seq}"

    def _require_usable(self) -> None:
        if self._closed:
            # 明确报错，而不是安静地收下一条永远不会被消费的消息 ——
            # 后者表现为「投递成功了，但 run 永远卡在 wait」。
            raise RuntimeError("队列已关闭，拒绝接受新消息")
        if self._client is None:
            raise RuntimeError("RedisStreamsQueue 尚未 start() —— 建组必须在第一次读写之前")

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
            # 用 **redis 的** ConnectionError/TimeoutError —— 内置的同名异常与它们
            # 没有继承关系，写错成内置版本不但不生效，还会让人以为已经配了重试。
            retry_on_error = [RedisConnectionError, RedisTimeoutError]

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


# --------------------------------------------------------------------------- #
# 锁
# --------------------------------------------------------------------------- #

#: 「不在 asyncio Task 里」这个情况的持有者标识。每个协程一旦被调度就是一个 Task，
#: 所以这个哨兵几乎不会被用到 —— 它存在只是为了让 ``acquire`` / ``release``
#: 在那种情况下仍然配对得上，而不是把 ``None`` 当成一个「谁都是它」的持有者。
_OUTSIDE_TASK = object()


class RedisLock:
    """``Lock`` 的 Redis 实现：``SET NX PX`` + Lua 比对释放。

    ### 为什么必须有 Lua

    释放不能是「先 GET 再 DEL」：中间那个窗口足够让锁超时、被别人拿到，
    而前一个持有者随后把**别人的**锁删掉。症状是两个执行体同时认为自己独占 ——
    既不报错，也不稳定复现。所以比对和删除必须在一条脚本里原子完成。

    ### 持有者是谁：和内存实现用**同一条规则**

    ``SET NX PX`` 天然只认「键在不在」，不认持有者。要能拒绝非持有者的释放，
    就得记住「我拿过哪些键」。这里用的是和 :class:`~sfly_bus.memory.InMemoryLock`
    完全一样的规则：**持有者 = 当前的 asyncio Task**。

    这不是照抄，是因为 Protocol 里 ``release(key)`` 没有 token 参数，
    实现只能自己认出持有者；而两种实现如果各用一套规则，「同一套代码、两种拓扑」
    在这条路径上就不成立了（同一个协程在内存模式下能释放、在 Redis 模式下不能）。

    代价也是一样的：**子协程无法释放父协程拿的锁**，而且不可重入。
    要绕开就在同一个 Task 里 acquire / release。

    ### 本进程关闭不释放远端锁

    ``close()`` 只关连接，不 best-effort 释放自己拿着的键。理由：关机路径上多发
    一批命令，换来的只是「少等几秒 TTL」，而代价是关机可能卡在网络上 ——
    而卡住的关机在容器编排里会被反复重试。锁会自己过期，这不是正确性问题
    （锁本来就不保证正确性，见 Protocol 的说明）。
    """

    def __init__(self, url: str, *, client_name: str = "sfly-lock") -> None:
        self.url = url
        self.client_name = client_name
        self._client: aioredis.Redis | None = None
        self._release_script: Any = None
        #: ``(持有者, 键) -> (token, 过期时刻)``。持有者身份是 Task，见类文档。
        self._held: dict[tuple[object, str], tuple[str, float]] = {}
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("这份 RedisLock 已经 close() 过，生命周期是一次性的")
        if self._client is not None:
            return
        self._client = aioredis.Redis.from_url(
            self.url,
            decode_responses=True,
            socket_connect_timeout=_STARTUP_CONNECT_TIMEOUT_S,
            # 锁的延迟直接落在请求路径上（webhook 去重、唤醒选举），所以这里
            # 限一个命令超时：一个卡住的 SET 会让整个请求跟着卡住。
            # 队列那边刻意**不**设，因为它有主动阻塞的 XREADGROUP。
            socket_timeout=_PING_TIMEOUT_S,
            retry=Retry(ExponentialWithJitterBackoff(base=0.1, cap=1.0), 3),
            retry_on_error=[ConnectionError, TimeoutError],
            health_check_interval=30,
            client_name=self.client_name,
        )
        # 注册脚本：EVALSHA + 首次 NOSCRIPT 自动回退 EVAL，由 redis-py 处理。
        self._release_script = self._client.register_script(_RELEASE_LUA)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._held.clear()
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.aclose()
            self._client = None

    async def acquire(self, key: str, ttl_ms: int) -> bool:
        """尝试获取。拿到返回 True，已被占用返回 False。**不阻塞、不重试。**"""
        self._prune()
        token = uuid4().hex
        # ``px`` 最小是 1：``SET ... PX 0`` 会被 Redis 直接拒绝
        # （``ERR invalid expire time``），而 ttl_ms=0 在语义上是「立刻就过期」，
        # 不是「不要过期」—— 拒绝它会把一个配置笔误变成一个抛异常的请求路径。
        ok_ = await self._client_required().set(key, token, nx=True, px=max(ttl_ms, 1))
        if ok_:
            self._held[(self._owner(), key)] = (token, time.monotonic() + ttl_ms / 1000.0)
        return bool(ok_)

    async def release(self, key: str) -> None:
        """释放，但**只释放自己的**：比对 token 之后才删。"""
        entry = self._held.pop((self._owner(), key), None)
        if entry is None:
            # 没拿过（或已经过期并被回收）。**绝不能在这里直接 DEL** ——
            # 那正是「释放掉别人的锁」那个 bug 的写法。
            log.warning("lock.release_not_owner", key=key)
            return
        token, _ = entry
        removed = await self._release_script(keys=[key], args=[token])
        if not removed:
            # 键已经不在了：我们超时之后别人拿到、又释放了，或者 TTL 到了。
            # 两种情况都不需要做什么，但值得留一条 —— 它意味着这段临界区
            # 跑得比 TTL 还久，而 TTL 是**故意**设短的那个参数。
            log.warning("lock.release_expired", key=key)

    def held(self) -> list[str]:
        """本进程当前持有的、还没过期的键。给测试和排查用。

        注意它只反映**本进程**：别的副本拿着的键这里看不到 —— 与内存实现的
        同名方法口径一致（内存实现里也不存在「别的进程」）。
        """
        now = time.monotonic()
        return sorted(k for (_, k), (_, expires_at) in self._held.items() if expires_at > now)

    async def ping(self) -> CheckResult:
        """探测 Redis 可达性。锁也可以回答这个问题（``queue_backend != redis``
        而 ``lock_backend == redis`` 时，健康页就靠它）。"""
        client = aioredis.Redis.from_url(
            self.url,
            decode_responses=True,
            socket_connect_timeout=_PING_CONNECT_TIMEOUT_S,
            socket_timeout=_PING_TIMEOUT_S,
            retry=Retry(NoBackoff(), 0),
        )
        t0 = time.perf_counter()
        try:
            info = await asyncio.wait_for(_ping_and_info(client), timeout=_PING_TIMEOUT_S)
        except Exception as exc:
            return down("redis", _describe(exc), (time.perf_counter() - t0) * 1000)
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
        latency = (time.perf_counter() - t0) * 1000
        return ok("redis", f"Redis {(info or {}).get('redis_version', '?')}", latency)

    def _prune(self) -> None:
        """清掉已经过期的本地记录。过期是**惰性**判定的：没有后台定时器，
        代价是一个过期又没人再申请的键会一直躺在字典里 —— 键的数量由业务决定
        （每个 run 一个），可以接受。"""
        now = time.monotonic()
        for owner_key, (_, expires_at) in list(self._held.items()):
            if expires_at <= now:
                del self._held[owner_key]

    def _client_required(self) -> aioredis.Redis:
        if self._client is None:
            raise RuntimeError("RedisLock 尚未 start() —— 在 lifespan 里漏了？")
        return self._client

    @staticmethod
    def _owner() -> object:
        # 不在 Task 里（极少数情况）时用一个哨兵，而不是把 None 当成
        # 一个「谁都是它」的持有者。
        return asyncio.current_task() or _OUTSIDE_TASK


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


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
