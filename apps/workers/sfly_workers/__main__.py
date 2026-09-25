"""``python -m sfly_workers --spec {security|performance|style}``

一套代码、三种部署。三个容器跑的是**同一个镜像同一个入口**，
只靠 ``--spec`` 区分消费者组和提示词 —— 见 ``specs.py``。

独立的 CLI 模式（``--diff fixtures/x.diff``）是 M1 的交付物：
不接队列、不起容器，直接对着一份 diff 跑单个 Worker 并打印结果。
调试提示词时这个入口比走完整链路快一个数量级。
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sfly_shared.config import get_settings
from sfly_shared.contracts import WorkerType
from sfly_shared.heartbeat import run_service
from sfly_shared.logging import get_logger
from sfly_workers.specs import SPECS, spec_for

log = get_logger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="sfly_workers",
        description="sfly 专业审查 Worker",
    )
    p.add_argument(
        "--spec",
        required=True,
        choices=[w.value for w in SPECS],
        help="本进程负责的审查类型；决定消费者组与提示词",
    )
    p.add_argument(
        "--diff",
        metavar="PATH",
        help="独立 CLI 模式（M1）：直接审查一份 diff 文件并打印结果，不接队列",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="独立 CLI 模式的别名，强调只跑一次",
    )
    return p.parse_args(argv)


async def _consume_forever(worker_type: WorkerType, stop: asyncio.Event) -> None:
    """常驻消费循环。M1/M3 填充。

    M3 的形态大致是：
        async for handle, task in bus.consume_tasks(worker_type):
            if stop.is_set():
                break
            if await already_done(task):          # 幂等快路径
                await handle.ack(); continue
            try:
                result = await runner.run(task)
            except Exception as exc:
                # 失败也是结果：先补一条 failed 结果闭合屏障，再决定是否进死信
                result = WorkerResult.failed(task.task_id, worker_type, str(exc),
                                             classify(exc), attempt=handle.attempt)
            await store.save_result(result)        # 1. 先落库
            await bus.publish_result(result)       # 2. 再唤醒编排器
            await handle.ack()                     # 3. 最后离开 PEL
    """
    spec = SPECS[worker_type]
    settings = get_settings()
    log.info(
        "worker.skeleton",
        consumer_group=spec.consumer_group,
        stream=spec.stream,
        categories=len(spec.categories),
        concurrency=settings.worker_concurrency,
        claim_idle_ms=settings.claim_idle_ms,
        status="skeleton — M1/M3 接入消费循环",
    )
    await stop.wait()


async def _main_async(args: argparse.Namespace) -> None:
    worker_type = WorkerType(args.spec)

    # 独立 CLI 模式：不起心跳、不接队列，跑完就退。M1 实现。
    if args.diff:
        log.error("worker.cli_mode_not_implemented", diff=args.diff, status="M1 交付")
        raise SystemExit(2)

    await run_service(f"worker-{worker_type.value}", lambda stop: _consume_forever(worker_type, stop))


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    # spec_for 会校验并给出可读的报错
    spec_for(args.spec)
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
