"""GitHub 的 ``GET /repos/{owner}/{repo}/pulls/{n}/files`` 响应 → ``FilePatch``。

**为什么这条转换住在 shared，而不是在用到它的某一个 app 里**：它有两个消费者，
而它们**必须用同一份实现**：

* **网关** —— 收到的是录制/回放的载荷（``scripts/record_pr_fixture.py`` 录的），
  里面已经带着 ``files`` 字段；
* **编排器** —— 收到的是 GitHub 的真载荷，而 ``pull_request`` 事件**不带任何
  代码**，所以它得自己调一次 ``/pulls/{n}/files`` 再把响应喂进来。

两份实现的样子会一模一样，而分叉的后果是**「回放时能审、真上线审不了」**：
本地演示一路绿灯，线上开 PR 什么都不会发生。这条判据和 ``diff.py`` 当初从
``sfly_agent`` 搬到 ``sfly_shared`` 是同一条 —— **看消费者有没有权利依赖那个包**。
网关不该为了这一条转换拖进整个 LLM 栈。

### 补丁要**重新拼回一份 git diff** 再解析

``/pulls/{n}/files`` 的 ``patch`` 字段是 **hunk 片段**，不含
``diff --git`` / ``---`` / ``+++`` 三行头。而整个项目计算
「哪些行是变更行」（inline 评论只能锚定在这些行上，见 ``FilePatch`` 的文档）
用的是同一个解析器 ``parse_unified_diff``，它**需要那三行**：
少了文件头，Mock LLM 的 ``iter_added_lines`` 会**静默返回空** ——
表现是「审完了，0 条发现」，没有任何报错。

所以这里干的事是：把 API 的每条记录**还原成一段标准 git diff**，再交给
唯一的解析器。刻意不自己数新增行：数行号的规则（hunk 头声明的行数、
新旧两侧要分开计数）已经在那边踩过坑了，抄一遍就是把那些坑再踩一遍。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sfly_shared.contracts import FilePatch
from sfly_shared.diff import parse_unified_diff

#: 新文件 / 删除文件在 git diff 头里的模式行。**用它而不是自己设
#: ``is_new_file``**：解析器认这三行，让标志从解析结果里出来，
#: 「哪些文件算新文件」这件事就只有一个判断处。
_MODE_BY_STATUS: dict[str, str] = {
    "added": "new file mode 100644",
    "removed": "deleted file mode 100644",
}


def _entry_text(entry: Mapping[str, Any], key: str) -> str:
    value = entry.get(key)
    return str(value).strip() if value is not None else ""


def _file_block(entry: Mapping[str, Any]) -> str:
    """把一条 API 记录还原成一段标准 git diff。

    重命名的处理值得说明：GitHub 给的是 ``filename``（新名）+
    ``previous_filename``（旧名），而真正的 git 会写
    ``diff --git a/旧名 b/新名``。这里两个位置都写新名 ——
    **因为我们取路径的地方是 ``+++`` 行，它给的是新名**，也就是评论要锚定的
    那个路径。写旧名反而会让解析器拿旧名去发布评论，而 GitHub 会拒绝它。
    """
    path = _entry_text(entry, "filename")
    status = _entry_text(entry, "status") or "modified"
    old = "/dev/null" if status == "added" else f"a/{path}"
    new = "/dev/null" if status == "removed" else f"b/{path}"

    lines = [f"diff --git a/{path} b/{path}"]
    if mode := _MODE_BY_STATUS.get(status):
        lines.append(mode)
    lines.append(f"--- {old}")
    lines.append(f"+++ {new}")
    lines.append(str(entry.get("patch") or ""))
    return "\n".join(lines)


def patches_from_files(
    files: Sequence[Any],
    *,
    max_patch_chars: int,
) -> tuple[list[FilePatch], dict[str, str]]:
    """把 ``/pulls/{n}/files`` 的条目转成 ``FilePatch``。

    返回 ``(补丁, 跳过的文件及原因)``。跳过原因要一路带到日志和 UI ——
    「这个 PR 审了 3 个文件」和「这个 PR 有 5 个文件，2 个没法审」是两件事。
    """
    blocks: list[str] = []
    skipped: dict[str, str] = {}

    for entry in files:
        if not isinstance(entry, Mapping):
            continue
        path = _entry_text(entry, "filename")
        if not path:
            continue
        if not str(entry.get("patch") or "").strip():
            # GitHub 对二进制文件、以及改动过大的文件**直接不返回 patch 字段**。
            # 它和「文件被删了」不是一回事：那里是有 hunk 的。
            skipped[path] = "GitHub 未提供补丁（二进制文件，或改动过大被省略）"
            continue
        blocks.append(_file_block(entry))

    if not blocks:
        return [], skipped

    parsed = parse_unified_diff("\n".join(blocks), max_patch_chars=max_patch_chars)
    # 解析器自己的跳过原因（二进制标记、没有 hunk）合并进来。两边的键都是路径，
    # 不会互相覆盖 —— 能走到解析器的文件，上面已经确认有 patch 了。
    skipped.update(parsed.skipped)
    return parsed.patches, skipped
