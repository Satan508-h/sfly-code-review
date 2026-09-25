"""Unified diff 解析 —— 把一份 patch 变成 ``FilePatch`` 列表。

### 它为什么在 ``sfly_shared`` 而不在 ``sfly_agent``

**因为它有四个消费方，而其中一个不该为此拖进整个 LLM 栈。**

* API 网关（M6）：把 webhook 载荷里的 patch 变成结构化文件列表
* 编排层的 ``plan`` 节点：按风险排序、截断
* Worker 与它的 ``--diff`` CLI：审之前要知道哪些行是变更行
* Mock LLM：它得真的「读」diff，才知道某段代码落在新文件的第几行

它原本住在 ``sfly_agent.diff``，直到 M6 在**容器里**才暴露：网关 import 它会
``ModuleNotFoundError: No module named 'sfly_agent'``（api 的依赖里没有
agent-core，也不该有 —— 那是 LLM/RAG/聚合的包）。修法有两条：给网关加上
agent-core 依赖（它的镜像会因此多出 rapidfuzz / rank_bm25 / LLM 客户端，
而网关一行都用不到），或者把这个模块搬到两边都能 import 的地方。
第二条是它对的位置 —— 它只依赖 ``contracts.FilePatch`` 和标准库。

> 本地跑得好好的、容器里才炸，是因为开发机上的 venv 装了**全部** workspace 包
> （``uv sync --all-packages``），于是「网关能不能 import 到 agent-core」
> 这个约束在本地根本不存在。这正是容器验收不能省的原因。

**``changed_lines`` 是整个项目的地基。** GitHub 会 422 拒绝锚定在未变更行上的
inline 评论，所以「这条 finding 的行号是否落在变更行上」必须在 Worker 侧就算清楚 ——
拖到发布时才发现的话，整条 finding 只能降级成文件级评论，而那时已经没法补救了。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field

from sfly_shared.contracts import FilePatch

#: 单个文件的补丁上限。超过就截断，并在补丁里留一行可见的标记 ——
#: LLM 看到标记才知道自己拿到的是不完整的文件，避免它对「看不到的部分」下结论。
DEFAULT_MAX_PATCH_CHARS = 8_000

#: 截断标记。**必须以非 diff 字符开头** —— 否则会被当成新增行，
#: 既污染 changed_lines 又会被送进模型当成代码。
TRUNCATION_MARKER = "... [补丁过长，此处已截断；后续行未参与审查] ..."

#: ``@@ -1,7 +1,9 @@`` —— 旧起点、旧行数、新起点、新行数。行数省略时默认 1。
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

#: ``diff --git a/src/a.py b/src/a.py``。路径含空格时 git 会加引号，
#: 那种情况走 ``+++`` 头部兜底，所以这里只做贪婪匹配。
_DIFF_GIT_RE = re.compile(r'^diff --git (?:"?a/)?(.+?)"? (?:"?b/)?(.+?)"?$')

#: ``+++ b/src/a.py`` / ``+++ /dev/null``。非 git 的 diff 会在路径后跟一个
#: 制表符和修改时间，要一并切掉。
_PLUS_HEADER_RE = re.compile(r"^\+\+\+ (.+)$")

#: ``--- a/src/a.py``
_MINUS_HEADER_RE = re.compile(r"^--- (.+)$")

_BINARY_MARKER = "Binary files"

_LANG_BY_NAME: dict[str, str] = {
    "dockerfile": "docker",
    "makefile": "make",
    "gemfile": "ruby",
    "rakefile": "ruby",
    "procfile": "unknown",
    ".gitignore": "unknown",
    ".env": "dotenv",
}

_LANG_BY_EXT: dict[str, str] = {
    "py": "python",
    "pyi": "python",
    "js": "javascript",
    "jsx": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "vue": "vue",
    "java": "java",
    "kt": "kotlin",
    "go": "go",
    "rs": "rust",
    "rb": "ruby",
    "php": "php",
    "cs": "csharp",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "hpp": "cpp",
    "swift": "swift",
    "scala": "scala",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "ps1": "powershell",
    "sql": "sql",
    "yml": "yaml",
    "yaml": "yaml",
    "json": "json",
    "toml": "toml",
    "ini": "ini",
    "cfg": "ini",
    "xml": "xml",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "md": "markdown",
    "rst": "rst",
    "tf": "terraform",
    "proto": "protobuf",
    "gradle": "gradle",
}


def detect_language(path: str) -> str:
    """从文件名猜语言。只用于提示词里的标注和规则的语言过滤，猜错不致命。

    猜不出来返回 ``"unknown"`` 而不是空串 —— 空串在日志和 UI 里都是一个洞。
    """
    name = path.rsplit("/", 1)[-1].lower()
    if name in _LANG_BY_NAME:
        return _LANG_BY_NAME[name]
    if name.startswith("dockerfile"):
        return "docker"
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    return _LANG_BY_EXT.get(ext, "unknown")


def _unquote(raw: str) -> str:
    """去掉 git 附加的时间戳和引号。"""
    path = raw.split("\t", 1)[0].strip()
    # 路径含空格/非 ASCII 时 git 会输出带引号的字符串
    if len(path) >= 2 and path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    return path


def _header_path(raw: str) -> str:
    """``+++ b/src/a.py`` / ``--- a/src/a.py`` → ``src/a.py``。

    **``a/`` 和 ``b/`` 前缀必须在这里剥掉。** 不剥的话，finding 里的路径会是
    ``b/app/db.py`` —— 归一化之后能对上，所以流程照常跑，但**它是错的**：
    这个字符串会进数据库、进 API 响应、进前端。到了 M7 拿它去请求 GitHub 的
    评论接口时才会炸，而那时离引入这个 bug 已经很远了。
    """
    path = _unquote(raw)
    for prefix in ("a/", "b/"):
        if path.startswith(prefix):
            return path[2:]
    return path


def _git_header_path(raw: str) -> str:
    """``diff --git a/x b/x`` 的第二个路径。

    **不能再剥一次前缀** —— ``_DIFF_GIT_RE`` 的可选分组已经把 ``a/`` ``b/``
    吃掉了。文件名本身就叫 ``a/...`` 时（仓库里真有一个叫 ``a`` 的目录），
    多剥一次会把 ``a/x`` 变成 ``x``，指向一个不存在的路径。
    """
    return _unquote(raw)


@dataclass(frozen=True, slots=True)
class DiffLine:
    """diff 里的一行，带它在**新文件**中的行号。

    ``marker`` 为 ``"-"`` 时 ``line`` 是 0 —— 被删除的行在新文件里没有位置。
    消费方必须显式处理这种情况（一般是跳过），而不是把 0 当成一个合法行号。
    """

    path: str
    line: int
    content: str
    marker: str = "+"

    @property
    def is_added(self) -> bool:
        return self.marker == "+"

    @property
    def is_removed(self) -> bool:
        return self.marker == "-"


@dataclass(slots=True)
class DiffParseResult:
    """解析结果。

    ``total_files`` 与 ``len(patches)`` 分开是为了区分「本来就没几个文件」和
    「文件都被跳过了」—— 前者正常，后者说明调用方喂进来的东西不是 diff。
    """

    patches: list[FilePatch] = field(default_factory=list)
    #: 解析出的文件总数（含被跳过的）
    total_files: int = 0
    #: 被跳过的文件及原因。二进制文件是最常见的一种，它没有补丁可审。
    skipped: dict[str, str] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        """一个可审的文件都没有。

        调用方**必须**把它和「审了但没发现问题」区分开：前者是输入错了，
        后者是审查结论。混淆这两者会得到一句致命的误导 ——
        「未发现问题」。
        """
        return not self.patches


def _split_file_blocks(text: str) -> list[list[str]]:
    """按文件切块。

    ``diff --git`` 之前的内容（``git show`` 的 commit message、``git format-patch``
    的信封头、手写 diff 前面的说明文字）全部丢弃 —— 它们不是 diff，
    喂给模型只会浪费 token 并诱发幻觉。
    """
    blocks: list[list[str]] = []
    cur: list[str] | None = None

    for raw in text.splitlines():
        if raw.startswith("diff --git "):
            if cur is not None:
                blocks.append(cur)
            cur = [raw]
        elif cur is not None:
            cur.append(raw)
        elif raw.startswith("--- "):
            # 没有 diff --git 头的裸 unified diff
            cur = [raw]

    if cur is not None:
        blocks.append(cur)
    return blocks


def _block_path(block: list[str]) -> tuple[str | None, bool, bool]:
    """从文件块里取出路径、是否新文件、是否删除。

    路径以 ``+++`` 为准而不是 ``diff --git``：前者是 git 真正用来定位新文件的
    依据，重命名时两者会不一致（``diff --git a/old.py b/new.py``，
    ``+++ b/new.py``）—— 以旧名字去发布评论会被 GitHub 拒绝。

    **只在第一个 ``@@`` 之前找头部。** 越过它去扫，会把 hunk 内部一行内容
    ``+++ b/fake.py`` 当成文件头 —— 那行与真头部逐字同形，靠内容本身
    无法区分，唯一的依据是「它出现在 hunks 开始之后」这个位置事实。
    """
    path: str | None = None
    is_new = False
    is_deleted = False

    for raw in block:
        if _HUNK_RE.match(raw) is not None:
            break  # 头部区结束，后面都是内容

        if raw.startswith("new file mode"):
            is_new = True
        elif raw.startswith("deleted file mode"):
            is_deleted = True
        elif (m := _PLUS_HEADER_RE.match(raw)) is not None:
            candidate = _header_path(m.group(1))
            if candidate == "/dev/null":
                # 删除。**不清空 path** —— 此时 path 已由上面的 diff --git
                # 头填好（git 在 b/ 位置写的仍是原文件名）。清空会让这个文件
                # 整个从解析结果里消失，`files_total` 少一个而没人会发现。
                is_deleted = True
            else:
                path = candidate
        elif path is None and (m2 := _DIFF_GIT_RE.match(raw)) is not None:
            path = _git_header_path(m2.group(2))

    return path, is_new, is_deleted


def _walk_hunks(block: list[str]) -> Iterator[tuple[int, str, str]]:
    """逐个吐出 ``(新文件行号, 标记, 内容)``，标记是 ``+`` / ``-`` / `` ``。

    **按 hunk 头部声明的行数消费，而不是看行首字符判断**。原因是真实存在的
    歧义：位于 hunk 内部的一行内容 ``+++ b/x`` 与文件头部长得一模一样，
    靠行首字符判断会把它当成下一个文件的开始。

    **新旧两侧的行数必须分开计数。** 这里踩过一次坑：一开始用
    ``old_count + new_count`` 当作 hunk 的总行数，而它比实际多算了
    「上下文行数」那么多行（上下文行在新旧两侧各算一次，但在文本里只出现一次）。
    后果是每个 hunk 都会多吃掉下一个 hunk 的头部和开头几行 ——
    表现是**行号错位、且中后段的新增行整段消失**，而解析器一句话都不报。
    这正是那种「看起来像模型没报问题」的静默故障，所以宁可多写十行也要计数精确。
    """
    new_line = 0
    old_left = 0
    new_left = 0

    for raw in block:
        if old_left > 0 or new_left > 0:
            # `\ No newline at end of file` 不占任何一侧的行数
            if raw.startswith("\\"):
                continue
            if raw.startswith("+"):
                new_left -= 1
                yield new_line, "+", raw[1:]
                new_line += 1
            elif raw.startswith("-"):
                old_left -= 1
                yield 0, "-", raw[1:]
            else:
                # 上下文行：新旧两侧各消耗一行。
                # 空行在有些工具的输出里会被吃掉行首那个空格，同样按上下文算。
                old_left -= 1
                new_left -= 1
                yield new_line, " ", raw[1:] if raw.startswith(" ") else raw
                new_line += 1
            continue

        m = _HUNK_RE.match(raw)
        if m is None:
            continue
        new_line = int(m.group(3))
        old_left = int(m.group(2)) if m.group(2) is not None else 1
        new_left = int(m.group(4)) if m.group(4) is not None else 1


def iter_diff_lines(text: str) -> Iterator[DiffLine]:
    """遍历文本里 diff 的每一行（新增 / 删除 / 上下文）。

    **上下文行不能丢。** 一个反直觉但很常见的事实：判断「循环里在做查询」
    需要那行 ``for`` —— 而在一个真实的 PR 里，``for`` 往往**原本就在**，
    这次新增的只是循环体。只看新增行的话，最典型的那类 N+1 恰好永远检测不到。
    """
    for block in _split_file_blocks(text):
        path, _is_new, _is_deleted = _block_path(block)
        if path is None:
            continue
        for line, marker, content in _walk_hunks(block):
            yield DiffLine(path=path, line=line, content=content, marker=marker)


def iter_added_lines(text: str) -> Iterator[DiffLine]:
    """只遍历新增行。给 Mock LLM 用 —— 它靠这个才能报出落在变更行上的行号。"""
    for item in iter_diff_lines(text):
        if item.is_added:
            yield item


def _truncate_patch(patch: str, limit: int) -> tuple[str, set[int]]:
    """按**行边界**截断补丁，返回截断后的文本和其中可见的新增行号。

    行边界而不是字符边界：截在半个 UTF-8 字符或半行 JSON 中间会让模型
    收到一段看起来像乱码的代码，比干脆截断更糟。

    返回的可见行号是**截断之后**重新扫出来的 —— 直接复用截断前的集合会让
    发布阶段以为某个行号在补丁里，而模型根本没看到那一行。
    """
    if len(patch) <= limit:
        return patch, {a.line for a in iter_added_lines(patch)}

    kept: list[str] = []
    size = 0
    for raw in patch.splitlines():
        if size + len(raw) + 1 > limit:
            break
        kept.append(raw)
        size += len(raw) + 1

    truncated = "\n".join([*kept, TRUNCATION_MARKER])
    return truncated, {a.line for a in iter_added_lines(truncated)}


def parse_unified_diff(text: str, *, max_patch_chars: int = DEFAULT_MAX_PATCH_CHARS) -> DiffParseResult:
    """解析一份 unified diff。

    不做文件数量上限 —— 「按风险排序后取前 N」是编排层 ``plan`` 节点的决策，
    把截断混进解析里会让「解析出 12 个文件」和「最终审了 8 个」变成同一件事，
    而评测需要分开统计这两个数。
    """
    result = DiffParseResult()

    for block in _split_file_blocks(text):
        path, is_new, is_deleted = _block_path(block)
        if path is None:
            continue

        result.total_files += 1

        if any(_BINARY_MARKER in raw for raw in block):
            result.skipped[path] = "二进制文件，无补丁"
            continue

        # 保留从 diff --git 起的全部原始行：模型需要 hunk 头部才能定位行号，
        # 也需要上下文行才能判断这段代码在做什么。
        patch = "\n".join(block)
        if not any(_HUNK_RE.match(raw) for raw in block):
            result.skipped[path] = "没有 hunk（纯模式变更或重命名）"
            continue

        patch, visible = _truncate_patch(patch, max_patch_chars)

        # 一次遍历同时数增删 —— 这两个数是给人看的，不该付两遍解析成本
        additions = deletions = 0
        for _line, marker, _content in _walk_hunks(block):
            if marker == "+":
                additions += 1
            elif marker == "-":
                deletions += 1

        result.patches.append(
            FilePatch(
                path=path,
                language=detect_language(path),
                patch=patch,
                additions=additions,
                deletions=deletions,
                changed_lines=sorted(visible),
                is_new_file=is_new,
                is_deleted_file=is_deleted,
                truncated=TRUNCATION_MARKER in patch,
            )
        )

    return result
