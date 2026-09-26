"""传输层协议 —— 决定「一套代码、两种拓扑」能否成立。

这个文件里**没有业务实现**，只有 Protocol 和两种实现必须共用的命名约定。
两个实现分别是：
  * ``redis_streams.py`` — 完整模式（消费者组、XAUTOCLAIM、死信）
  * ``memory.py``        — 精简模式（进程内，单事件循环）

### 为什么 ``consume_*`` 返回 ``(handle, message)`` 而不是 ``(msg_id, message)``

这是本文件最重要的一个设计决定。

Redis 要 ``XACK`` 一条消息，需要 ``(stream, group, msg_id)`` 三元组。
``InMemoryQueue`` 根本没有这个概念。如果 ``consume_*`` 返回裸的消息 id，
那么每一个调用方都得自己持有 stream 名和 group 名，Redis 的细节会在第一天
就泄漏到全部 5 个 app 里 —— 两种拓扑共用代码这条卖点当场作废。

有了 ``MessageHandle``，``InMemoryQueue.ack()`` 就是个 no-op，
``WorkerRunner`` 只写一遍，对着 Protocol 编程，在两种模式下逐字不变。

**这一条必须在写第一个实现之前定死**，事后改会动到每一个消费者。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sfly_shared.contracts import (
    BootstrapMessage,
    DeliveryRow,
    DeliveryStatus,
    ErrorClass,
    LlmSpend,
    ReviewReport,
    RunEvent,
    RunRow,
    RunStatus,
    RunTotals,
    TaskMessage,
    WorkerResult,
    WorkerType,
)

#: 全部 ``worker_type``。从枚举推导而不是写字面量：将来加第四个 Worker 时，
#: 漏掉这里的后果是「它的任务永远没人消费」—— 而那是「往枚举里加了个值」
#: 这类看起来最无害的改动最容易漏掉的地方。
#:
#: 放在这里而不是某个实现里：两种实现都要给每个 ``worker_type`` 建一个消费者组，
#: 而**漏建一个组**在 Redis 上表现为 ``NOGROUP`` 报错、在内存实现里表现为
#: 那条消息永远没人消费 —— 同一个疏漏、两种完全不同的症状。推导一次，两边共用。
WORKER_TYPES: tuple[WorkerType, ...] = tuple(WorkerType)

# --------------------------------------------------------------------------- #
# 命名约定 —— 两种实现必须用同一套名字
# --------------------------------------------------------------------------- #
#
# 这不是为了整齐。精简模式的日志、健康页、以及「用 XINFO 排查线上问题」的
# 手法，都要能和完整模式一一对上；名字一分叉，「同一套代码」在**出故障的时候**
# 就不是同一套了 —— 而那正是唯一要紧的时候。

STREAMS: dict[str, str] = {
    "bootstrap": "review_bootstrap",
    "tasks": "review_tasks",
    "results": "review_results",
    "dead_letter": "dead_letter",
}

#: 只有**独占消费**的流在这里。``review_tasks`` 的组按 worker_type 动态生成，
#: 见 :func:`group_for`。
CONSUMER_GROUPS: dict[str, str] = {
    "bootstrap": "orchestrator-group",
    "results": "orchestrator-group",
}

#: ``dead_letter`` 刻意**不在**上面那张表里 —— 死信不建消费者组。
#: 它的全部价值是「事后能回看」，而消费组的语义是一读一 ack、游标只往前挪；
#: 建了组就一定会有人用 ``XREADGROUP`` 去读它，读完那条死信就从组的视角消失了。
#: 正确的访问方式是 ``XRANGE dead_letter - +`` —— 只读，不推进任何游标。


def group_for(worker_type: WorkerType | str) -> str:
    """每个 ``worker_type`` 一个消费者组。

    同组的多个进程/协程**竞争消费**：一条消息只会投递给组内一个成员。
    这就是 ``docker compose up --scale worker-security=3`` 能工作的机制 ——
    它不依赖任何应用层代码，只是消费者组的定义。
    """
    return f"{worker_type}-group"


#: 死信里保留的错误文本长度。LLM 的失败信息经常是一整篇散文 ——
#: 不截断的话，死信流会慢慢变成第二个日志系统。
MAX_ERROR_CHARS = 2000


def dead_letter_fields(
    source_fields: dict[str, str],
    *,
    source_stream: str,
    source_id: str,
    group: str,
    attempt: int,
    error: str,
    error_class: ErrorClass,
    failed_at: datetime,
) -> dict[str, str]:
    """构造一条死信条目的字段。**两种实现共用这一份。**

    理由和 ``STREAMS`` 一样：``XRANGE dead_letter - +`` 在两种拓扑下必须长得
    一模一样。排障时你只有一次看的机会（死信是「事后回看」的东西，现场早没了），
    字段名对不上就等于这条死信不存在。

    字段要够到「不用翻日志就能判断该不该手动重跑」—— 死信唯一的用途是运维可见性，
    它**不是**完成机制（见 CLAUDE.md 约定 #2）。
    """
    return {
        # 原 payload 留一份：没有它，复盘时连「它当时想干什么」都不知道
        "payload": source_fields.get("payload", ""),
        "source_stream": source_stream,
        "source_id": source_id,
        "group": group,
        "task_id": source_fields.get("task_id", ""),
        "worker_type": source_fields.get("worker_type", ""),
        "attempt": str(attempt),
        "error": error[:MAX_ERROR_CHARS],
        "error_class": str(error_class),
        "failed_at": failed_at.isoformat(),
    }


# --------------------------------------------------------------------------- #
# 消费句柄
# --------------------------------------------------------------------------- #


@runtime_checkable
class MessageHandle(Protocol):
    """一条已投递未确认的消息。

    生命周期严格是：``消费 → 处理 → ack()``，或者 ``消费 → 处理失败 → to_dead_letter()``。
    **不要既 ack 又 to_dead_letter**，也不要处理完不 ack —— 不 ack 的消息会一直留在
    PEL 里，直到被 ``XAUTOCLAIM`` 回收后重跑，浪费 LLM token。
    """

    #: 消息在底层传输中的唯一标识。仅用于日志串联，不要用它做业务判断。
    id: str
    #: 第几次投递，从 1 开始。重派时递增，是死信判定的依据。
    attempt: int

    async def ack(self) -> None:
        """确认处理完成，消息离开 PEL。

        必须在 ``RunStore.save_result()`` **之后**调用 —— 见 CLAUDE.md 的投递顺序铁律。
        """
        ...

    async def to_dead_letter(self, error: str, error_class: ErrorClass) -> None:
        """把消息转入死信流并 ack。仅用于运维可见性。

        .. warning::
           死信**不是**完成机制。调用这个方法之前，必须已经发出一条
           ``status=FAILED`` 的 ``WorkerResult``，否则 ``wait`` 节点的屏障
           永远闭合不了，整个 run 会挂到超时。见 CLAUDE.md 约定 #2。
        """
        ...


# --------------------------------------------------------------------------- #
# 任务队列
# --------------------------------------------------------------------------- #


@runtime_checkable
class TaskQueue(Protocol):
    """四条流的读写接口。

    ``review_bootstrap``  api → orchestrator
    ``review_tasks``      orchestrator → worker（按 worker_type 分消费者组）
    ``review_results``    worker → orchestrator
    ``dead_letter``       任何一方 → 运维
    """

    # -- 生命周期 ---------------------------------------------------------- #

    async def start(self) -> None:
        """建连、建消费者组（幂等）、启动后台回收协程。"""
        ...

    async def close(self) -> None:
        """停止后台协程、释放连接。"""
        ...

    # -- bootstrap --------------------------------------------------------- #

    async def publish_bootstrap(self, msg: BootstrapMessage) -> str:
        """把新任务交给编排层。返回消息 id。"""
        ...

    def consume_bootstrap(self) -> AsyncIterator[tuple[MessageHandle, BootstrapMessage]]:
        """编排器独占消费。永远只有一个消费者组 ``orchestrator-group``。"""
        ...

    # -- tasks ------------------------------------------------------------- #

    async def publish_task(self, msg: TaskMessage) -> str:
        """派发一个 Worker 任务。"""
        ...

    def consume_tasks(
        self, worker_type: WorkerType | str
    ) -> AsyncIterator[tuple[MessageHandle, TaskMessage]]:
        """按消费者组消费任务。

        同一个 ``worker_type`` 的多个进程/协程共享消费者组，Redis 保证
        每条消息只投递给组内一个成员 —— 这是 ``--scale worker-security=3``
        能工作的机制。
        """
        ...

    # -- results ----------------------------------------------------------- #

    async def publish_result(self, result: WorkerResult) -> str:
        """Worker 上报结果。

        .. warning::
           调用顺序：先 ``RunStore.save_result()``，再本方法，最后 ``handle.ack()``。
           先 XADD 后写库会让编排器去读一个还不存在的结果。
        """
        ...

    def consume_results(self) -> AsyncIterator[tuple[MessageHandle, WorkerResult]]:
        """编排器的 coordinator 协程消费，用于屏障检查与唤醒。"""
        ...

    # -- 运维 -------------------------------------------------------------- #

    async def reclaim(self, worker_type: WorkerType | str | None = None) -> int:
        """回收空闲超时的待确认消息，返回回收条数。

        由每个 Worker 每 30s 调用一次（``CLAIM_IDLE_MS=180000``）。回收早了
        只是浪费 token —— 重复结果会被 ``worker_results`` 的主键吸收 ——
        所以这个阈值可以实测调参，不必证明。

        .. important::
           **回收不等于投递。** 本方法只把消息重新变回「可投递」，它必须经由
           ``consume_tasks`` / ``consume_results`` 才交到消费者手里。

           这不是随手定的：``reclaim`` 返回**计数**，所以它是维护动作，
           而消费者的消息来源因此只有一个。代价由实现自己扛 —— Redis 的
           ``XAUTOCLAIM`` 是**直接返回消息**的（与 ``XREADGROUP`` 是两条不同的
           读路径），所以那个实现要把 CLAIM 到的消息先缓冲起来，
           等下一次 ``consume_*`` 时再交出。
        """
        ...

    async def lag(self, worker_type: WorkerType | str) -> int:
        """该消费者组的积压条数，用于健康检查和 UI 展示。

        口径同 Redis 的 ``XINFO GROUPS``：**还有多少条没投给这个组**，
        不是「还有多少条没被 ack」。两者只在消费者卡住时才分叉 ——
        那时前者是 0、后者在涨，而这个区别恰好是排查时最需要分清的一件事。
        """
        ...

    async def trim(self, stream_key: str, maxlen: int = 0) -> int:
        """**精确**裁剪一条流（Redis 的 ``XTRIM MAXLEN n``，不带 ``~``），返回删掉的条数。

        生产代码不该调它 —— 生产靠发布时的**近似**裁剪（``XADD ... MAXLEN ~ n``），
        因为精确裁剪要数条目，而近似裁剪不用。它存在的两个理由：

        * 运维：积压太多时手动清一次。
        * 测试：让「消息在消费者手里被裁掉」这条路径能被**确定性地**复现。
          ``MAXLEN ~`` 裁不裁、裁几条都不保证，拿它写断言等于写一个偶尔红的测试。

        .. important::
           被裁掉的条目**对所有人都消失了**，包括还持有它的消费者：它既不会被
           投递，也不会被当成「待重投」的消息送回来。两个后端在这一点上必须一致，
           而**清理时机**是不同的 —— 见下面这段实测记录。

           Redis（实测 7.4）的清理是**惰性**的：``XTRIM`` 只从流里删条目，
           PEL 里那个 id 会悬在那里；真正把它摘掉的是下一次 ``XAUTOCLAIM``，
           它把悬空的 id 从 PEL 里删除并在返回值的第三段报出来
           （``min-idle-time`` 多大都不影响这件事）。内存实现在裁剪那一刻就
           一并清掉 PEL 与回收区（``memory.py`` 的 ``_purge``）—— 两者的
           中间状态不同，但对调用方可观察的结果相同。

           > 这段是被一次实测纠正过的。第一次读到的现象是「``XTRIM`` 之后
           > ``XPENDING`` 少了一条」，于是照着「Redis 会立刻清 PEL」去写了内存
           > 实现 —— 而那个读数是同一段脚本里紧跟着的 ``XAUTOCLAIM`` 造成的。
           > 把清理时机写进契约是错的，把结果写进契约是对的。
        """
        ...

    async def dead_letters(self) -> list[dict[str, str]]:
        """死信列表，按写入顺序。等价于 ``XRANGE dead_letter - +``。

        **只读**，不消费、不推进任何游标 —— 死信刻意没有消费者组（见 ``base.py``
        里 ``CONSUMER_GROUPS`` 之上那段）。字段集合由 :func:`dead_letter_fields`
        固定，两种实现逐字一致：线上排查时只有一次看的机会，字段名对不上就等于
        这条死信不存在。契约测试逐字段断言这一点。
        """
        ...


# --------------------------------------------------------------------------- #
# 状态存储
# --------------------------------------------------------------------------- #


@runtime_checkable
class RunStore(Protocol):
    """Postgres 持久化。**两种拓扑都必须有它**，所以没有内存实现。

    这里不是「可选依赖」：``wait`` 节点的屏障查询、幂等性的唯一约束、
    SSE 的事件日志、恢复逻辑的超时扫描，全部落在这一层。
    """

    # -- 生命周期 ---------------------------------------------------------- #

    async def migrate(self) -> None:
        """幂等地建表。启动时调用，本地和 Neon 走同一条路径（纯 SQL，不上 Alembic）。"""
        ...

    async def close(self) -> None: ...

    # -- run --------------------------------------------------------------- #

    async def create_run(self, msg: BootstrapMessage) -> RunRow:
        """创建 run 记录。

        幂等键冲突时返回**已存在**的那一行而不是抛错 —— 重放的 webhook
        应该拿到同一个 run_id。
        """
        ...

    async def get_run(self, task_id: str) -> RunRow | None: ...

    async def get_run_by_key(self, idempotency_key: str) -> RunRow | None: ...

    async def set_status(
        self,
        task_id: str,
        status: RunStatus,
        *,
        degraded: bool | None = None,
        missing_workers: list[WorkerType] | None = None,
    ) -> None: ...

    async def set_plan(
        self,
        task_id: str,
        planned_workers: list[WorkerType],
        *,
        files_total: int,
        files_reviewed: int,
        diff_truncated: bool,
        deadline_at: datetime,
    ) -> None:
        """``plan`` 节点写回。``deadline_at`` 一旦落库，超时扫描器就开始管这个 run。"""
        ...

    async def set_decision(self, task_id: str, *, block_merge: bool, totals: RunTotals) -> None:
        """把最终决定与成本汇总写回 run 行。

        和 ``save_report`` 分开：报告是**产物**（jsonb，评测和重新发布要用），
        这两列是**索引**（运行列表直接读，不解 jsonb）。合成一次写入的话，
        列表页就会被绑在报告的结构上。
        """
        ...

    async def due_runs(self, now: datetime) -> list[RunRow]:
        """扫出已过 ``deadline_at`` 但仍在 ``DISPATCHED``/``WAITING`` 的 run。

        这是断点恢复的核心查询：图被 ``interrupt()`` 暂停后 orchestrator 崩溃，
        恢复不依赖任何内存状态，只依赖这一条 SELECT。
        """
        ...

    # -- 结果 -------------------------------------------------------------- #

    async def save_result(self, result: WorkerResult) -> None:
        """UPSERT 一条 Worker 结果，同时写入其 findings。

        实现必须是 ``INSERT ... ON CONFLICT (task_id, worker_type) DO NOTHING``。
        **这个唯一约束才是幂等性的真正保证** —— Redis 的 SETNX 只是省 token 的快路径。
        """
        ...

    async def get_results(self, task_id: str) -> list[WorkerResult]: ...

    async def completed_workers(self, task_id: str) -> list[WorkerType]:
        """屏障查询：已经上报过结果（成功**或失败**）的 Worker 列表。

        ``wait`` 节点靠它判断能否进入 aggregate。注意失败也算完成 ——
        这就是「失败也是结果」这条约定在存储层的体现。
        """
        ...

    async def exists_result(self, task_id: str, worker_type: WorkerType | str) -> bool:
        """Worker 端幂等检查的兜底路径。"""
        ...

    # -- 报告 -------------------------------------------------------------- #

    async def save_report(self, report: ReviewReport) -> None:
        """持久化最终报告。

        即使在 ``publish`` 失败时也必须调用 —— GitHub 限流不该让报告丢失，
        评论正文存在库里，之后可以手动重新发布。
        """
        ...

    async def get_report(self, task_id: str) -> ReviewReport | None: ...

    async def mark_published(self, task_id: str, comment_id: int) -> None:
        """记录已发布的评论 id。

        ``publish`` 节点发帖**之前**先查这个字段，这是一道防重复评论的闸；
        另一道是评论正文里的隐藏标记 ``<!-- sfly:run:{task_id} -->``。
        """
        ...

    # -- 事件（SSE + UI 时间线） ------------------------------------------- #

    async def append_event(self, task_id: str, kind: str, payload: dict[str, Any]) -> int:
        """追加一条事件，返回自增的 ``seq``。

        这张表是权威来源，SSE 只是快路径 —— 所以客户端带 ``Last-Event-ID``
        重连时可以零缺口补齐，轮询也是免费的降级方案。
        """
        ...

    async def events_since(self, task_id: str, after_seq: int) -> list[RunEvent]: ...

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[RunRow]: ...

    # -- webhook 投递 ------------------------------------------------------ #

    async def record_delivery(
        self,
        delivery_id: str,
        *,
        event: str,
        repo_id: str = "",
        pr_number: int | None = None,
    ) -> bool:
        """认领一次 webhook 投递。**首次返回 True，已经见过返回 False。**

        判据必须是主键冲突（``INSERT ... ON CONFLICT DO NOTHING``），不是先查后写：
        并发投递同一个 delivery id 时，先查后写两边都会读到「不存在」，
        然后两条都往下走。

        .. note::
           「已经见过」不等于「已经处理完」。要求调用方在冲突时再读一次
           :meth:`get_delivery` —— 停在 ``RECEIVED`` 的那条是**可以接管的**
           （进程在记账之后、干活之前崩了）。只认主键不看状态的话，
           一次崩溃就能让那批投递被永久当成重复。
        """
        ...

    async def get_delivery(self, delivery_id: str) -> DeliveryRow | None: ...

    async def list_deliveries(self, limit: int = 50) -> list[DeliveryRow]: ...

    async def release_delivery(self, delivery_id: str) -> None:
        """撤销一次还没结算的认领，让这次投递可以被重新处理。

        「干了活但没干成」的路径要用它：账本记的是**处置结果**，
        而那次处置没有发生。留一行停在 ``RECEIVED`` 的记录，
        下一次排查时会被读成「处理过但没结果」—— 一个查不下去的状态。
        """
        ...

    async def finish_delivery(
        self,
        delivery_id: str,
        status: DeliveryStatus,
        *,
        task_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        """回填这次投递的处置结果。

        **没有回填的投递可以被下一次重投接管** —— 这是 ``received`` 存在的全部理由。
        所以这个方法只在「事情真的做完/决定了」之后调，不要在中间调。
        """
        ...

    # -- 成本 -------------------------------------------------------------- #

    async def record_llm_call(
        self,
        *,
        task_id: str | None,
        agent: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        cached_tokens: int,
        cost_usd: float,
        latency_ms: int,
        ok: bool,
        error_class: str | None = None,
    ) -> None:
        """记录一次 LLM 调用。评测的每个 token 数字都是这张表的 SUM。

        必须用 provider 返回的 ``usage`` 字段，**不要**用本地 tokenizer 估算 ——
        估算会错，而错的地方恰好是面试官会追问的地方。
        """
        ...

    async def sum_costs(self, task_id: str) -> dict[str, float]: ...

    async def llm_spend_since(self, since: datetime) -> LlmSpend:
        """从 ``since`` 起花掉的调用次数与钱 —— 线上成本闸读它。

        **为什么是从表里 SUM，而不是进程内一个计数器**：精简模式跑在 Render
        免费档上，15 分钟没人访问就休眠、下次访问重新拉起进程。内存计数器
        每天会被重置几十次，于是「每日上限」看起来在保护、实际不保护 ——
        而它失败的方向是多花钱，没有任何东西会报警。

        代价是**检查在花完之后**（``_record_cost`` 写在结果落库之后）：
        最坏情况下会多花「同时在跑的 Worker 个数」次调用的钱。这个方向是
        刻意选的 —— 反过来要在调用前预扣，就得处理「调用失败要退」，
        而那是一条会写错的路径，代价还比多花几厘钱大。
        """
        ...

    # -- 清理 -------------------------------------------------------------- #

    async def purge_older_than(self, days: int = 14) -> dict[str, int]:
        """清理旧的 checkpoint 与 run_events。

        Neon 免费版只有 0.5GB，而 LangGraph 的 checkpoint 按 thread 无界增长 ——
        没有这个任务，线上演示跑一段时间就会写满。
        """
        ...


# --------------------------------------------------------------------------- #
# 分布式锁
# --------------------------------------------------------------------------- #


@runtime_checkable
class Lock(Protocol):
    """短时互斥。用于防止同一 PR 被重复处理、防止图被并发唤醒。

    .. note::
       锁**不是**正确性机制。它可能过期，也可能随 Redis 重启丢失。
       跨进程的幂等性必须由数据库唯一约束保证（见 ``RunStore.save_result``）。
       锁只用来避免重复劳动和重复唤醒。
    """

    async def acquire(self, key: str, ttl_ms: int) -> bool:
        """尝试获取。拿到返回 True，已被占用返回 False。**不阻塞、不重试。**"""
        ...

    async def release(self, key: str) -> None:
        """释放。实现必须校验持有者身份（Redis 用 Lua compare-and-delete），
        否则会释放掉别人的锁。"""
        ...

    async def close(self) -> None: ...
