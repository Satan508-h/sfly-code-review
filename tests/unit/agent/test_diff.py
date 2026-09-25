"""unified diff 解析的测试。

这个文件里的用例大多来自**实际踩到的坑**，而不是格式规范的边界。
原因：解析器错了之后不会抛异常，它只是安静地少给你几行 ——
下游看到的是「模型没发现问题」，于是你会去调提示词，而 bug 在解析器里。
所以每个曾经错过的形态都在这里留一条。

``fixtures/security_demo.diff`` 是 ``git diff`` 的真实输出，
下面有一组用例把它的行号逐条钉死。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sfly_agent.diff import (
    TRUNCATION_MARKER,
    detect_language,
    iter_added_lines,
    iter_diff_lines,
    parse_unified_diff,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"

# --------------------------------------------------------------------------- #
# 行号必须精确
# --------------------------------------------------------------------------- #


def _one_file(body: str, header: str = "@@ -1,2 +1,3 @@") -> str:
    return f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n{header}\n{body}"


@pytest.mark.unit
def test_single_line_hunk_header_defaults_counts_to_one() -> None:
    """``@@ -1 +1 @@`` 省略了行数，按规范默认是 1，不是 0。"""
    text = _one_file("-old\n+new\n", header="@@ -1 +1 @@")
    assert [a.line for a in iter_added_lines(text)] == [1]


@pytest.mark.unit
def test_hunk_start_is_taken_from_the_header_not_counted_up() -> None:
    """新文件的起始行号来自 ``+N``，不是从上一个 hunk 接着数。"""
    text = _one_file(" x\n+y\n z\n w\n", header="@@ -1,4 +10,5 @@")
    assert [a.line for a in iter_added_lines(text)] == [11]


@pytest.mark.unit
def test_multi_hunk_line_numbers_are_exact() -> None:
    """**回归测试**：hunk 的行数必须新旧两侧分开计。

    这里踩过一次坑：一开始把 ``old_count + new_count`` 当作 hunk 的总行数，
    而它比实际多算了「上下文行数」那么多行（上下文行在新旧两侧各算一次，
    但在文本里只出现一次）。后果是每个 hunk 都会多吃掉下一个 hunk 的头部 ——
    表现是**中后段的新增行整段消失，而解析器一句话都不报**。
    """
    text = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1,3 +1,4 @@\n"
        " import os\n"  # 新 1
        " \n"  # 新 2（上下文空行）
        "+import sys\n"  # 新 3
        " def main():\n"  # 新 4
        "@@ -10,3 +11,5 @@\n"
        "     x = 1\n"  # 新 11
        "+    y = 2\n"  # 新 12
        "+    z = 3\n"  # 新 13
        "     return x\n"  # 新 14
    )
    got = [(a.line, a.content) for a in iter_added_lines(text)]
    assert got == [(3, "import sys"), (12, "    y = 2"), (13, "    z = 3")]


@pytest.mark.unit
def test_context_lines_are_available_too() -> None:
    """上下文行必须能被拿到。

    判断「循环里在做查询」需要那行 ``for``，而在真实 PR 里 ``for`` 通常
    **原本就在**，这次新增的只有循环体 —— 只看新增行的话，
    最典型的那类 N+1 恰好永远检测不到。
    """
    text = _one_file(" a\n+b\n c\n", header="@@ -1,2 +1,3 @@")
    got = [(x.marker, x.line, x.content) for x in iter_diff_lines(text)]
    assert got == [(" ", 1, "a"), ("+", 2, "b"), (" ", 3, "c")]


@pytest.mark.unit
def test_removed_lines_report_line_zero() -> None:
    """删除的行在新文件里没有位置，报 0 —— 消费方必须显式处理它。

    把它当成一个合法行号（比如沿用一个旧值）会让「变更行校验」失效，
    于是发布阶段会拿着一个不存在的行号去请求 GitHub 的评论接口。
    """
    text = _one_file(" a\n-b\n+c\n", header="@@ -1,2 +1,2 @@")
    removed = [x for x in iter_diff_lines(text) if x.is_removed]
    assert removed and all(x.line == 0 for x in removed)


# --------------------------------------------------------------------------- #
# 真实 git 输出
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_real_fixture_line_numbers_are_exact() -> None:
    """把 fixture 里的关键行逐条钉死。

    这组断言是「解析器可信」的全部依据。行号错位不会让任何东西报错，
    但会让每一条 finding 指向错误的代码 —— 而 PR 评论里挂着错行的代码，
    比没有评论更糟。
    """
    text = (FIXTURES / "security_demo.diff").read_text(encoding="utf-8")
    got = {(a.path, a.line): a.content for a in iter_added_lines(text)}

    assert got[("app/db.py", 8)] == 'API_KEY = "sk-live-9f8a7b6c5d4e3f2a1b0c7d8e"'
    assert got[("app/db.py", 17)].strip().startswith("cur.execute(f")
    assert got[("app/db.py", 28)].strip().startswith("return hashlib.md5")
    assert got[("app/db.py", 32)].strip() == "return pickle.loads(blob)"
    assert got[("app/db.py", 36)].strip().startswith("return random.randint")
    assert got[("app/db.py", 49)].strip() == 'print("saved", key)'
    assert got[("app/api.py", 32)].strip().startswith("subprocess.run(f")
    assert got[("app/api.py", 39)].strip().startswith("resp = requests.get(url")
    assert got[("static/app.js", 3)].strip() == "el.innerHTML = content"


@pytest.mark.unit
def test_prefixes_are_stripped_from_paths() -> None:
    """``b/app/db.py`` 必须变成 ``app/db.py``。

    不剥前缀的话流程照跑（归一化之后能对上），但那个带前缀的字符串会一路
    进数据库、进 API 响应、进前端 —— 直到 M7 拿它去请求 GitHub 的评论接口
    才炸，而那时离引入这个 bug 已经很远了。
    """
    text = (FIXTURES / "security_demo.diff").read_text(encoding="utf-8")
    result = parse_unified_diff(text)
    assert [p.path for p in result.patches] == [
        "app/api.py",
        "app/db.py",
        "app/report.py",
        "static/app.js",
    ]
    assert not any(p.path.startswith(("a/", "b/")) for p in result.patches)


@pytest.mark.unit
def test_fixture_additions_and_deletions_are_counted() -> None:
    text = (FIXTURES / "security_demo.diff").read_text(encoding="utf-8")
    by_path = {p.path: p for p in parse_unified_diff(text).patches}
    assert (by_path["app/db.py"].additions, by_path["app/db.py"].deletions) == (24, 1)
    assert (by_path["static/app.js"].additions, by_path["static/app.js"].deletions) == (4, 0)
    assert by_path["static/app.js"].is_new_file is True


# --------------------------------------------------------------------------- #
# 文件头部的各种形态
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_content_line_looking_like_a_file_header_does_not_split_the_file() -> None:
    """hunk 内部一行内容 ``+++ b/fake.py`` 与文件头长得一模一样。

    靠行首字符判断的实现会把它当成下一个文件，从此整份 diff 的路径全错。
    正确做法是按 hunk 头部声明的行数消费，而不是猜。
    """
    text = _one_file(" x = 1\n+++ b/fake.py\n y = 2\n", header="@@ -1,2 +1,3 @@")
    result = parse_unified_diff(text)
    assert [p.path for p in result.patches] == ["a.py"]
    assert result.patches[0].changed_lines == [2]
    assert "+++ b/fake.py" in result.patches[0].patch


@pytest.mark.unit
def test_new_file_is_flagged() -> None:
    text = (
        "diff --git a/new.py b/new.py\n"
        "new file mode 100644\n"
        "index 0000000..abc1234\n"
        "--- /dev/null\n"
        "+++ b/new.py\n"
        "@@ -0,0 +1,2 @@\n"
        "+import os\n"
        "+print(os.name)\n"
    )
    patch = parse_unified_diff(text).patches[0]
    assert patch.path == "new.py"
    assert patch.is_new_file is True
    assert patch.changed_lines == [1, 2]


@pytest.mark.unit
def test_deleted_file_keeps_its_path_and_has_no_changed_lines() -> None:
    """删除的文件**不能从结果里消失**。

    它没有可挂评论的行，但它必须仍然算一个文件 —— 否则 ``files_total``
    会少一个，而没人会发现少了一个。
    """
    text = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-import os\n"
        "-x = 1\n"
    )
    result = parse_unified_diff(text)
    assert result.total_files == 1
    assert result.patches[0].path == "gone.py"
    assert result.patches[0].is_deleted_file is True
    assert result.patches[0].changed_lines == []


@pytest.mark.unit
def test_binary_files_are_skipped_with_a_reason() -> None:
    text = (
        "diff --git a/logo.png b/logo.png\n"
        "index 1111111..2222222 100644\n"
        "Binary files a/logo.png and b/logo.png differ\n"
    )
    result = parse_unified_diff(text)
    assert result.patches == []
    assert "logo.png" in result.skipped


@pytest.mark.unit
def test_preamble_before_the_first_diff_header_is_dropped() -> None:
    """``git show`` 的 commit 头、手写的说明文字都不是 diff，喂给模型只会诱发幻觉。"""
    text = (
        "commit 1a2b3c4d5e6f\n"
        "Author: someone <a@b.c>\n"
        "Date:   Mon Sep 1 10:00:00 2025 +0800\n"
        "\n"
        "    修复登录问题\n"
        "\n" + _one_file(" a\n+b\n c\n", header="@@ -1,2 +1,3 @@")
    )
    result = parse_unified_diff(text)
    assert [p.path for p in result.patches] == ["a.py"]


@pytest.mark.unit
def test_quoted_paths_with_spaces() -> None:
    text = (
        'diff --git "a/my file.py" "b/my file.py"\n'
        '--- "a/my file.py"\n'
        '+++ "b/my file.py"\n'
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    assert parse_unified_diff(text).patches[0].path == "my file.py"


@pytest.mark.unit
def test_no_newline_marker_does_not_consume_a_line() -> None:
    text = _one_file("-a\n+b\n\\ No newline at end of file\n", header="@@ -1 +1 @@")
    assert [a.line for a in iter_added_lines(text)] == [1]


# --------------------------------------------------------------------------- #
# 明确区分「没有 diff」和「没有问题」
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "   \n\n", "这是一段普通的文字，不是 diff。", '{"a": 1}'])
def test_text_without_any_diff_is_empty(text: str) -> None:
    """输入不是 diff 时必须能被识别出来。

    把这种情况当成「审了但没问题」是最坏的一种误导：用户以为审查通过了，
    实际上面向的是空气。CLI 因此在这里返回**输入错误**而不是成功。
    """
    result = parse_unified_diff(text)
    assert result.is_empty
    assert result.total_files == 0


@pytest.mark.unit
def test_result_exposes_both_counts_so_callers_can_tell_them_apart() -> None:
    text = "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
    result = parse_unified_diff(text)
    assert result.total_files == 1
    assert result.patches == []


# --------------------------------------------------------------------------- #
# 截断
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_truncation_limits_changed_lines_to_what_is_actually_visible() -> None:
    """截断后 ``changed_lines`` 必须**重新算**。

    直接沿用截断前的集合，会让发布阶段以为某个行号在补丁里，
    而模型根本没看到那一行 —— 于是它会为一段自己没读过的代码写评论。
    """
    body = "".join(f"+added_line_number_{i:02d}_" + "x" * 40 + "\n" for i in range(2, 20))
    text = _one_file(" line1\n" + body + " line2\n", header="@@ -1,2 +1,20 @@")

    full = parse_unified_diff(text)
    assert full.patches[0].truncated is False
    assert len(full.patches[0].changed_lines) == 18

    cut = parse_unified_diff(text, max_patch_chars=200)
    patch = cut.patches[0]
    assert patch.truncated is True
    assert TRUNCATION_MARKER in patch.patch
    assert 0 < len(patch.changed_lines) < 18
    # 留下来的行号必须是前几个 —— 也就是模型真正能看到的那些
    assert patch.changed_lines == list(range(2, 2 + len(patch.changed_lines)))


@pytest.mark.unit
def test_truncation_marker_is_not_mistaken_for_code() -> None:
    """截断标记以 ``.`` 开头，**绝不能**以 ``+`` ``-`` 或空格开头。

    否则它会被当成一条新增行：既污染 ``changed_lines``，
    又会被当成代码送给模型。
    """
    assert not TRUNCATION_MARKER.startswith(("+", "-", " "))
    body = "".join(f"+line_{i:02d}_" + "y" * 60 + "\n" for i in range(2, 30))
    text = _one_file(body, header="@@ -1,1 +1,30 @@")
    patch = parse_unified_diff(text, max_patch_chars=150).patches[0]
    assert all("截断" not in c for c in [a.content for a in iter_added_lines(patch.patch)])


# --------------------------------------------------------------------------- #
# 语言识别
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("app/db.py", "python"),
        ("src/main.ts", "typescript"),
        ("ui/Card.vue", "vue"),
        ("Dockerfile", "docker"),
        ("infra/Makefile", "make"),
        ("config/app.yaml", "yaml"),
        ("unknownfile", "unknown"),
        ("a/b/c.unknownext", "unknown"),
    ],
)
def test_language_detection(path: str, expected: str) -> None:
    """猜不出来返回 ``"unknown"`` 而不是空串 —— 空串在日志和 UI 里都是一个洞。"""
    assert detect_language(path) == expected
