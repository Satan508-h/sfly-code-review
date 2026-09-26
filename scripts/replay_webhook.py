#!/usr/bin/env python3
"""回放一份 GitHub webhook 载荷 —— 投 N 次、验幂等、可选跟全程时间线。

    python scripts/replay_webhook.py                     # 同一份载荷投 3 次
    python scripts/replay_webhook.py --new-delivery      # 每次换一个投递 id
    python scripts/replay_webhook.py --follow            # 跟到 run 结束
    python scripts/replay_webhook.py --follow --drop-after 3   # 断开再重连，验证无缺口

它是 M6 的可运行证据：**同一份 webhook 投 3 次，只产生 1 个 run。**
去重发生在两层，脚本把两层都演出来：

1. **投递层**（默认）：三次用同一个 ``X-GitHub-Delivery`` —— 这正是 GitHub
   超时重投的样子（重投时那个 GUID 不变）。第二次起返回 ``duplicate``，
   连库都不用查第二次。
2. **run 层**（``--new-delivery``）：每次换一个投递 id，但载荷里的
   ``head_sha`` 不变 —— 于是幂等键（``repo:pr:head_sha``）撞车。
   这一层是 ``review_runs.idempotency_key`` 的唯一约束在兜，
   API 只负责把「已经审过了」这个答案告诉调用方。

两层都要有：只做投递层，那么「push 事件和 pull_request 事件同时到达」
（两个不同的 delivery id、同一个提交）就会审两遍。

### 它说的是 HTTP，不是数据库

脚本从头到尾只做两件事：POST 一个载荷、GET 一个 run。**不连 Postgres、
不连 Redis** —— 这样它才能当成一个真正的外部调用方用，也才能证明
「接口层自己就足以表达幂等」。要直接看库，用 ``psql`` 或
``python tasks.py tables``。

### head_sha 默认会被改写

不改写的话，第二次跑这个脚本会命中**上一轮演示**留下的 run，
输出变成「1 duplicate + 2 duplicate」—— 看着像坏了，其实是幂等做对了。
所以默认换一个 head_sha（模拟往 PR 上推了新提交），
``--verbatim`` 保留原样，那才是「同一个提交投三次」的严格形态。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import secrets
import sys
from pathlib import Path
from typing import Any

import httpx

from sfly_api.webhook import DELIVERY_HEADER, EVENT_HEADER, SIGNATURE_HEADER, sign
from sfly_shared.aio import run
from sfly_shared.config import get_settings

# --- Windows 控制台编码 --------------------------------------------------- #
#
# 和 tasks.py / demo_reclaim.py 里那一段同一个理由：Windows 控制台默认代码页是
# GBK，中文能显示、✓ / ✗ 这类符号装不下 —— 输出被重定向或接到管道时
# （git-bash、CI、`| grep`）会退化成按 GBK 编码字节流，编码失败抛在 print() 里，
# 于是**命令实际跑完了才崩**，看起来像执行失败。
#
# 所以下面所有提示标记一律 ASCII（OK / !! / --），这里再加一道兜底。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        with contextlib.suppress(ValueError, OSError):
            _stream.reconfigure(errors="replace")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PAYLOAD = ROOT / "fixtures" / "webhook_pr.json"

#: 等 run 出现（``GET /api/runs/{id}`` 返回 200）的时长。
#:
#: run 是**编排器**建的，不是 API 建的（见 ``routes/runs.py`` 的说明），
#: 所以投递成功之后有一小段窗口里它还不存在。正常在百毫秒级，
#: 这里给得宽松些 —— 冷启动的数据库连接、队列的第一次拉取都可能慢一拍。
RUN_WAIT_S = 15.0

#: 开流前先轮询详情接口的间隔。**先 JSON 唤醒再开流** —— 直接开流的话，
#: 冷启动期间拿到的是一条 404，而浏览器里的 EventSource 会把它当成
#: 永不重试的失败。
RUN_POLL_S = 0.4


def _host_port() -> str:
    """API 在宿主机上的端口。

    从 ``.env`` 读而不是写死 8000：这台开发机上 5432/6379 被别的项目占着，
    端口是被改过的（``API_HOST_PORT``），写死的话提示出来的地址是错的 ——
    而一个指向错误端口的提示比没有提示更误导。
    """
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, _, value = line.strip().partition("=")
            if key.strip() == "API_HOST_PORT" and value.strip():
                return value.strip()
    return "8000"


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="replay_webhook.py",
        description="回放 GitHub webhook 载荷，验证投递去重与断线补齐",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--payload", default=str(DEFAULT_PAYLOAD), metavar="PATH")
    p.add_argument(
        "--url",
        default=None,
        metavar="URL",
        help=f"webhook 地址（默认 http://localhost:{_host_port()}/api/webhook）",
    )
    p.add_argument("--times", type=int, default=3, help="投几次（默认 3）")
    p.add_argument("--delivery", default=None, help="固定的 X-GitHub-Delivery（默认本次运行随机生成一个）")
    p.add_argument("--event", default="pull_request", help="X-GitHub-Event 的值（默认 pull_request）")
    p.add_argument(
        "--new-delivery",
        action="store_true",
        help="每次投递换一个新的 delivery id —— 演示第二层去重（撞的是幂等键）",
    )
    p.add_argument(
        "--verbatim",
        action="store_true",
        help="原样发送载荷里的 head_sha（默认改写，避免撞上一轮演示留下的 run）",
    )
    p.add_argument("--secret", default=None, help="webhook 密钥（默认取 GITHUB_WEBHOOK_SECRET）")
    p.add_argument("--follow", action="store_true", help="投完之后跟 SSE 时间线到 run 结束")
    p.add_argument(
        "--drop-after",
        type=int,
        default=0,
        metavar="N",
        help="收到 N 条事件后主动断开，再用 Last-Event-ID 重连（验证无缺口）",
    )
    return p.parse_args(argv)


# --------------------------------------------------------------------------- #
# 投递
# --------------------------------------------------------------------------- #


def _load_body(args: argparse.Namespace) -> bytes:
    """读载荷，必要的话改写 ``head_sha``。**返回要原样发送的字节。**

    签名是对**这一段字节**算的，所以它必须在发送前定下来、且之后不再变 ——
    先 ``json.dumps`` 再签名，不能反过来（重新序列化会改变字节，见
    ``sfly_api/webhook.py`` 里那段说明）。
    """
    text = Path(args.payload).read_text(encoding="utf-8")
    if args.verbatim:
        return text.encode("utf-8")

    payload: dict[str, Any] = json.loads(text)
    head = payload.get("pull_request", {}).get("head", {})
    sha = str(head.get("sha") or "")
    if not sha:
        return text.encode("utf-8")
    # 保留原 sha 的前 12 位，后面加一段随机 —— 一眼能看出「这是同一个提交的变体」
    head["sha"] = f"{sha[:12]}{secrets.token_hex(14)}"
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _headers(args: argparse.Namespace, delivery: str, body: bytes, secret: str) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "sfly-replay-webhook/0.1",
        EVENT_HEADER: args.event,
        DELIVERY_HEADER: delivery,
    }
    if secret:
        # 和服务器共用同一个 ``sign`` —— 两边各写一遍 HMAC 是那种会静默分叉的东西
        headers[SIGNATURE_HEADER] = sign(body, secret)
    return headers


async def _post_times(
    client: httpx.AsyncClient,
    args: argparse.Namespace,
    body: bytes,
    secret: str,
) -> list[dict[str, Any]]:
    """投 N 次，返回每次的响应体。"""
    results: list[dict[str, Any]] = []
    shared_delivery = args.delivery or f"cli-{secrets.token_hex(8)}"

    for i in range(1, args.times + 1):
        delivery = f"cli-{secrets.token_hex(8)}" if args.new_delivery else shared_delivery
        try:
            response = await client.post(
                args.url, content=body, headers=_headers(args, delivery, body, secret)
            )
        except httpx.RequestError as exc:
            print(f"\n!! 连不上 {args.url}：{exc}")
            print("   先起服务：python tasks.py up（或至少 up api postgres redis）")
            raise SystemExit(1) from None

        with contextlib.suppress(ValueError):
            body_json = response.json()
        payload = body_json if isinstance(body_json, dict) else {"raw": response.text[:200]}
        payload["_http"] = response.status_code
        payload["_delivery"] = delivery
        results.append(payload)

        mark = {202: "->", 200: "==", 401: "!!", 400: "!!", 503: "!!"}.get(response.status_code, "??")
        print(
            f"  {mark} #{i}  HTTP {response.status_code}  {payload.get('status', '?'):9} {payload.get('detail', '')}"
        )

        # 第二层去重需要 run **已经存在**才能触发（API 是读库判断的）。
        # 不等的话，三次投递会在 run 建出来之前全部到达 —— 全部 accepted，
        # 然后由编排器的幂等键兜住。结果同样是 1 个 run，但脚本就没法
        # 把「第二层去重生效了」这个事实打印出来了。
        if args.new_delivery and i == 1 and payload.get("task_id"):
            await _wait_for_run(client, args.url, str(payload["task_id"]))
    return results


def _verdict(results: list[dict[str, Any]], args: argparse.Namespace) -> bool:
    """打印结论并返回「是否符合预期」。"""
    accepted = [r for r in results if r.get("status") == "accepted"]
    duplicates = [r for r in results if r.get("status") == "duplicate"]
    tasks = {str(r["task_id"]) for r in results if r.get("task_id")}

    layer = "run 层（幂等键撞车）" if args.new_delivery else "投递层（同一个 delivery id）"
    print(f"\n  去重发生在 {layer}")
    print(f"  accepted {len(accepted)}  duplicate {len(duplicates)}  涉及的 run {len(tasks)} 个")
    for task_id in tasks:
        print(f"  run: {task_id}")

    ok = len(accepted) == 1 and len(duplicates) == args.times - 1 and len(tasks) == 1
    if ok:
        print(f"\n[OK] {args.times} 次投递 -> 1 个 run + {args.times - 1} 次 duplicate")
    else:
        print(f"\n[!!] 预期 1 accepted + {args.times - 1} duplicate / 1 个 run，实际不是")
    return ok


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


async def _wait_for_run(client: httpx.AsyncClient, url: str, task_id: str) -> bool:
    """等 run 出现。**先 JSON 唤醒再开流** —— 见 ``routes/events.py`` 的说明。"""
    base = url.rsplit("/api/", 1)[0]
    deadline = asyncio.get_running_loop().time() + RUN_WAIT_S
    while asyncio.get_running_loop().time() < deadline:
        response = await client.get(f"{base}/api/runs/{task_id}")
        if response.status_code == 200:
            return True
        await asyncio.sleep(RUN_POLL_S)
    print(f"  !! run {task_id} 在 {RUN_WAIT_S:.0f} 秒内没有出现 —— 编排器在跑吗？")
    return False


async def _read_stream(
    client: httpx.AsyncClient,
    url: str,
    *,
    cursor: int,
    drop_after: int,
    seen: list[int],
) -> int:
    """读一条 SSE 流，返回最后收到的 ``seq``。

    ``cursor == 0`` 时用 ``?after=`` 开流；否则用 ``Last-Event-ID`` 头 ——
    **两条路径都要走一遍**，因为浏览器只在重连时才带头（首屏没法设头），
    而只测其中一条的话，另一条坏了不会被发现。

    ``drop_after > 0`` 时收够那么多条就返回（调用方据此重连）——
    那是「断线」的模拟：不是让服务端关流，而是客户端自己走开。
    """
    headers = {"Last-Event-ID": str(cursor)} if cursor else {}
    params = {} if cursor else {"after": 0}

    event_id = 0
    name = "message"
    data: list[str] = []
    received_here = 0

    async with client.stream("GET", url, headers=headers, params=params) as response:
        if response.status_code != 200:
            print(f"  !! 开流失败：HTTP {response.status_code}")
            return cursor
        async for line in response.aiter_lines():
            if line == "":
                if data and event_id:
                    seen.append(event_id)
                    received_here += 1
                    _print_event(event_id, name, "\n".join(data))
                    cursor = event_id
                    if drop_after and received_here >= drop_after:
                        print(f"  -- 模拟断线：收到 {received_here} 条后主动断开 --")
                        return cursor
                event_id, name, data = 0, "message", []
                continue
            if line.startswith(":"):
                continue  # 心跳（sse-starlette 每 15 秒一条），不是事件
            field, _, value = line.partition(":")
            value = value.lstrip(" ")
            if field == "id":
                event_id = int(value) if value.isdigit() else 0
            elif field == "event":
                name = value
            elif field == "data":
                data.append(value)
    return cursor


#: 时间线里单个字段值的最大长度。``head_sha`` 有 40 字符、``deadline_at`` 更长，
#: 不截断的话一行会折成两三行，而时间线**是靠一眼扫过去看的**。
_MAX_VALUE_CHARS = 28


def _short(value: Any) -> str:
    text = str(value)
    return text if len(text) <= _MAX_VALUE_CHARS else text[: _MAX_VALUE_CHARS - 1] + "…"


def _print_event(seq: int, sse_name: str, raw: str) -> None:
    """打一行时间线。

    取的是事件**自己的 payload**（``RunEvent.payload``），不是整条 RunEvent ——
    后者会把 ``seq`` / ``task_id`` / ``created_at`` 这三个每一行都一样的字段
    重复打 12 遍，而真正想看的信息（``node=plan``、``worker_type=security``）
    被挤到看不见的地方。``None`` 的字段也丢掉：``error_class=None`` 只说明
    「这个字段存在」，那件事读一次就知道了。

    ``sse_name`` 是 SSE 帧的名字，**正常情况下永远是 ``"message"``** ——
    服务端刻意不发 ``event:`` 字段（理由见 ``sfly_api/sse.py`` 的 ``frame()``）。
    事件类型从 JSON 的 ``kind`` 取，那里才是唯一来源。
    """
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:  # pragma: no cover
        payload = {}
    if not isinstance(payload, dict):  # pragma: no cover —— 坏帧不该让演示崩掉
        payload = {}
    kind = str(payload.get("kind") or sse_name)
    inner = payload.get("payload")
    parts = [
        f"{k}={_short(v)}"
        for k, v in (inner if isinstance(inner, dict) else {}).items()
        if not isinstance(v, (dict, list)) and v is not None
    ]
    print(f"  #{seq:<4} {kind:<18} {' '.join(parts[:4])}")


async def _follow(
    client: httpx.AsyncClient,
    url: str,
    task_id: str,
    *,
    drop_after: int,
) -> bool:
    """跟一条 run 的时间线到结束，返回「有没有缺口」。"""
    base = url.rsplit("/api/", 1)[0]
    if not await _wait_for_run(client, url, task_id):
        return False

    events_url = f"{base}/api/runs/{task_id}/events"
    print(f"\n  时间线（SSE {events_url}）")
    seen: list[int] = []
    cursor = await _read_stream(client, events_url, cursor=0, drop_after=drop_after, seen=seen)
    if drop_after:
        # 重连走的是浏览器的那条路：带 ``Last-Event-ID`` 头，不带查询参数
        cursor = await _read_stream(client, events_url, cursor=cursor, drop_after=0, seen=seen)

    # **缺口怎么验**：拿详情接口（它返回这个 run 的全部事件）对一遍。
    # 不能靠「seq 连续」判断 —— ``run_events.seq`` 是**全表**自增，
    # 同一个 run 的 seq 本来就会跳（中间的事件属于别的 run）。
    detail = await client.get(f"{base}/api/runs/{task_id}")
    expected = (
        {int(e["seq"]) for e in detail.json().get("events", [])} if detail.status_code == 200 else set()
    )
    missing = sorted(expected - set(seen))
    extra = sorted(set(seen) - expected)

    print(f"\n  SSE 收到 {len(seen)} 条，详情接口 {len(expected)} 条")
    if seen == sorted(set(seen)) and not missing and not extra:
        print("[OK] 断线前后无缺口、无重复")
        return True
    print(f"[!!] 缺口 {missing[:5]} 重复 {extra[:5]}")
    return False


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


async def _main(args: argparse.Namespace) -> int:
    args.url = args.url or f"http://localhost:{_host_port()}/api/webhook"
    secret = args.secret if args.secret is not None else get_settings().github_webhook_secret
    body = _load_body(args)

    print(f"  载荷 {args.payload}")
    print(f"  地址 {args.url}")
    print(f"  签名 {'已配置密钥' if secret else '未配置密钥（服务端只在 full 模式下放行）'}")
    print(f"  投递 {args.times} 次\n")

    # 读超时给得长：SSE 是长连接，而心跳每 15 秒一条
    timeout = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        results = await _post_times(client, args, body, secret)
        ok = _verdict(results, args)

        task_id = next((str(r["task_id"]) for r in results if r.get("task_id")), None)
        if args.follow and task_id:
            ok = await _follow(client, args.url, task_id, drop_after=args.drop_after) and ok
        elif args.follow:
            print("\n  !! 没有 task_id，没法跟时间线")

    return 0 if ok else 1


def main() -> None:
    raise SystemExit(run(_main(_args())))


if __name__ == "__main__":
    main()
