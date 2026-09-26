#!/usr/bin/env python3
"""从真实的 GitHub PR 录一份**可回放**的 webhook 载荷。

    python tasks.py record-fixture --repo Satan508-h/sfly-playground --pr 1

输出写进 ``fixtures/webhook_pr.json`` —— 也就是 ``replay_webhook.py`` 默认回放的
那份载荷。**它是录出来的，不是编的**，而这件事有两个具体后果：

* 载荷里的 ``files`` 字段要求是 ``/pulls/{n}/files`` 的**原始响应**
  （见 ``sfly_api/github_payload.py`` 的说明）。手写一份等于把
  「我们以为 GitHub 会返回什么」当成了「GitHub 会返回什么」——
  ``patch`` 的 hunk 头、``status`` 的取值、二进制文件没有 ``patch`` 字段
  这些细节，猜错任何一处都不会报错，只会让本地演示和线上行为分叉。
* ``repository.full_name`` 和 ``pull_request.number`` 决定**评论发到哪里**。
  编出来的仓库名会让 publish 走完三次重试之后稳定 404 —— 那正是 M7 第二步
  验收之后的已知限制，这个脚本就是来消掉它的。

### 三个调用，两种性质

前两个是**脚手架**：

* ``GET /repos/{repo}`` —— 仓库 id / node_id / owner
* ``GET /repos/{repo}/pulls/{n}`` —— 标题、作者、head/base 的 sha 与 ref

真实 webhook 自带这些字段，产品路径上不会再问一次，所以它们没有进
``GitHubClient``：往产品代码里加一个只为脚本存在的方法，是把脚手架焊进承重墙。

第三个是**产品路径本身**：

* ``GET /repos/{repo}/pulls/{n}/files`` —— 走 ``GitHubClient.pull_files``，
  同一套分页、同一套重试、同一套 ``User-Agent``。录 fixture 的调用和线上审 PR
  的调用是同一段代码；两份「看起来一样」的实现迟早会分叉，而分叉的表现是
  「回放能审、真上线审不了」。

### 它不打印 token

``GitHubClient`` 自己管鉴权头。这里的 ``_get_json``（脚手架的两次调用）
照抄同一组常量，也照抄「不回显任何响应头」。
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from sfly_agent.github.client import ACCEPT, API_VERSION, USER_AGENT, GitHubClient
from sfly_shared.aio import run
from sfly_shared.config import get_settings

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "fixtures" / "webhook_pr.json"

#: 脚手架的两次调用等多久。**没有重试** —— 它们是脚本的一部分，失败了
#: 重跑一遍就是了；而给它们配一套重试逻辑，就等于在 ``GitHubClient``
#: 之外再养一份会漂移的重试策略。
SCRATCH_TIMEOUT_S = 30.0


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="record_pr_fixture.py",
        description="从真实 PR 录一份 webhook 载荷（files 字段是 /pulls/{n}/files 的真实响应）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--repo", required=True, metavar="OWNER/NAME", help="owner/name")
    p.add_argument("--pr", required=True, type=int, metavar="N", help="PR 编号")
    p.add_argument("--out", default=str(DEFAULT_OUT), metavar="PATH")
    p.add_argument(
        "--action",
        default="opened",
        help="载荷里的 action（默认 opened；改成 synchronize 就是「推了新提交」那一次）",
    )
    return p.parse_args(argv)


def _dig(data: Mapping[str, Any], *path: str) -> Any:
    """按路径取嵌套字段。中间任何一层不是对象就返回 ``None``。"""
    cur: Any = data
    for key in path:
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(key)
    return cur


def _text(data: Mapping[str, Any], *path: str) -> str:
    value = _dig(data, *path)
    return str(value).strip() if value is not None else ""


def _write(path: Path, payload: dict[str, Any]) -> int:
    """写载荷，返回字节数。

    **同步函数**（虽然调用方是 async）—— 和 ``replay_webhook.py`` 的
    ``_load_body`` 同一个理由：文件 IO 是本地、几 KB、一次性的，为它开线程
    或包一层 ``to_thread`` 只是让代码多一层。这条边界是「async 里只放
    IO 密集的远端调用」，而磁盘不在此列。
    """
    # ``write_text`` 会把 ``\n`` 翻成 ``os.linesep``（Windows 上是 CRLF），
    # 于是每次录完 git 都显示整个文件变了 —— 而真正变的可能只有一行。
    # 直接写字节，换行符只由我们自己决定。（同一个坑在 tests/ 里踩过一次，
    # 见 CLAUDE.md 里 ``Path.write_text`` 那条。）
    data = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(data)
    return len(data)


async def _get_json(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    """脚手架用的**一次性** GET。失败就直说，不重试。"""
    try:
        resp = await client.get(path)
        resp.raise_for_status()
        data: Any = resp.json()
    except httpx.HTTPError as exc:
        raise SystemExit(f"!! GET {path} 失败：{exc}") from None
    if not isinstance(data, dict):
        raise SystemExit(f"!! GET {path} 返回的不是对象：{type(data).__name__}")
    return dict(data)


def _payload(
    *,
    action: str,
    repo: Mapping[str, Any],
    pr: Mapping[str, Any],
    files: list[dict[str, Any]],
    recorded_at: str,
    pr_number: int,
) -> dict[str, Any]:
    """把三个响应拼成 GitHub 会发的那种形状。

    只放**这个项目会读的字段**（见 ``bootstrap_from_payload``），外加一条
    ``_note`` 说明出处。真实的 webhook 载荷有三十多个顶层键，照抄它们
    唯一的后果是让这份 fixture 变得没法一眼读完 —— 而它的用途正是
    「一眼读完，确认审的是哪一段代码」。

    ``installation`` 刻意**不写**：用 user token 手工开出来的 PR 没有
    GitHub App 安装 id，编一个只会让 ``installation_id`` 这个字段看起来
    有人用。
    """
    return {
        "action": action,
        "number": pr_number,
        "pull_request": {
            "number": pr_number,
            "node_id": _text(pr, "node_id"),
            "title": _text(pr, "title"),
            "draft": bool(pr.get("draft") is True),
            "user": {"login": _text(pr, "user", "login")},
            "head": {"sha": _text(pr, "head", "sha"), "ref": _text(pr, "head", "ref")},
            "base": {"sha": _text(pr, "base", "sha"), "ref": _text(pr, "base", "ref")},
        },
        "repository": {
            "id": repo.get("id"),
            "node_id": _text(repo, "node_id"),
            "full_name": _text(repo, "full_name"),
            "name": _text(repo, "name"),
            "owner": {"login": _text(repo, "owner", "login")},
        },
        "files": files,
        # 以 ``_`` 开头的键不是 GitHub 的字段，也不会被任何解析器读到
        # （``bootstrap_from_payload`` 只按路径取值）。它存在的理由是：
        # 半年后打开这个文件的人要能知道**它是怎么来的**。
        "_note": (
            f"由 scripts/record_pr_fixture.py 于 {recorded_at} 从 "
            f"{_text(repo, 'full_name')}#{pr_number} 录制。"
            "repository / pull_request 来自 GET /repos 与 GET /pulls/{n}（重建 webhook 的元数据），"
            "files 来自 GET /pulls/{n}/files —— 真实响应，未经改动。"
            f"PR: {_text(pr, 'html_url') or _text(pr, 'url')}"
        ),
    }


async def _main(args: argparse.Namespace) -> int:
    settings = get_settings()
    token = settings.github_token
    if not token:
        print("!! 没有 GITHUB_TOKEN —— 私有仓库读不到，公开仓库也会被限流到 60 次/小时")
        print("   在 .env 里配一个，然后重跑（见 .env.example）")
        return 1
    if "/" not in args.repo:
        print("!! --repo 要写成 owner/name")
        return 1

    base = settings.github_api_base
    print(f"  仓库 {args.repo}  PR #{args.pr}")
    print(f"  API  {base}")

    # 脚手架专用的一小块：只读元数据，一次就够。
    scratch = httpx.AsyncClient(
        base_url=base,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": ACCEPT,
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        },
        timeout=httpx.Timeout(SCRATCH_TIMEOUT_S),
    )
    client = GitHubClient(token=token, base_url=base)
    try:
        repo = await _get_json(scratch, f"/repos/{args.repo}")
        pr = await _get_json(scratch, f"/repos/{args.repo}/pulls/{args.pr}")
        files = await client.pull_files(args.repo, args.pr)
    finally:
        await scratch.aclose()
        await client.aclose()

    if not files:
        print("!! 这个 PR 没有任何带 patch 的文件 —— 录出来的载荷审不出东西")
        return 1

    number = pr.get("number")
    pr_number = int(number) if isinstance(number, int) else args.pr
    recorded_at = datetime.now(UTC).isoformat(timespec="seconds")
    payload = _payload(
        action=args.action,
        repo=repo,
        pr=pr,
        files=files,
        recorded_at=recorded_at,
        pr_number=pr_number,
    )

    out = Path(args.out)
    size = _write(out, payload)

    head_sha = _text(pr, "head", "sha")
    additions = sum(int(f.get("additions") or 0) for f in files)
    deletions = sum(int(f.get("deletions") or 0) for f in files)
    print(f"  标题 {_text(pr, 'title')}")
    print(f"  作者 {_text(pr, 'user', 'login')}  状态 {_text(pr, 'state')}")
    print(f"  head {head_sha[:12]}（{_text(pr, 'head', 'ref')}）")
    print(f"  文件 {len(files)} 个  +{additions} -{deletions}")
    print(f"  写出 {out}（{size} 字节）")

    if _text(pr, "state") != "open":
        # 关掉的 PR 发不出 review（GitHub 会 422）。这里只警告，不拦 ——
        # 「录一份历史 PR 的载荷去复盘」是个合理的用途。
        print(f"\n!! 这个 PR 的状态是 {_text(pr, 'state')}，往它上面发评论会失败")

    print("\n[OK] 录好了。跑一遍：python tasks.py demo --follow")
    return 0


def main() -> None:
    raise SystemExit(run(_main(_args())))


if __name__ == "__main__":
    main()
