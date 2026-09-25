"""进程内队列与锁 —— 精简模式（单容器）的 ``TaskQueue`` / ``Lock`` 实现。

### 它不是一个「简化版」，是一个**语义等价版**

这个文件的意义在于让 ``QUEUE_BACKEND=memory`` 成为一个诚实的选择。如果它退化
成「一个 ``asyncio.Queue`` 加几个方法」，会有两个后果：

1. M3 要在两个后端上跑的那份契约测试（``tests/unit/bus/queue_contract.py``）就只能
   断言「``asyncio.Queue`` 做得到的事」，于是「两种拓扑共用一套代码」的证据
   悄悄缩水成「两种拓扑都收得发得了消息」—— 而证据缩水是不会有报错的。
2. 精简模式独有的 bug 在完整模式上复现不了，反过来也一样。那正是这个项目
   号称要避免的事。

所以这里实现的是真正的 Streams 语义：**每个消费者组一个独立游标 + 各自的
待确认列表（PEL）+ 投递计数**。三条容易被忽略的推论：

* **发布是扇出。** 一条消息会进入该流上**每一个**组的可见范围 —— 因为 Redis 的
  消费者组各持一个游标，互不影响。``security-group`` 消费掉它，不会让
  ``performance-group`` 看不到同一条消息；后者看到之后按 ``worker_type``
  过滤掉并 ack（这一步不花任何 LLM token）。

* **回收不是投递。** ``reclaim()`` 只是把消息重新变成「可投递」，它仍然要经由
  ``consume_*`` 才交到消费者手里，而那时 ``handle.attempt`` 已经是 2。
  这是 Protocol 的决定（``reclaim`` 返回计数），见 ``base.py``。

* **裁剪留下墓碑。** 条目被 ``maxlen`` 裁掉之后，仍然持有它的 PEL 读到的是一条
  ``trimmed=True`` 的条目 —— 对应 Redis 里 ``XAUTOCLAIM`` 返回 null payload 的
  id。消费循环必须把它当「已处理」ack 掉（否则它会永远卡在 PEL 里，
  ``reclaim`` 一遍遍把它捞回来），绝不能让 ``None`` 流进解析器。

### 单事件循环让「检查—修改」天然原子

下面 ``_claim_next`` / ``_ack`` / ``_dead_letter`` 都在**没有 ``await``** 的区间里完成
检查与修改，所以在同一个事件循环内是原子的。这不是省掉了锁，而是利用了
「一个进程只有一个事件循环」这个前提 —— 也正因为如此，``InMemoryQueue``
**不能**跨进程用，精简模式必须 ``workers=1``。

### 生命周期是一次性的

``start()`` 必须在任何读之前调用（Redis 那边，``XGROUP CREATE`` 建在流尾，
先发布再建组会丢消息；这里用同样的规则免得两种模式行为分叉）。
``close()`` 之后不能再 ``start()`` —— Redis 实现没这个限制，但也没有代码
依赖重新 start()，所以宁可在这里明确报错。
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TypeVar

from pydantic import ValidationError

from sfly_bus.base import CONSUMER_GROUPS, STREAMS, MessageHandle, group_for
from sfly_shared.contracts import (
    BootstrapMessage,
    ErrorClass,
    TaskMessage,
    WorkerResult,
    WorkerType,
)
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 全部 ``worker_type``。从枚举推导而不是写字面量：将来加第四个 Worker 时，
#: 漏掉这里的后果是「它的任务永远没人消费」—— 而那是「往枚举里加了个值」
#: 这类看起来最无害的改动最容易漏掉的地方。
WORKER_TYPES: tuple[WorkerType, ...] = tuple(WorkerType)

MsgT = TypeVar("MsgT", BootstrapMessage, TaskMessage, WorkerResult)

#: 错误信息与死信字段里保留的文本长度
_MAX_ERROR_CHARS = 2000


@dataclass(slots=True)
class _Entry:
    """一条流条目。

    **流日志和 PEL 持有的是同一个对象。** 所以裁剪只需要把它标成墓碑，
    还引用着它的 PEL 立刻就能看见 —— 这正是 Redis 里「``XTRIM`` 之后再
    ``XAUTOCLAIM``，拿到的是一个 payload 为 nil 的 id」的等价物。
    （``fields`` 刻意不清空：内存里没有任何理由丢掉它，死信还能带上原文。）
    """

    id: str
    seq: int
    fields: dict[str, str]
    trimmed: bool = False
    #: 投递次数。跨「回收 → 重投」保留，等价于 Redis 侧的
    #: ``HINCRBY sfly:attempts:{msg_id}``，是死信判定的依据。
    deliveries: int = 0
    #: 已经被哪些组 ack 过。用于识别「回收之后原消费者才 ack」的重投 ——
    #: 那种情况下这条消息不该再被投递给别人。
    acked_by: set[str] = field(default_factory=set)


@dataclass(slots=True)
class _Pending:
    """PEL 里的一条：已投递、未确认。"""

    entry: _Entry
    delivered_at: float


@dataclass
class _Group:
    """一个消费者组：独立游标 + 自己的 PEL。"""

    stream: str
    name: str
    #: 最高一条已投递给本组的 ``seq``。``lag`` 和「本组读过哪些」都由它决定。
    #: 建组时设在流尾（Redis 的 ``XGROUP CREATE ... $``）：历史消息不回放。
    cursor: int = 0
    pending: dict[str, _Pending] = field(default_factory=dict)
    #: ``reclaim()`` 捞回来的条目，等着被下一次 ``consume_*`` 交出。
    redeliver: deque[_Entry] = field(default_factory=deque)
    #: 有新条目时的唤醒信号。见 ``_next_entry`` 里的顺序说明。
    notify: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Stream:
    name: str
    maxlen: int | None
    entries: deque[_Entry] = field(default_factory=deque)
    groups: dict[str, _Group] = field(default_factory=dict)


class _MemoryHandle:
    """``MessageHandle`` 的进程内实现。

    ``ack()`` **必须幂等**：被回收重投之后，原消费者和新消费者都可能 ack 同一条
    消息，而 Redis 的 ``XACK`` 对不在 PEL 里的 id 就是返回 0 而已。
    """

    __slots__ = ("_entry", "_group", "_queue", "attempt", "id")

    def __init__(self, queue: InMemoryQueue, group: _Group, entry: _Entry, *, attempt: int) -> None:
        self._queue = queue
        self._group = group
        self._entry = entry
        self.id = entry.id
        self.attempt = attempt

    async def ack(self) -> None:
        self._queue.ack(self._group, self._entry)

    async def to_dead_letter(self, error: str, error_class: ErrorClass) -> None:
        self._queue.dead_letter(self._group, self._entry, error, error_class)
        self._queue.ack(self._group, self._entry)


class InMemoryQueue:
    """``TaskQueue`` 的进程内实现。"""

    def __init__(
        self,
        *,
        claim_idle_ms: int = 180_000,
        stream_maxlen_tasks: int = 10_000,
        stream_maxlen_results: int = 10_000,
    ) -> None:
        self.claim_idle_ms = claim_idle_ms
        self._streams: dict[str, _Stream] = {
            # bootstrap 不裁剪：裁掉一条就等于丢一个 run，而它的流量是「每个 PR 一条」
            STREAMS["bootstrap"]: _Stream(STREAMS["bootstrap"], None),
            STREAMS["tasks"]: _Stream(STREAMS["tasks"], stream_maxlen_tasks),
            STREAMS["results"]: _Stream(STREAMS["results"], stream_maxlen_results),
            # 死信永不裁剪 —— 它的全部价值就是「事后能回看」
            STREAMS["dead_letter"]: _Stream(STREAMS["dead_letter"], None),
        }
        self._seq = 0
        self._started = False
        self._closed = False

    # -- 生命周期 ---------------------------------------------------------- #

    async def start(self) -> None:
        """建组。幂等；``close()`` 之后调用会报错（生命周期是一次性的）。"""
        if self._started:
            return
        if self._closed:
            raise RuntimeError("这份 InMemoryQueue 已经 close() 过，生命周期是一次性的")
        self._started = True
        for stream_key, group in CONSUMER_GROUPS.items():
            self._group(STREAMS[stream_key], group)
        for wt in WORKER_TYPES:
            self._group(STREAMS["tasks"], group_for(wt))
        log.info(
            "memory_queue.started",
            groups={
                key: sorted(self._streams[stream].groups)
                for key, stream in STREAMS.items()
                if key != "dead_letter"
            },
            claim_idle_ms=self.claim_idle_ms,
            # 死信没有消费者组是有意的：它的正确访问方式是只读遍历，
            # 见 base.py 里 CONSUMER_GROUPS 之上那段说明。
        )

    async def close(self) -> None:
        """停止消费。**必须唤醒**所有阻塞中的消费者 —— 否则关机会挂在
        ``await wait()`` 上，表现为「进程不退出」，而那时日志里什么都没有。
        """
        if self._closed:
            return
        self._closed = True
        for stream in self._streams.values():
            for group in stream.groups.values():
                group.notify.set()
        log.info("memory_queue.closed")

    # -- 发布 -------------------------------------------------------------- #

    async def publish_bootstrap(self, msg: BootstrapMessage) -> str:
        return self._append(STREAMS["bootstrap"], msg.to_stream_fields())

    async def publish_task(self, msg: TaskMessage) -> str:
        return self._append(STREAMS["tasks"], msg.to_stream_fields())

    async def publish_result(self, result: WorkerResult) -> str:
        return self._append(STREAMS["results"], result.to_stream_fields())

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
        """把空闲超时的待确认条目放回「可投递」。

        阈值判据是 ``delivered_at``，而它**只在投递时刷新** —— 所以一个正在
        慢跑（LLM 还没返回）的消费者，它的消息也会被回收，然后被投给第二个
        消费者重复处理一遍。这不是 bug，是 Redis 的 ``XAUTOCLAIM`` 同样会做的事：
        回收早了只浪费 token（重复结果被 ``worker_results`` 的主键吸收），
        回收晚了才真的慢。所以这个阈值可以实测调，不必证明。
        """
        cutoff = time.monotonic() - self.claim_idle_ms / 1000.0
        groups = self._target_groups(worker_type)
        reclaimed = 0
        for group in groups:
            stale = [p.entry for p in group.pending.values() if p.delivered_at <= cutoff]
            for entry in stale:
                # 从 PEL 里摘掉再放回投递区。deliveries 不在这里加 ——
                # 它记的是「投递了几次」，加在真正投出去的那一刻（_claim_next）。
                del group.pending[entry.id]
                group.redeliver.append(entry)
            if stale:
                group.notify.set()
            reclaimed += len(stale)
        if reclaimed:
            log.info("memory_queue.reclaimed", count=reclaimed, worker_type=str(worker_type or "*"))
        return reclaimed

    async def lag(self, worker_type: WorkerType | str) -> int:
        """还有多少条没投给这个组。口径同 ``XINFO GROUPS``。"""
        group = self._group(STREAMS["tasks"], group_for(worker_type))
        stream = self._streams[group.stream]
        return sum(1 for e in stream.entries if e.seq > group.cursor)

    # -- 实现自省（不在 Protocol 里，给测试与本机排查用） -------------------- #

    def pending_count(self, worker_type: WorkerType | str) -> int:
        """该组的 PEL 深度。**不是** ``lag``：它数的是「投出去还没 ack 的」，
        而 ``lag`` 数的是「还没投出去的」。消费者卡住时前者在涨、后者是 0。"""
        return len(self._group(STREAMS["tasks"], group_for(worker_type)).pending)

    def dead_letters(self) -> list[dict[str, str]]:
        """死信，按投递顺序。等价于 ``XRANGE dead_letter - +``。"""
        return [dict(e.fields) for e in self._streams[STREAMS["dead_letter"]].entries]

    # -- 内部：写入 -------------------------------------------------------- #

    def _append(self, stream_name: str, fields: dict[str, str]) -> str:
        self._require_usable()
        self._seq += 1
        entry = _Entry(
            # 和 Redis 的 id 同形（``毫秒时间戳-序号``），这样日志里两种模式的
            # 消息 id 长得一样，排查手法不用切换。
            id=f"{int(time.time() * 1000)}-{self._seq}",
            seq=self._seq,
            fields=dict(fields),
        )
        stream = self._streams[stream_name]
        stream.entries.append(entry)
        if stream.maxlen is not None:
            while len(stream.entries) > stream.maxlen:
                # 标记墓碑而不是直接丢弃：还持有它的 PEL 会读到「已被裁剪」。
                stream.entries.popleft().trimmed = True
        for group in stream.groups.values():
            group.notify.set()
        return entry.id

    # -- 内部：读取 -------------------------------------------------------- #

    def _claim_next(self, group: _Group) -> _Entry | None:
        """取一条可投递的条目，没有就返回 ``None``。

        **这个方法里不允许出现 ``await``。** 它是「检查—修改」原子性的来源，
        也是 ``_next_entry`` 里唤醒顺序能成立的前提。
        """
        while group.redeliver:
            entry = group.redeliver.popleft()
            if entry.trimmed or group.name in entry.acked_by:
                # 回收之后原消费者才 ack 的，或条目已被裁剪 —— 作废，不重投。
                # 它已经不在 PEL 里了，所以这里不需要 ack，也不写 pending。
                log.debug("memory_queue.redelivery_dropped", msg_id=entry.id, group=group.name)
                continue
            return self._deliver(group, entry)

        stream = self._streams[group.stream]
        # 从流日志的头部往尾扫。成本是 O(maxlen)，而 maxlen 默认一万 ——
        # 单次几十微秒，换掉的是「每组一个投递队列」带来的内存只增不减。
        for entry in stream.entries:
            if entry.seq <= group.cursor:
                continue
            group.cursor = entry.seq
            if entry.trimmed:
                # 已被裁剪的条目：**消费循环不该看到它**。在这里就 ack 掉，
                # 否则它会永远留在 PEL 里被 reclaim 一遍遍捞回来。
                self.ack(group, entry)
                log.warning("memory_queue.trimmed_entry_acked", msg_id=entry.id, group=group.name)
                continue
            return self._deliver(group, entry)
        return None

    def _deliver(self, group: _Group, entry: _Entry) -> _Entry:
        entry.deliveries += 1
        group.pending[entry.id] = _Pending(entry, time.monotonic())
        return entry

    async def _next_entry(self, group: _Group) -> _Entry | None:
        """阻塞到有一条可投递的条目，或队列被关闭（返回 ``None``）。"""
        while True:
            entry = self._claim_next(group)
            if entry is not None:
                return entry
            if self._closed:
                return None
            # 「clear → 复查 → wait」这个顺序是**必需的**，而且要连着看：
            # 发布者在 set() 之前没有 await，而 clear() 和复查之间也没有 await，
            # 所以不存在「复查完 → 发布者 set → 我才 clear」这个窗口。
            # 那个窗口会让消费者睡死在一条已经到达的消息上 —— 而一个睡死的
            # 消费者没有任何症状，只是这个 run 永远不结束。
            group.notify.clear()
            if self._claim_next(group) is not None:
                group.notify.set()  # 可能还有别的消费者也在等，别把信号吞了
                continue
            await group.notify.wait()

    async def _consume(
        self,
        stream_name: str,
        group_name: str,
        model: type[MsgT],
        *,
        only_worker_type: str | None = None,
    ) -> AsyncIterator[tuple[MessageHandle, MsgT]]:
        self._require_usable()
        group = self._group(stream_name, group_name)
        while True:
            entry = await self._next_entry(group)
            if entry is None:
                return
            # 别的 worker_type 的活：判掉并 ack。
            # 不 ack 的话这个组的游标不前进，lag 会永远挂着一条它永远不会处理的
            # 消息 —— 而这恰好是「组是独立游标，不是分工」的代价：同一条消息
            # 会被三个组各读一遍（多两次读 + 两次 ack，不花 token）。
            if only_worker_type is not None and entry.fields.get("worker_type") != only_worker_type:
                self.ack(group, entry)
                continue
            try:
                msg = model.from_stream_fields(entry.fields)
            except ValidationError as exc:
                # 生产者与消费者的 schema 对不上（比如滚动发布），或者 payload 被
                # 谁改坏了。重投一万次结果也一样，所以直接进死信 ——
                # 而且**必须 ack**，否则它就是一条永远卡在 PEL 里的毒药消息。
                self.dead_letter(group, entry, f"payload 无法解析：{exc}", ErrorClass.SCHEMA_UNRECOVERABLE)
                self.ack(group, entry)
                log.error(
                    "memory_queue.poison_message",
                    msg_id=entry.id,
                    group=group.name,
                    task_id=entry.fields.get("task_id", "?"),
                    error=str(exc)[:_MAX_ERROR_CHARS],
                )
                continue
            yield _MemoryHandle(self, group, entry, attempt=entry.deliveries), msg

    # -- 内部：状态迁移 ----------------------------------------------------- #

    def ack(self, group: _Group, entry: _Entry) -> None:
        """确认一条消息。**幂等** —— 不在 PEL 里就什么都不做（``XACK`` 返回 0）。"""
        group.pending.pop(entry.id, None)
        entry.acked_by.add(group.name)

    def dead_letter(self, group: _Group, entry: _Entry, error: str, error_class: ErrorClass) -> None:
        """把原消息复制一份进死信流。

        .. warning::
           死信**不是**完成机制。调用方必须**另外**发一条 ``status=failed`` 的结果，
           否则 ``wait`` 节点的屏障永远闭合不了（CLAUDE.md 约定 #2）。
           这里存的只是「什么失败了、为什么、第几次」。
        """
        self._append(
            STREAMS["dead_letter"],
            {
                "payload": entry.fields.get("payload", ""),
                "source_stream": group.stream,
                "source_id": entry.id,
                "group": group.name,
                "task_id": entry.fields.get("task_id", ""),
                "worker_type": entry.fields.get("worker_type", ""),
                "attempt": str(entry.deliveries),
                "error": error[:_MAX_ERROR_CHARS],
                "error_class": str(error_class),
                "failed_at": datetime.now(UTC).isoformat(),
            },
        )

    # -- 内部：杂项 -------------------------------------------------------- #

    def _group(self, stream_name: str, group_name: str) -> _Group:
        """取组，没有就建一个。

        建组时游标设在**流尾**（Redis 的 ``XGROUP CREATE ... $``）：历史消息不回放。
        否则一个刚起来的 Worker 会去重跑上一个 run 的任务。
        """
        stream = self._streams[stream_name]
        group = stream.groups.get(group_name)
        if group is None:
            group = _Group(stream=stream_name, name=group_name, cursor=self._seq)
            stream.groups[group_name] = group
            log.debug("memory_queue.group_created", stream=stream_name, group=group_name, at_seq=self._seq)
        return group

    def _target_groups(self, worker_type: WorkerType | str | None) -> list[_Group]:
        stream = self._streams[STREAMS["tasks"]]
        if worker_type is None:
            return list(stream.groups.values()) + list(self._streams[STREAMS["results"]].groups.values())
        return [self._group(STREAMS["tasks"], group_for(worker_type))]

    def _require_usable(self) -> None:
        if self._closed:
            # 明确报错，而不是安静地收下一条永远不会被消费的消息 ——
            # 后者表现为「投递成功了，但 run 永远卡在 wait」。
            raise RuntimeError("队列已关闭，拒绝接受新消息")
        if not self._started:
            raise RuntimeError("InMemoryQueue 尚未 start() —— 建组必须在第一次读写之前")


# --------------------------------------------------------------------------- #
# 锁
# --------------------------------------------------------------------------- #

#: 「不在 asyncio Task 里」这个情况的持有者标识。
#: 每个协程一旦被调度就是一个 Task，所以这个哨兵几乎不会被用到 ——
#: 它存在只是为了让 ``acquire`` / ``release`` 在那种情况下仍然配对得上，
#: 而不是把 ``None`` 当成一个「谁都是它」的持有者。
_OUTSIDE_TASK = object()


class InMemoryLock:
    """``Lock`` 的进程内实现。

    ### 为什么必须校验持有者

    Redis 那边用 ``SET NX PX`` 存一个 token，释放时用 Lua 做「比对再删」。
    少了这个比对，一个已经超时（很可能已被别人重新拿到）的锁会被前一个持有者
    删掉 —— 于是两个执行体同时认为自己独占。进程内同样成立：A 拿了锁、超时、
    B 拿到、A 来释放 —— 不校验的话 B 的锁就没了。所以这里记持有者，
    释放时比对。

    ### 进程内的「持有者」就是当前的 Task

    这带来一条和 Redis 不一样的限制：**子协程无法释放父协程拿的锁**，因为它们是
    不同的 Task，而 Redis 的 token 是可以随手传下去的。这是刻意的取舍 ——
    Protocol 里 ``release(key)`` 没有 token 参数，所以实现只能自己想办法认出
    持有者。要绕开它，就在同一个 Task 里 acquire / release。

    另外它是**不可重入**的（同一个 Task 拿两次，第二次会失败）—— 这正是 ``SET NX``
    的行为，别按 ``asyncio.Lock`` 或可重入锁的直觉去用它。

    ### 超时是惰性的

    没有后台定时器，「过期」只在 ``acquire`` 时检查。代价是一个已经过期、
    又没人再申请过的键会一直躺在字典里 —— 键的数量由业务决定（每个 run 一个），
    所以可以接受；换成定时器则意味着每个锁一份回调。
    """

    def __init__(self) -> None:
        self._held: dict[str, tuple[object, float]] = {}

    async def acquire(self, key: str, ttl_ms: int) -> bool:
        """尝试获取。拿到返回 True，已被占用返回 False。**不阻塞、不重试。**"""
        now = time.monotonic()
        current = self._held.get(key)
        if current is not None and current[1] > now:
            return False
        self._held[key] = (self._owner(), now + ttl_ms / 1000.0)
        return True

    async def release(self, key: str) -> None:
        """释放，但**只释放自己的**。

        这里不检查「过期了没有」：如果还记着我，说明没有别人拿到过它
        （别人要拿到，就会把持有者改写成他自己），所以删掉是安全的 ——
        这和 Redis 那段 Lua 的语义一致。反过来，如果已经过期**并且**被别人
        重新拿到了，持有者就不是我，上面那行 warning 会记下这次误释放。
        """
        current = self._held.get(key)
        if current is None:
            return
        if current[0] is not self._owner():
            log.warning("lock.release_not_owner", key=key)
            return
        del self._held[key]

    async def close(self) -> None:
        self._held.clear()

    def held(self) -> list[str]:
        """当前仍然有效的锁键。给测试和健康页用。"""
        now = time.monotonic()
        return sorted(k for k, (_, expires_at) in self._held.items() if expires_at > now)

    @staticmethod
    def _owner() -> object:
        return asyncio.current_task() or _OUTSIDE_TASK
