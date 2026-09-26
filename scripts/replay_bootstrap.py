#!/usr/bin/env python3
"""把一条已经处理过的 bootstrap **原样重投一次** —— 走一遍「节点重放」。

    python tasks.py replay-bootstrap --task-id 01M3DVHP409TBPY721JN9VTA0P

### 为什么需要它

``publish`` 的三道防重复闸里，前两道**在正常运行时永远走不到**：publish 每个 run
只跑一次，而它们真正的触发条件是「编排器在发出评论之后、写库之前崩了」。
那件事没法等，也没法稳定复现 —— 于是它就只有单测覆盖，而「注释发出去了但
写库失败」恰好是最需要真实证据的一条路径。

重投一条 bootstrap 就等价于那个场景：同一个 ``task_id``、同一份报告、同一批
已经躺在 ``worker_results`` 里的结果（Worker 会走 ``exists_result`` 快路径跳过
重算，屏障照常闭合）。预期是 **PR 上不会多出第二条评论**，事件流里多一条
``publish.done``。

### 光重投还不够：入口还有一道闸

``GraphRunner.handle`` 会先看 ``run.status``，**终态的 run 直接把消息丢掉**
（``graph.bootstrap_ignored``）—— 那是对的：GitHub 超时重投同一个 webhook 时，
重投的是一个已经审完的 PR，不该再审一遍。代价是「重投」这条路走不到 ``publish``。

所以这个工具有两个**制造崩溃窗口**的开关，它们对应的正是那两行状态：

* ``--simulate-crash`` —— 把状态改回 ``aggregating``。那是 ``publish`` 发出评论
  之后、写终态之前崩溃时留下的状态。
* ``--forget-comment-id`` —— 把 ``github_comment_id`` 抹成 NULL。那是「评论发出去了、
  但写库那一步失败了」的样子。

两个都不改代码路径：改完之后图照常被唤醒、照常跑到 ``publish``，被检验的是
**真实的那两道闸**。

### 两种验法

* ``--simulate-crash`` —— 命中第 1 道闸，事件里 ``form=already``。
* ``--simulate-crash --forget-comment-id`` —— 第 1 道认不出来（id 是 NULL），
  publish 会去 PR 上找隐藏标记、找到之后**把 id 写回库**（``mark_published``），
  事件里 ``form=adopted:review`` —— 顺带证明那种半途失败能自愈。

两种都会打印结论并以退出码表示「有没有多出一条评论」，所以它可以进 CI 之外的
手动验收清单。

### 它为什么在宿主机跑

镜像里只 ``COPY`` 了 ``apps/`` 和 ``packages/`` —— ``scripts/`` 不是运行时的一部分，
所以 ``docker compose exec`` 找不到这个文件。宿主机跑用的是 ``.env`` 里那套
**host 侧地址**（``localhost:56379`` / ``localhost:55432``），集成测试连的也是这两个。
"""

from __future__ import annotations

import argparse
import asyncio
import time
from typing import Any

from redis.asyncio import Redis

from sfly_bus.base import STREAMS, RunStore
from sfly_bus.factory import open_dependencies
from sfly_bus.postgres import PostgresPool
from sfly_shared.aio import run
from sfly_shared.config import get_settings
from sfly_shared.contracts import BootstrapMessage, RunStatus

#: 往回翻多少条流条目去找 ``--task-id``。审过的 PR 远没有这么多条，
#: 而 ``XRANGE`` 是 O(N) 的 —— 给一个够大的上限比「一直翻到底」安全。
SCAN_ENTRIES = 200

#: 等那条新的 ``publish.done`` 出现。整个重放是「读已经存在的结果 + Mock LLM」，
#: 正常在两三秒内；给 30 秒是因为它要和正在跑的编排器抢一次调度。
WAIT_S = 30.0
POLL_S = 0.5


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="replay_bootstrap.py",
        description="重投一条 bootstrap，验证 publish 的防重复闸",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--task-id", required=True, metavar="ID", help="要重投的 run（task_id）")
    p.add_argument("--stream", default=STREAMS["bootstrap"], metavar="NAME")
    p.add_argument("--timeout", type=float, default=WAIT_S, metavar="S")
    p.add_argument(
        "--simulate-crash",
        action="store_true",
        help="先把状态改回 aggregating（publish 崩溃窗口留下的状态）—— 不改就没有闸可验",
    )
    p.add_argument(
        "--forget-comment-id",
        action="store_true",
        help="先把 github_comment_id 抹成 NULL（模拟评论已发出、写库失败）",
    )
    return p.parse_args(argv)


async def _forget_comment_id(postgres: PostgresPool, task_id: str) -> None:
    """把 ``github_comment_id`` 清空 —— **原生 SQL，不走仓储**。

    仓储里刻意没有「清空 id」这个方法：业务上不存在这个动作，有的话就是
    留给人手动改库的后门。而故障注入要的正是那个后门 —— 所以它只出现在这里，
    并且带着这段说明。
    """
    async with postgres.connection() as conn, conn.transaction():
        # 主键列叫 ``task_id``，不叫 ``id``（见 001 迁移）—— 写成 ``id`` 的报错是
        # ``column "id" does not exist``，一个不告诉你该写什么的错误。
        await conn.execute("UPDATE review_runs SET github_comment_id = NULL WHERE task_id = %s", (task_id,))


