#!/usr/bin/env python3
"""队列层的容错演示：**一个消费者猝死，同伴把它手里的活接过去跑完。**

    python scripts/demo_reclaim.py

这是 M3 的可运行证据。完整的「docker kill 一个 Worker，run 照样跑完并显示降级
徽章」要等 M4/M5（Worker 常驻循环需要 Postgres 仓储、屏障闭合需要编排器），
但那个演示依赖的**机制**就是这里跑的这几步，一步不少：

    1. 两个消费者竞争同一个消费者组           —— ``--scale worker-security=3`` 的机制
    2. 一个副本在处理中途「死掉」              —— 不 ack，消息留在它的 PEL 上
    3. 现场：PEL 里挂着一条没人认领的消息      —— 「猝死」在 Redis 里的样子
    4. 同伴 ``reclaim()``（XAUTOCLAIM）把它抢过来
    5. 重投时 ``attempt`` 是 2                 —— 死信判定的依据，也是「重跑过」的凭据

### 它跑在 db 14，跑完会清掉

不想让演示的假消息留在真栈的 ``review_tasks`` 里（M5 接上之后，它们会变成
几个永远等不到编排器的孤儿 run）。``--redis-url`` 可以改，但**别指向 db 0**。

自己用 redis-cli 复核（脚本最后会把这几条原样打出来）：

    docker compose exec redis redis-cli -n 14 XPENDING review_tasks security-group
    docker compose exec redis redis-cli -n 14 XINFO CONSUMERS review_tasks security-group
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import redis.asyncio as aioredis

from sfly_bus.base import STREAMS
from sfly_bus.redis_streams import RedisStreamsQueue
from sfly_shared.aio import run
from sfly_shared.config import get_settings
from sfly_shared.contracts import FilePatch, TaskMessage, WorkerType
from sfly_shared.logging import setup_logging

# --- Windows 控制台编码 --------------------------------------------------- #
#
# 和 tasks.py 里那一段是同一个理由：Windows 控制台默认代码页是 GBK，而中文能
# 显示、✓ / ✗ 这类符号装不下 —— 输出被重定向或接到管道时（git-bash、CI、
# `| grep`）会退化成按 GBK 编码字节流，编码失败抛在 print() 里，
# 于是**命令实际跑完了才崩**，看起来像执行失败。
#
# 所以下面所有提示标记一律 ASCII（OK / !! / ->），这里再加一道兜底：
# 任何意外字符退化成 "?" 而不是中断整个演示。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with contextlib.suppress(ValueError, OSError):
            _stream.reconfigure(errors="replace")

#: 演示专用库。**不要用 0** —— 那里是开发栈正在用的地方。
DEMO_DB = 14

#: 假装的 LLM 耗时。真实价值是几十秒，这里缩到 0.3s 只为让演示跑得完 ——
#: 但「处理中途被打断」这个性质保留了：消息在 PEL 上而消费者还没 ack。
FAKE_LLM_S = 0.3

GROUP = "security-group"


def _rule(title: str) -> None:
    print(f"\n\033[36m{'=' * 4} {title} {'=' * 4}\033[0m", flush=True)


def _demo_task(task_id: str) -> TaskMessage:
    return TaskMessage(
        task_id=task_id,
        worker_type=WorkerType.SECURITY,
        idempotency_key=f"123456:7:{'a' * 40}",
        repo_id="123456",
        repo_node_id="R_kgDOAbcdef",
        pr_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        file_patches=[
            FilePatch(path="app/db.py", language="python", patch="@@ -1 +1 @@\n-x\n+y\n", changed_lines=[1])
        ],
        language="python",
    )


class _Worker:
    """一个副本的消费循环 —— 形状与 M4 的真实 WorkerRunner 一致，只是不落库。

    ``die_next`` 是「下一个消息就猝死」的开关。用开关而不是「死在第 N 条上」，
    是因为两个消费者竞争时「谁抢到哪条」本身就不确定 —— 而演示必须**每次跑都
    讲同一个故事**。用开关之后，唯一不确定的是「死在哪条消息上」，
    而那件事脚本会如实打印出来。
    """

    def __init__(self, queue: RedisStreamsQueue, name: str) -> None:
        self.queue = queue
        self.name = name
        self.die_next = False
        self.died_on: str | None = None
        self.handled: list[tuple[str, int]] = []

    async def run(self, stop: asyncio.Event) -> None:
        async for handle, task in self.queue.consume_tasks(WorkerType.SECURITY):
            if stop.is_set():
                return
            if self.die_next:
                # 「猝死」：不 ack、不 close、不留遗言。消息就留在 PEL 上 ——
                # 而这正是最难查的地方：现场**什么都没发生**。
                self.died_on = task.task_id
                print(
                    f"  \033[31m!!\033[0m {self.name} 在处理 {task.task_id} 的途中被干掉，没 ack", flush=True
                )
                return
            await asyncio.sleep(FAKE_LLM_S)
            await handle.ack()
            self.handled.append((task.task_id, handle.attempt))
            mark = "（第 2 次投递）" if handle.attempt > 1 else ""
            print(f"  \033[32mOK\033[0m {self.name} 处理完 {task.task_id}{mark}", flush=True)


async def _wait_for(predicate: Callable[[], Awaitable[bool]], *, timeout_s: float = 10.0) -> bool:
    """等一个条件成立。用轮询而不是固定 sleep —— 固定 sleep 的演示在慢一点的
    机器上会时对时错，而「演示偶尔失败」比演示失败更糟。

    条件本身是协程：要等的往往是 Redis 里的状态（PEL 深度），而不是内存里的计数。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _all_handled(first: _Worker, second: _Worker, expected: int) -> bool:
    return len(first.handled) + len(second.handled) >= expected


