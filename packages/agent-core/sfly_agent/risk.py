"""文件风险排序 —— ``plan`` 节点决定「先看哪些文件」的依据。

``PR_MAX_FILES`` 默认 40，而真实 PR 经常超过。截断必须**有依据**：按 diff 出现
顺序取前 40 意味着一个改了 200 个文件的 PR 恰好跳过哪些文件，取决于 git 的
路径排序 —— 也就是说，最危险的那几个很可能被跳过，而**没有任何东西会提示这件事**。

这是一个**确定性启发式**，不调 LLM：同样的输入永远得到同样的顺序，
于是评测里「换一个排序函数，精确率有没有变」是一个可测量的实验。

排序原则（按权重从高到低）：

1. **路径关键词**：``auth`` / ``crypto`` / ``secret`` / ``sql`` / ``exec`` 这类
   词出现在路径里，说明这个文件的职责本身就在信任边界上。
2. **变更行数**：改得多的更值得看。但有上限 —— 一个 5000 行的自动生成文件
   不该因为行数压过所有业务代码。
3. **测试与文档降权**：它们不是不审，而是同一个 PR 里，
   先看 ``payment.py`` 再看 ``test_payment.py`` 几乎没有争议。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import PurePosixPath
from typing import NamedTuple

from sfly_shared.contracts import FilePatch, normalize_path

#: ``(关键词, 权重)``。权重是相对的，不是绝对分数 —— 见 :func:`risk_score`。
#:
#: **匹配用的是子串，不是整词。** 子串匹配会多认一些（``nosql_store.py`` 命中
#: ``sql``），但在一个**排序**函数里，误报的代价只是顺序，漏报的代价是一个危险
#: 文件被截断掉 —— 两者不对称，所以往宽的方向偏。代价是短词要慎用：
#: ``acl`` 会命中 ``oracle.py``，所以这里写 ``access`` 而不是 ``acl``。
_PATH_HINTS: tuple[tuple[str, int], ...] = (
    ("auth", 5),
    ("login", 5),
    ("password", 5),
    ("passwd", 5),
    ("secret", 5),
    ("credential", 5),
    ("oauth", 5),
    ("jwt", 5),
    ("csrf", 4),
    ("token", 4),
    ("crypto", 4),
    ("cipher", 4),
    ("session", 4),
    ("permission", 4),
    ("access", 3),
    ("admin", 3),
    ("exec", 4),
    ("shell", 3),
    ("sql", 3),
    ("query", 2),
    ("upload", 3),
    ("serial", 3),
    ("deserial", 3),
    ("ssrf", 3),
    ("template", 2),
    ("config", 2),
    ("settings", 2),
    ("request", 1),
    ("migration", 1),
)

#: 命中这些目录段就降权。**不是不审**，是排在后面。
_LOW_PRIORITY_SEGMENTS = ("test", "tests", "spec", "specs", "__tests__", "fixtures", "vendor", "node_modules")
#: 文档后缀同样降权。
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")

#: 变更行数的封顶。见模块文档第 2 条：没有它，一个生成出来的大文件会压过一切。
_CHANGED_LINES_CAP = 200
#: 变更行数的除数。``min(变更行, 200) // 4`` → 最多贡献 50 分，
#: 与最强的关键词命中（5 × 10 = 50）同量级 —— 这是刻意的。
_CHANGED_LINES_DIVISOR = 4
#: 每个关键词权重乘这个数，让「命中一个强关键词」压过「改了 100 行」。
_HINT_MULTIPLIER = 10
#: 降权幅度。要大到让 test_payment.py 排到 payment.py 之后，但不能大到
#: 让一个**只改了测试**的 PR 拿不到任何审查（那时它是唯一的内容）。
_DEMOTION = 40


class RankedFile(NamedTuple):
    """一个文件以及它为什么排在这里。``reason`` 会进日志，便于解释一次截断。"""

    patch: FilePatch
    score: int
    reason: str


def risk_score(path: str, changed_lines: int) -> tuple[int, str]:
    """``(分数, 原因)``。分数只用来排序，绝对值没有意义。

    同分的文件按路径排序（见 :func:`rank_files`），所以结果是**完全确定**的：
    输入相同 → 顺序相同 → 截断相同。这一点比分数本身重要得多 ——
    一个会抖动的排序会让「这次为什么少审了那个文件」变成一个无法复现的问题。
    """
    normalized = normalize_path(path)

    score = 0
    hits: list[str] = []
    for hint, weight in _PATH_HINTS:
        if hint in normalized:
            score += weight * _HINT_MULTIPLIER
            hits.append(hint)

    reasons: list[str] = []
    if hits:
        reasons.append("路径命中 " + "/".join(hits))

    lines = min(changed_lines, _CHANGED_LINES_CAP)
    score += lines // _CHANGED_LINES_DIVISOR
    reasons.append(f"{changed_lines} 个变更行")

    parts = PurePosixPath(normalized).parts
    if any(segment in _LOW_PRIORITY_SEGMENTS for segment in parts[:-1]):
        score -= _DEMOTION
        reasons.append("测试/fixture 目录，降权")
    if normalized.endswith(_DOC_SUFFIXES):
        score -= _DEMOTION
        reasons.append("文档，降权")

    return score, "；".join(reasons)


def rank_files(patches: Iterable[FilePatch]) -> list[RankedFile]:
    """按风险从高到低排序。**同分按路径**，这是确定性的来源。

    ``reverse=True`` 配 ``key=(score, -path)`` 不好写，所以直接排正序再反过来 ——
    但这会让同分的文件顺序被反转，于是路径排序也反了。做法是排 ``(-score, path)``
    然后升序，这样「分数高的在前、同分按路径升序」两个条件同时成立。
    """
    ranked = [RankedFile(patch, *_score_of(patch)) for patch in patches]
    ranked.sort(key=lambda r: (-r.score, normalize_path(r.patch.path)))
    return ranked


def _score_of(patch: FilePatch) -> tuple[int, str]:
    changed = len(patch.changed_lines) or (patch.additions + patch.deletions)
    return risk_score(patch.path, changed)


def select_files(patches: Sequence[FilePatch], limit: int) -> tuple[list[FilePatch], bool]:
    """排序后取前 ``limit`` 个。返回 ``(选中的, 是否截断过)``。

    ``limit <= 0`` 表示不限制 —— 和 ``WorkerRunner`` 的独立 CLI 模式同一个约定，
    那里也把「上限」当可选参数。
    """
    ranked = rank_files(patches)
    if limit <= 0 or len(ranked) <= limit:
        return [r.patch for r in ranked], False
    return [r.patch for r in ranked[:limit]], True