async def _find_payload(redis_url: str, stream: str, task_id: str) -> tuple[str, str] | None:
    """在流里找那条 bootstrap，返回 ``(entry_id, payload_json)``。

    **直接开一个 redis 连接，不走 ``TaskQueue``**：协议里刻意没有「看一眼但不
    消费」的读法（消费者只该用 ``XREADGROUP``，否则会把消息从别人手里抢走）。
    而这里要的正是「看一眼、原样投回去」—— 那是故障注入，不是消费。

    匹配用的是条目里平铺的 ``task_id`` 字段（见 ``BootstrapMessage`` 的文档：
    平铺那几个字段就是为了「不必反序列化整包就能看出这是哪个 PR 的」）。
    """
    client: Redis = Redis.from_url(redis_url, decode_responses=True)
    try:
        raw = await client.xrevrange(stream, count=SCAN_ENTRIES)
    finally:
        await client.aclose()

    # 两个 ``or``：redis-py 的返回类型里带着 ``None``（它可能被用在
    # ``XREADGROUP ... NOACK`` 那种拿不到东西也合法的调用上），而这里的
    # 语义是「空就是空」—— 写成 ``for ... in raw or []`` 比在下面
    # 逐个字段防 None 更贴近真正可能发生的事。
    for entry_id, fields in raw or []:
        row = fields or {}
        if row.get("task_id") != task_id:
            continue
        payload = row.get("payload")
        if isinstance(payload, str) and payload.strip():
            return str(entry_id), payload
        return None
    return None


async def _high_water(store: RunStore, task_id: str) -> int:
    """重投前的 ``seq`` 水位。用它把「新事件」和这之前的历史区分开。"""
    events = await store.events_since(task_id, 0)
    return max((e.seq for e in events), default=0)


async def _wait_for_publish(
    store: RunStore, task_id: str, after_seq: int, wait_s: float
) -> dict[str, Any] | None:
    """等一条新的 ``publish.done`` / ``publish.failed``，返回它的 payload。

    参数叫 ``wait_s`` 而不是 ``timeout``：这里等的是**编排器**（另一条进程里的
    一串协程），不是一个可以包在 ``asyncio.timeout()`` 里的调用 ——
    名字叫 timeout 会让人以为可以那样改。
    """
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        for event in await store.events_since(task_id, after_seq):
            if event.kind in ("publish.done", "publish.failed"):
                inner = event.payload.get("payload")
                return dict(inner) if isinstance(inner, dict) else dict(event.payload)
        await asyncio.sleep(POLL_S)
    return None


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    deps = await open_dependencies(settings)
    try:
        if deps.queue is None:  # pragma: no cover —— 精简模式没有流可投
            print("!! 当前是精简模式（没有 Redis 队列），这条路只对完整模式有意义")
            return 1

        found = await _find_payload(settings.redis_url, args.stream, args.task_id)
        if found is None:
            print(f"!! 在 {args.stream} 里找不到 task_id={args.task_id} 的条目")
            print("   先跑一次 python tasks.py demo，然后从它的输出里拿 task_id")
            return 1
        entry_id, payload = found

        message = BootstrapMessage.model_validate_json(payload)
        run_before = await deps.store.get_run(args.task_id)
        water = await _high_water(deps.store, args.task_id)

        print(f"  源条目 {entry_id}（{args.stream}）")
        print(f"  重投 {args.task_id}  {message.repo_id}#{message.pr_number}")
        print(f"  库里的 comment id  {run_before.github_comment_id if run_before else None}")
        print(f"  事件水位 seq={water}")

        if args.forget_comment_id:
            # 顺序要紧：先抹 id 再唤醒图，否则图跑起来时 id 还在（闸 1 已命中）。
            await _forget_comment_id(deps.postgres, args.task_id)
            print("  模拟：github_comment_id 已抹成 NULL（评论发了、写库失败）")
        if args.simulate_crash:
            # 终态的 run 会被 GraphRunner 直接丢弃（graph.bootstrap_ignored），
            # 所以重投之前必须先把它挪回崩溃窗口里的那个状态。
            await deps.store.set_status(args.task_id, RunStatus.AGGREGATING)
            print("  模拟：状态已改回 aggregating（publish 发出评论后崩溃）")
        print()

        new_id = await deps.queue.publish_bootstrap(message)
        print(f"  已投出 {new_id}，等编排器重放一遍……")

        outcome = await _wait_for_publish(deps.store, args.task_id, water, args.timeout)
        if outcome is None:
            print(f"!! {args.timeout:.0f} 秒内没有新的 publish 事件 —— 编排器在跑吗？")
            print("   python tasks.py logs --tail 50 orchestrator")
            return 1

        form = str(outcome.get("form") or "?")
        print(f"\n  publish.done  form={form}")
        print(f"    reason      {outcome.get('reason')}")
        print(f"    comment_id  {outcome.get('comment_id')}")
        print(f"    inline_sent {outcome.get('inline_sent')}")

        after = await deps.store.get_run(args.task_id)
        print(f"  库里的 comment id  ->  {after.github_comment_id if after else None}")

        # 结论只认 form：`already` 是第 1 道闸，`adopted:*` 是第 2 道。
        # 其余任何取值都意味着**真的又发了一条**，而重复评论是用户可见的噪音。
        if form == "already" or form.startswith("adopted:"):
            gate = "第 1 道（库里的 comment id）" if form == "already" else "第 2 道（正文里的隐藏标记）"
            print(f"\n[OK] 命中{gate} —— 没有重复评论")
            print("     PR 上应该还是原来那一条，行内评论数不变")
            return 0

        print(f"\n[!!] form={form} —— 这不是「跳过」，去 PR 上数一下评论条数")
        return 1
    finally:
        await deps.close()


def main() -> None:
    raise SystemExit(run(_main(_args())))


if __name__ == "__main__":
    main()
