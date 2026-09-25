"""传输层协议 —— 决定「一套代码、两种拓扑」能否成立。

这个文件里**没有实现**，只有 Protocol。两个实现分别是：
  * ``redis_streams.py`` — 完整模式（消费者组、XAUTOCLAIM、死信）
  * ``memory.py``        — 精简模式（asyncio 队列，单进程）

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
from typing import Protocol, runtime_checkable

from sfly_shared.contracts import (
    BootstrapMessage,
    ErrorClass,
    ReviewReport,
    RunEvent,
    RunRow,
    RunStatus,
    TaskMessage,
    WorkerResult,
    WorkerType,
)

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
        """
        ...

    async def lag(self, worker_type: WorkerType | str) -> int:
        """该消费者组的积压条数，用于健康检查和 UI 展示。"""
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

    async def append_event(self, task_id: str, kind: str, payload: dict) -> int:
        """追加一条事件，返回自增的 ``seq``。

        这张表是权威来源，SSE 只是快路径 —— 所以客户端带 ``Last-Event-ID``
        重连时可以零缺口补齐，轮询也是免费的降级方案。
        """
        ...

    async def events_since(self, task_id: str, after_seq: int) -> list[RunEvent]: ...

    async def list_runs(self, limit: int = 50, offset: int = 0) -> list[RunRow]: ...

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