async def _has_died(worker: _Worker) -> bool:
    return worker.died_on is not None


async def _handled(worker: _Worker, task_id: str | None) -> bool:
    return any(t == task_id for t, _ in worker.handled)


async def _only_one_pending(queue: RedisStreamsQueue) -> bool:
    return await queue.pending_count(WorkerType.SECURITY) == 1


async def _show_pending(queue: RedisStreamsQueue, label: str) -> None:
    summary: Any = await queue.client.xpending(STREAMS["tasks"], GROUP)
    consumers: Any = await queue.client.xinfo_consumers(STREAMS["tasks"], GROUP)
    pending = summary.get("pending") if isinstance(summary, dict) else (summary[0] if summary else 0)
    print(f"  {label}：PEL 里 {pending} 条未确认")
    for c in consumers:
        who = str(c["name"])
        flag = "  <- 已经死掉的那个副本还挂在这里" if int(c["pending"]) else ""
        print(f"    · {who}：{c['pending']} 条，空闲 {c['idle']}ms{flag}")


async def main(url: str) -> int:
    settings = get_settings()

    # 先清库再 start()。**顺序不能反**：start() 会把消费者组建在流尾，
    # 之后再 flushdb 会把组一起删掉，而 `_groups_ready` 还是 True —— 消费循环
    # 于是撞上 NOGROUP，重建的组又落在流尾，于是「已经发布但还没被消费」的消息
    # 全部变成不会回放的历史。第一次写这个脚本就踩了，而且症状是
    # 「没有任何报错，只是什么都没被处理」。见 redis_streams.py 的 `_ensure_groups`。
    cleaner = aioredis.Redis.from_url(url, decode_responses=True)
    await cleaner.flushdb()
    await cleaner.aclose()

    queue = RedisStreamsQueue(
        url,
        client_name="sfly-demo",
        # 演示要的是「立刻能回收」。生产用 180000：回收早了只浪费 token
        # （重复结果被数据库主键吸收），回收晚了才真的慢 —— 所以这个值可以实测调。
        claim_idle_ms=0,
    )
    await queue.start()

    _rule("0. 环境")
    print(f"  {url}")
    print(f"  claim_idle_ms：生产 {settings.claim_idle_ms}，本演示 0（立刻可回收）")
    print(f"  消费者组：{STREAMS['tasks']} / {GROUP}")

    _rule("1. 两个副本竞争同一个消费者组")
    print("  这就是 --scale worker-security=3 的机制：同组竞争，一条消息只投给一个成员。")
    print("  注意这条机制**不需要任何应用层代码** —— 它就是消费者组的定义。")
    stop = asyncio.Event()
    a = _Worker(queue, "副本 A")
    b = _Worker(queue, "副本 B")
    loops = [asyncio.create_task(a.run(stop)), asyncio.create_task(b.run(stop))]

    ids = [f"run-{i}" for i in range(1, 5)]
    for task_id in ids:
        await queue.publish_task(_demo_task(task_id))
    print(f"\n  已发布 {len(ids)} 条任务，等它们被瓜分完……")
    await _wait_for(lambda: _all_handled(a, b, len(ids)))
    print(f"  分配结果：A 拿到 {len(a.handled)} 条，B 拿到 {len(b.handled)} 条")

    _rule("2. 副本 B 在处理中途猝死")
    b.die_next = True
    for task_id in ("run-5", "run-6"):
        await queue.publish_task(_demo_task(task_id))
    if not await _wait_for(lambda: _has_died(b)):
        print("  \033[33m副本 B 这次没抢到消息（竞争本来就是随机的），重跑一次即可\033[0m")
        return 1
    print(f"  剩下两条（run-5 / run-6）里，B 抢到了 {b.died_on}，然后它没了。")

    # 等 A 把手上那条干完。**这一步不是为了让输出好看** —— 演示把 claim_idle_ms
    # 设成了 0，于是连「正在处理中」的消息也满足回收条件；不等到 A 空闲，
    # PEL 里就会同时躺着 B 那条和 A 正在跑的那条，`reclaim()` 会返回 2，
    # 故事就讲不清了。生产里这个阈值是 180 秒，正常耗时的任务不会被抢走。
    if not await _wait_for(lambda: _only_one_pending(queue)):
        print("  \033[33mPEL 没能稳定到 1 条，重跑一次\033[0m")
        return 1

    _rule("3. 现场：那条消息还挂在 PEL 上")
    print("  「Worker 猝死」在 Redis 里的样子就是这样 —— 没有任何报错、没有任何日志，")
    print("  只有一条没人认领的记录。不看 PEL 就永远发现不了。")
    await _show_pending(queue, "猝死后")

    _rule("4. 同伴执行 XAUTOCLAIM（reclaim()）")
    reclaimed = await queue.reclaim(WorkerType.SECURITY)
    print(f"  reclaim() 返回 {reclaimed} —— 抢回来几条。")
    print("  注意它**不等于**「又跑了一遍」：回收只把消息变回可投递，")
    print("  真正交到消费者手里要等下一次消费循环（最多 500ms，即一个阻塞周期）。")
    await _show_pending(queue, "回收后")

    _rule("5. 重投：attempt 变成 2")
    survivor = a if b.died_on else b
    await _wait_for(lambda: _handled(survivor, b.died_on))
    print(f"  接过它的是「{survivor.name}」。")

    _rule("6. 结论")
    all_handled = sorted(a.handled + b.handled)
    for task_id, attempt in all_handled:
        mark = "\033[33m<- 被抢回来重跑过\033[0m" if attempt > 1 else ""
        print(f"  {task_id}  第 {attempt} 次投递  {mark}")
    expected = [*ids, "run-5", "run-6"]
    if sorted(t for t, _ in all_handled) != sorted(expected):
        print(f"\n  \033[31m有任务没跑完：{sorted(set(expected) - {t for t, _ in all_handled})}\033[0m")
        return 1
    print(f"\n  {len(expected)} 条任务全部处理完，其中 {b.died_on} 是被同伴抢回来重跑的。")
    print("  attempt=2 这个数字是死信判定的依据：超过 MAX_ATTEMPTS 就该停下问为什么，")
    print("  而不是无限重试把 token 烧光。")

    _rule("自己复核")
    print(f"  docker compose exec redis redis-cli -n {DEMO_DB} XPENDING {STREAMS['tasks']} {GROUP}")
    print(f"  docker compose exec redis redis-cli -n {DEMO_DB} XINFO CONSUMERS {STREAMS['tasks']} {GROUP}")
    print(f"  docker compose exec redis redis-cli -n {DEMO_DB} XRANGE {STREAMS['tasks']} - + COUNT 2")

    stop.set()
    for task in loops:
        task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.gather(*loops, return_exceptions=True)
    await queue.close()
    return 0


def _default_url() -> str:
    """演示用的 Redis URL：把配置里的库号换成 :data:`DEMO_DB`。"""
    parts = urlsplit(get_settings().redis_url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{DEMO_DB}", "", ""))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="队列层容错演示：副本猝死 → 同伴回收")
    parser.add_argument("--redis-url", default=None, help=f"默认 {_default_url()}（会 flushdb，别指向 db 0）")
    args = parser.parse_args()

    # 演示的旁白走 stdout，日志走 stderr（项目的约定），所以这里把日志压到 WARNING ——
    # 否则 INFO 级的队列日志会把旁白冲散。要排查就用 LOG_LEVEL=DEBUG 覆盖。
    setup_logging(level="WARNING", json_output=False)
    try:
        sys.exit(run(main(args.redis_url or _default_url())))
    except KeyboardInterrupt:
        sys.exit(130)
