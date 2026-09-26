"""Worker 的消费循环 —— ``__main__.py`` 的常驻部分被搬到这里，加上 ``WorkerPool``。

搬家的理由不是「文件太长」：消费循环要被**两个宿主**共用。
完整模式下它是一个容器（``python -m sfly_workers --spec security``），
精简模式下它是单进程里的三条协程（``WorkerPool``）。放在 ``__main__.py`` 里
的话，第二个宿主只能 ``from sfly_workers.__main__ import ...`` ——
导入一个模块叫 ``__main__`` 的东西，语义上是在导入「那个命令行程序」。

### ``WorkerPool`` 不是「另一种 Worker 实现」

它内部起的还是同一批 ``WorkerRunner``，走的是同一条 :func:`process_one`，
对着同一份 ``TaskQueue`` 协议。差别只在**进程边界**：三个 spec 是三个进程，
还是三条协程。这正是「一套代码、两种拓扑」在 Worker 这一侧的证据 ——
如果精简模式需要另一个消费循环，那句话就只是宣传。

### 为什么进程边界不是免费的

同进程的三条协程共享一个事件循环，所以一个 lane 里的阻塞调用会拖住另外两个。
完整模式里它们各自独立（一个 Worker 卡住不影响别人）。这是精简模式**真实的、
写在 README 里的**代价，不是实现瑕疵 —— 而它也正是「为什么完整模式值得存在」
这个问题的答案。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from sfly_agent.llm.base import LLMProvider
from sfly_agent.llm.pricing import estimate_cost_usd
from sfly_agent.llm.registry import build_llm
from sfly_bus.base import MessageHandle, RunStore, TaskQueue
from sfly_shared.config import Settings, get_settings
from sfly_shared.contracts import (
    NON_RETRYABLE_ERRORS,
    ErrorClass,
    ResultStatus,
    TaskMessage,
    WorkerResult,
)
from sfly_shared.logging import bind_task, get_logger
from sfly_workers.runner import WorkerRunner, failed_result
from sfly_workers.specs import SPECS, WorkerSpec

log = get_logger(__name__)

#: 存储层故障之后取下一条之前的退避。依赖挂了的时候每条消息都会立刻失败，
#: 不退避就是一串说同一件事的异常日志。
FAILURE_BACKOFF_S = 1.0


async def process_one(
    handle: MessageHandle,
    task: TaskMessage,
    *,
    runner: WorkerRunner,
    queue: TaskQueue,
    store: RunStore,
    settings: Settings,
) -> None:
    """处理一条任务。**这个函数的语句顺序就是 CLAUDE.md 约定 #1（投递顺序铁律）。**

    三行写入的顺序不是风格问题：

    * 先 ``XADD`` 后写库 → 编排器被唤醒去读一个还不存在的结果，屏障检查失败
    * 先 ``XACK`` 后写库 → 结果同时从 PEL 和数据库消失，**永久丢失**

    而 ``save_result`` 抛异常时**不 ack** 也是这条约定的一部分：消息留在 PEL 里，
    等 Postgres 恢复后被回收重跑，两处都不丢。
    """
    worker_type = task.worker_type
    bind_task(task.task_id, worker_type.value)

    # 幂等快路径：回收之后重投的消息很可能早就写过了。省下的是一次真实的 LLM 调用，
    # 但**正确性不靠它** —— 真正的保证是 worker_results 的主键
    # （``save_result`` 里的 ON CONFLICT DO NOTHING），而这条路径可能因为
    # 「上次写库成功、这次查询失败」而给出错误答案，所以它只是快路径。
    if await store.exists_result(task.task_id, worker_type):
        log.info(
            "worker.skip_already_done",
            task_id=task.task_id,
            worker_type=worker_type.value,
            attempt=handle.attempt,
        )
        await handle.ack()
        return

    try:
        result = await runner.review(
            task_id=task.task_id,
            patches=task.file_patches,
            rules=task.rules,
            attempt=handle.attempt,
        )
    except Exception as exc:
        # 失败也是结果（约定 #2）：不补一条 failed 结果的话，wait 节点的屏障
        # 永远闭合不了，整个 run 挂到超时 —— 而日志里只会有一条「Worker 报错」。
        #
        # 注意这里只接 ``Exception``：CancelledError 必须继续往上走（停机信号），
        # 把它翻译成一条 failed 结果会让停机变成一次「失败的审查」。
        log.exception("worker.review_crashed", task_id=task.task_id, worker_type=worker_type.value)
        result = failed_result(task.task_id, worker_type, exc, attempt=handle.attempt)

    await store.save_result(result)  # 1. 先落库
    await _record_result_event(store, result)
    await _record_cost(store, task, result)
    await queue.publish_result(result)  # 2. 再唤醒编排器
    if should_dead_letter(result, handle, settings):
        # 死信**不是**完成机制（约定 #2）：failed 结果上面已经发过了，
        # 这一条只是为了运维可见性。to_dead_letter 自己会 ack。
        await handle.to_dead_letter(result.error or "", result.error_class or ErrorClass.TRANSIENT)
    else:
        await handle.ack()  # 3. 最后离开 PEL


async def _record_result_event(store: RunStore, result: WorkerResult) -> None:
    """把「这个 Worker 上报了」写进 run_events（SSE 时间线的权威来源）。

    **为什么由 Worker 写而不是协调协程写**：协调协程和图是**并发的两条路径**，
    而图判断屏障读的是数据库（不是消息流）。所以完全可能出现
    「数据库里三条结果齐了、图已经 aggregate 完、协调协程才开始消费第一条消息」——
    实测过，时间线上会看到 ``worker.result`` 排在 ``run.finished`` **后面**。

    那不是排序问题，是**写事件的人站错了位置**：这件事的因果起点是
    「Worker 写完了结果」，那就该由 Worker 在写完结果的那一刻记下来。
    协调协程因此变得更单纯 —— 它只做屏障检查和唤醒，一条事件都不写。

    代价是 Worker 多一次写库。它和 ``save_result`` 在同一条关键路径上，
    所以失败处置也一致：**抛出去，不 ack**（消息留在 PEL 里等重投）。
    一条事件缺失就是时间线上一个永久的空洞，而空洞是静默的。

    好消息是这条路顺带修掉了重复事件：结果早就写过的那些消息会在
    ``exists_result`` 的快路径上直接 ack，根本走不到这里。
    """
    await store.append_event(
        result.task_id,
        "worker.failed" if result.status is ResultStatus.FAILED else "worker.result",
        {
            "worker_type": result.worker_type.value,
            "status": result.status.value,
            "findings": len(result.findings),
            "latency_ms": result.latency_ms,
            "error_class": result.error_class.value if result.error_class else None,
        },
    )


async def _record_cost(store: RunStore, task: TaskMessage, result: WorkerResult) -> None:
    """把这一条结果的 token 换算成钱，记进 ``llm_calls``。

    位置在 ``save_result`` **之后、``publish_result`` 之前**：
    前面那一步已经把结果落库了，所以记账失败不会丢任何东西 ——
    而它**必须**失败得无害，见下。

    ### 为什么这里可以吞异常

    这是全项目唯一一处「记账失败就跳过」的地方，所以要写清楚理由：
    ``llm_calls`` 的每一列都可以从 ``worker_results`` 重算出来
    （token 数在结果里，单价在 ``pricing.py`` 里）。也就是说丢一行账的代价是
    **重算一次**，不是丢数据。而记账失败时抛出去会走消费循环的存储故障分支：
    不 ack → 消息留在 PEL → 被回收 → 整个审查重跑一遍，再烧一次 LLM 的钱 ——
    为了一条可以被重算的账，付一次真实的模型调用。代价不对称，所以这里吞掉。

    ``agent`` 字段填 ``worker_type``：评测要做「安全 Worker 花了多少钱」这类
    分组，而按 lane 分组是唯一能回答它的方式。
    """
    try:
        await store.record_llm_call(
            task_id=task.task_id,
            agent=task.worker_type.value,
            model=result.model or "unknown",
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cached_tokens=result.cached_tokens,
            cost_usd=estimate_cost_usd(
                result.model,
                tokens_in=result.tokens_in,
                tokens_out=result.tokens_out,
                cached_tokens=result.cached_tokens,
            ),
            latency_ms=result.latency_ms,
            ok=result.status is not ResultStatus.FAILED,
            error_class=result.error_class.value if result.error_class else None,
        )
    except Exception:
        log.exception(
            "worker.cost_record_failed",
            task_id=task.task_id,
            worker_type=task.worker_type.value,
            hint="只丢了记账；token 数在 worker_results 里，可以重算",
        )


def should_dead_letter(result: WorkerResult, handle: MessageHandle, settings: Settings) -> bool:
    """这条消息该不该进死信。

    ``attempt >= max_attempts`` 而不是 ``>``：attempt 从 1 开始计数，
    所以 ``MAX_ATTEMPTS=3`` 的意思是「第 3 次投递失败时进死信」——
    总共三次机会，不是四次。

    不可重试的错误（schema_unrecoverable / diff_too_large / repo_not_found /
    auth_revoked）一次就进死信：再试一百次的结果完全一样，而每一次都要烧一份
    prompt 的 token。
    """
    if result.status is not ResultStatus.FAILED:
        return False
    if result.error_class in NON_RETRYABLE_ERRORS:
        return True
    return handle.attempt >= settings.max_attempts


async def consumer_loop(
    spec: WorkerSpec,
    runner: WorkerRunner,
    *,
    queue: TaskQueue,
    store: RunStore,
    settings: Settings,
) -> None:
    """一条消费路径。一个进程起 ``WORKER_CONCURRENCY`` 条，``--scale`` 再叠一层。

    每条协程在 Redis 那边有**自己的 consumer 名**（``_next_consumer`` 的序号），
    所以 ``XINFO CONSUMERS`` 数出来的条数和这里起的一样多。
    """
    async for handle, task in queue.consume_tasks(spec.worker_type):
        try:
            await process_one(handle, task, runner=runner, queue=queue, store=store, settings=settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            # 能漏到这里的只有**存储层故障**（写库失败 / 唤醒失败）——
            # 每一条业务错误在 ``process_one`` 里都已经有了归宿。
            #
            # **绝不能 ack**：不 ack 的消息留在 PEL 里，等 Postgres 恢复后
            # 被回收重跑，两处都不丢。这就是 README 里「Postgres 不可达不丢结果」
            # 那一行的实现。
            log.exception(
                "worker.message_failed",
                task_id=task.task_id,
                worker_type=task.worker_type.value,
                attempt=handle.attempt,
                hint="没有 ack —— 消息留在 PEL，等依赖恢复后由 reclaim 重投",
            )
            # 退避一下再取下一条：依赖挂了的时候每条消息都会立刻失败，
            # 不退避就是一串刷屏的异常日志，而它们说的是同一件事。
            await asyncio.sleep(FAILURE_BACKOFF_S)


async def reclaim_forever(queue: TaskQueue, spec: WorkerSpec, interval_s: float) -> None:
    """定期把同伴手里超时未确认的消息抢回来。

    这是「副本猝死」能被兜住的那一半（另一半是幂等写入）。**每个副本都跑它** ——
    多跑几次是无害的（``XAUTOCLAIM`` 只会拿走空闲超过阈值的那些），
    而少跑一个副本的后果是「那个副本手里的活没人接」。

    回收只是把消息变回「可投递」，它要经由同进程的消费协程才真的被处理 ——
    这是 ``reclaim()`` 返回计数而不是返回消息的直接后果（见 base.py 的说明）。
    """
    while True:
        await asyncio.sleep(interval_s)
        try:
            count = await queue.reclaim(spec.worker_type)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker.reclaim_failed", worker_type=spec.worker_type.value)
            continue
        if count:
            log.info(
                "worker.reclaimed",
                worker_type=spec.worker_type.value,
                count=count,
                hint="这些是空闲超过 CLAIM_IDLE_MS 未被确认的消息，多半来自一个猝死的副本",
            )


async def serve_spec(
    spec: WorkerSpec,
    stop: asyncio.Event,
    *,
    deps_queue: TaskQueue,
    deps_store: RunStore,
    settings: Settings | None = None,
    llm_factory: Callable[..., LLMProvider] = build_llm,
    llm: LLMProvider | None = None,
) -> None:
    """一个 lane 的常驻消费：``WORKER_CONCURRENCY`` 条消费协程 + 一条回收协程。

    ``llm`` 可以外部注入（测试和 ``--diff`` 那样的离线路径要用），
    不给就按设置自己造一个 —— 造出来的由本函数负责关。
    """
    s = settings or get_settings()
    owns_llm = llm is None
    # ``ledger=deps_store`` 是**线上成本闸的接线**。它无条件传进去，而不是
    # 让调用方决定：忘了传的后果是「公网上的服务没有任何花费上限」，而那个
    # 后果不会以任何方式表现出来 —— 直到账单。store 本来就握在手里，没有
    # 任何理由不接。见 sfly_agent/llm/budget.py。
    provider: LLMProvider = llm or llm_factory(s, worker_types=(spec.worker_type,), ledger=deps_store)
    runner = WorkerRunner(spec, provider, s)

    # **心跳不在这里起。** 它是「进程还活着吗」的信号，而进程的边界由宿主决定：
    # 完整模式下一个容器一条 lane（``run_service`` 起心跳），精简模式下三条
    # 协程共享一个进程（``WorkerPool`` 的宿主起一个）。在这里起的话，
    # 精简模式会有三条协程往同一个文件上写，而它们说的是同一件事。
    log.info(
        "worker.consuming",
        consumer_group=spec.consumer_group,
        stream=spec.stream,
        worker_type=spec.worker_type.value,
        categories=len(spec.categories),
        concurrency=s.worker_concurrency,
        claim_idle_ms=s.claim_idle_ms,
        reclaim_interval_s=s.reclaim_interval_s,
    )

    tasks = [
        asyncio.create_task(
            consumer_loop(spec, runner, queue=deps_queue, store=deps_store, settings=s),
            name=f"{spec.name}-consumer-{i}",
        )
        for i in range(max(1, s.worker_concurrency))
    ]
    tasks.append(
        asyncio.create_task(
            reclaim_forever(deps_queue, spec, s.reclaim_interval_s), name=f"{spec.name}-reclaim"
        )
    )

    try:
        # 停机信号在这里等 —— 消费协程自己不会返回（它们阻塞在流上）。
        await stop.wait()
    finally:
        # **取消在飞的那条消息是可以的**，不是妥协：它留在 PEL 里，由 reclaim
        # 交给同伴重跑。这和「副本猝死」走的是同一条路径，而那条路径有测试。
        #
        # 不这么做的代价是停机要等一个 60 秒的 LLM 调用跑完，而 Docker 只给 10 秒
        # 就 SIGKILL —— 结果一样，只是过程更难解释。
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if owns_llm:
            closer = getattr(provider, "aclose", None)
            if closer is not None:
                await closer()


class WorkerPool:
    """**单进程里跑三个 lane** —— 精简模式（Render）和端到端测试的宿主。

    完整模式请用三个容器：``python -m sfly_workers --spec <lane>``。
    两者跑的是同一个 :func:`serve_spec`，所以「一个进程三个 lane」和
    「三个进程各一个 lane」之间没有第二份实现可以漂移。
    """

    def __init__(
        self,
        *,
        queue: TaskQueue,
        store: RunStore,
        settings: Settings | None = None,
        specs: Sequence[WorkerSpec] | None = None,
        llm_factory: Callable[..., LLMProvider] = build_llm,
    ) -> None:
        self.settings = settings or get_settings()
        self.lanes: tuple[WorkerSpec, ...] = tuple(specs) if specs is not None else tuple(SPECS.values())
        self._queue = queue
        self._store = store
        self._llm_factory = llm_factory

    async def run(self, stop: asyncio.Event) -> None:
        """跑到 ``stop`` 被 set。**三条 lane 的异常各自独立**，不互相拖垮。

        ``gather(return_exceptions=False)`` 会让一条 lane 的崩溃静默地取消另外
        两条 —— 而在这里那是最坏的选择：一个 lane 的配置错误（比如它那个
        provider 缺密钥）会让另外两个本来能干活的一起停摆。所以收集异常、
        逐条记录，然后让 ``stop`` 决定什么时候真的退出。
        """
        tasks = [
            asyncio.create_task(
                serve_spec(
                    spec,
                    stop,
                    deps_queue=self._queue,
                    deps_store=self._store,
                    settings=self.settings,
                    llm_factory=self._llm_factory,
                ),
                name=f"pool-{spec.name}",
            )
            for spec in self.lanes
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for spec, outcome in zip(self.lanes, results, strict=True):
                if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                    log.error(
                        "worker.lane_crashed",
                        worker_type=spec.worker_type.value,
                        error=str(outcome),
                        error_class=type(outcome).__name__,
                    )
