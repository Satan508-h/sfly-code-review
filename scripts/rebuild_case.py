"""从公开仓库的**真实漏洞修复提交**重建一条评测用例。

    python scripts/rebuild_case.py --repo aiohttp/aiohttp --commit <sha> \
        --category path_traversal --case-id rebuilt-aiohttp-traversal

### 为什么这一组用例的 ground truth 最硬

其余两组用例的「正确答案」是**我写的**：手写注入组由我判断哪一行有问题，
干净组由我判断它没问题。面试官完全可以问「凭什么是你说的那样」。

这一组不一样：那些提交**已经被维护者合并、且带着 CVE 编号**。
漏洞在哪里、修法是什么，都不是我的判断 —— 我只是把修复**倒过来放回去**，
让有漏洞的版本重新出现。

### 「回退修复」就是反转补丁

修复做的是「把有漏洞的代码换成安全的写法」。把补丁的 ``+`` 与 ``-`` 对调、
hunk 头的新旧范围对调，得到的就是「让有漏洞的版本重新出现」的那次改动 ——
于是它成为**本次新增的行**，正好是可以被审查、也应该被审查的对象。

行的镜像性还有一层好处：**被测系统看到的上下文和真实提交里一模一样**，
只是方向反了。这不是我编出来的代码。

### 这个脚本是**可追溯性**本身

它被提交进仓库，所以「这 10 条用例从哪来」有据可查：仓库、commit sha、
类目、以及生成时的原话说明都写进了 yaml。跑一遍就能复现出同样的 diff。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "eval" / "cases"

#: ``@@ -a,b +c,d @@`` —— 结尾还可能有函数名之类的上下文，原样留着。
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


class RebuildError(Exception):
    """这个提交不适合做用例。**讲清楚为什么**，因为下一个候选还得挑。"""


def _request(url: str, token: str) -> dict[str, object]:
    # ``S310`` 在这里是误报：URL 由本脚本拼死成 ``https://api.github.com/...``，
    # 命令行的 ``--repo`` 只进路径，改不了 scheme。写清楚原因而不是关掉规则 ——
    # 关掉的话，将来真有人把 URL 改成可变的时候也没有东西会提醒。
    request = urllib.request.Request(  # noqa: S310
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "sfly-eval-rebuild",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        raise RebuildError(f"GitHub 返回 {exc.code}：{url}") from exc
    except OSError as exc:
        raise RebuildError(f"连不上 GitHub（{exc}）：{url}") from exc
    if not isinstance(payload, dict):
        raise RebuildError(f"返回的不是对象：{url}")
    return payload


def _invert_hunk_header(line: str) -> str:
    match = _HUNK.match(line)
    if match is None:
        raise RebuildError(f"看不懂的 hunk 头：{line!r}")
    old_start, old_count, new_start, new_count, tail = match.groups()
    old = f"{new_start},{new_count}" if new_count is not None else new_start
    new = f"{old_start},{old_count}" if old_count is not None else old_start
    return f"@@ -{old} +{new} @@{tail}"


def invert_patch(patch: str) -> str:
    """把一段补丁反过来。见模块文档。"""
    out: list[str] = []
    for line in patch.splitlines():
        if line.startswith("@@"):
            out.append(_invert_hunk_header(line))
        elif line.startswith(("+++", "---")):
            # 文件头**不反转**：``--- a/x`` / ``+++ b/x`` 描述的是「哪个文件」，
            # 与改动方向无关。反转它们会得到一个语义颠倒的文件头。
            out.append(line)
        elif line.startswith("+"):
            out.append("-" + line[1:])
        elif line.startswith("-"):
            out.append("+" + line[1:])
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def _pick_file(files: list[dict[str, object]], wanted: str | None) -> dict[str, object]:
    """挑出要用的那个文件。**只接受带补丁的单文件改动。**

    多文件提交不选：回退整批会把若干不相关的改动一起放进来，
    而 ground truth 只能标一个类目 —— 多出来的部分是评测里的噪声，
    通常还会被算成假阳性。
    """
    usable = [f for f in files if isinstance(f.get("patch"), str) and f["patch"]]
    if wanted:
        for item in usable:
            if item.get("filename") == wanted:
                return item
        raise RebuildError(f"提交里没有 {wanted}（有的：{[f.get('filename') for f in usable]}）")
    if len(usable) != 1:
        names = [str(f.get("filename")) for f in usable]
        raise RebuildError(f"提交改了 {len(usable)} 个可用的文件，需要 --file 指定：{names}")
    return usable[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从真实漏洞修复提交重建一条评测用例")
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--commit", required=True, help="修复漏洞的那个提交")
    parser.add_argument("--category", required=True, help="类目（必须是 CATEGORY_OWNER 的键）")
    parser.add_argument("--severity", default="high", help="真实危害对应的严重度")
    parser.add_argument("--case-id", required=True)
    parser.add_argument("--file", default=None, help="提交改了多个文件时指定用哪一个")
    parser.add_argument("--note", default="", help="这条用例想测什么")
    parser.add_argument("--line", type=int, default=None, help="漏洞所在行（默认取唯一的那个新增行）")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT / "packages" / "shared"))
    from sfly_shared.config import Settings
    from sfly_shared.contracts import CATEGORY_OWNER, Severity
    from sfly_shared.diff import iter_added_lines

    if args.category not in CATEGORY_OWNER:
        print(f"类目 {args.category!r} 不在分类体系里", file=sys.stderr)
        return 2
    if Severity(args.severity) not in set(Severity):
        print(f"严重度 {args.severity!r} 不合法", file=sys.stderr)
        return 2

    payload = _request(
        f"https://api.github.com/repos/{args.repo}/commits/{args.commit}",
        Settings().github_token,
    )
    files = payload.get("files")
    if not isinstance(files, list):
        raise RebuildError("返回里没有 files")

    # 提交信息也是证据的一部分：它常常写着 CVE 编号和修的是什么。
    commit_info = payload.get("commit")
    message = ""
    if isinstance(commit_info, dict) and isinstance(commit_info.get("message"), str):
        message = str(commit_info["message"]).strip().splitlines()[0]

    chosen = _pick_file(files, args.file)
    patch = str(chosen["patch"])
    if any(line.startswith("-") and not line.startswith("---") for line in patch.splitlines()):
        pass
    else:
        raise RebuildError("这个补丁只有新增行（纯新增文件的提交反向没有意义）")

    filename = str(chosen["filename"])
    inverted = invert_patch(patch)
    body = f"diff --git a/{filename} b/{filename}\n--- a/{filename}\n+++ b/{filename}\n{inverted}"

    added = list(iter_added_lines(body))
    if not added:
        raise RebuildError("反转之后没有任何新增行")
    if args.line is not None:
        target = next((item for item in added if item.line == args.line), None)
        if target is None:
            lines = ", ".join(f"{item.line}: {item.content.strip()}" for item in added)
            raise RebuildError(f"--line {args.line} 不是新增行。新增的有：\n  {lines}")
    elif len(added) == 1:
        target = added[0]
    else:
        lines = "\n  ".join(f"{item.line}: {item.content.strip()}" for item in added)
        print(f"这个补丁有 {len(added)} 个新增行，请用 --line 指定漏洞所在的那一行：\n  {lines}")
        return 2

    (CASES / f"{args.case_id}.diff").write_text(body, encoding="utf-8", newline="")
    note = args.note or f"回退修复：{message}"
    # yaml 里的字符串一律加引号 —— note 里出现 ``: `` 或 ``#`` 时
    # 不加引号会被 yaml 解析成别的东西，而报错会在很久之后。
    (CASES / f"{args.case_id}.yaml").write_text(
        f"id: {args.case_id}\n"
        "group: rebuilt\n"
        "origin: rebuilt\n"
        f"repo: {args.repo}\n"
        f"commit: {args.commit}\n"
        f"note: {json.dumps(note, ensure_ascii=False)}\n"
        "expected:\n"
        f"  - file: {filename}\n"
        f"    line: {target.line}\n"
        f"    category: {args.category}\n"
        f"    severity: {args.severity}\n",
        encoding="utf-8",
        newline="",
    )

    print(f"写好 {args.case_id}")
    print(f"  {args.repo}@{args.commit[:10]}  {message}")
    print(f"  {filename}:{target.line}  {target.content.strip()}")
    print(f"  {len(added)} 个新增行，其中这一个是 ground truth")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RebuildError as error:
        print(f"重建失败：{error}", file=sys.stderr)
        raise SystemExit(1) from error
