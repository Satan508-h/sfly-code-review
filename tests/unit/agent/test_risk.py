"""文件风险排序 —— 纯函数，毫秒级。

这个文件最重要的是**确定性**那两条：截断掉哪些文件必须可复现。
一个会抖动的排序会让「这次为什么少审了那个文件」变成一个无法复现的问题，
而那种问题在一次面试里被追问两句就散了。
"""

from __future__ import annotations

import pytest

from sfly_agent.risk import rank_files, risk_score, select_files
from sfly_shared.contracts import FilePatch

pytestmark = pytest.mark.unit


def _patch(path: str, lines: int = 5, **over: object) -> FilePatch:
    return FilePatch.model_validate(
        {
            "path": path,
            "patch": "@@ -1 +1 @@\n-a\n+b\n",
            "changed_lines": list(range(1, lines + 1)),
            **over,
        }
    )


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #


def test_a_sensitive_path_outranks_a_larger_neutral_one() -> None:
    """**这是整个模块存在的理由**：一个改了 150 行的普通文件，
    不该压过一个改了 10 行的 ``auth.py``。"""
    neutral, _ = risk_score("src/utils/helpers.py", 150)
    sensitive, _ = risk_score("src/auth/session.py", 10)

    assert sensitive > neutral


@pytest.mark.parametrize(
    "path",
    ["app/auth.py", "app/oauth_client.py", "db/sql_store.py", "app/crypto.py", "app/secrets.yaml"],
)
def test_the_keywords_actually_match(path: str) -> None:
    """子串匹配（不是整词）：``oauth_client.py`` 要能命中 ``auth``。

    实测：整词匹配会把 ``oauth_client.py`` 漏掉，而它显然是安全相关的。
    代价是多认一些（``nosql_store.py`` 命中 ``sql``）—— 在一个排序函数里，
    误报的代价只是顺序，漏报的代价是一个危险文件被截断掉。
    """
    score, reason = risk_score(path, 5)
    assert "路径命中" in reason, f"{path} 没有命中任何关键词（score={score}）"


def test_short_keywords_that_collide_are_not_in_the_table() -> None:
    """``acl`` 会命中 ``oracle.py``，所以表里写的是 ``access``。

    这条测试是给未来的人看的：往关键词表里加短词之前先想想它会不会
    出现在一个完全无关的文件名里。
    """
    _, reason = risk_score("app/oracle_client.py", 5)
    assert reason == "5 个变更行", "oracle 不该命中任何安全关键词"


def test_test_files_are_demoted_but_not_excluded() -> None:
    """降权不是排除：一个只改了测试的 PR，测试文件仍然是唯一的审查内容。"""
    test_score, test_reason = risk_score("tests/test_payment.py", 20)
    src_score, _ = risk_score("payment.py", 20)

    assert test_score < src_score
    assert "降权" in test_reason


def test_docs_are_demoted() -> None:
    doc, reason = risk_score("docs/api.md", 20)
    src, _ = risk_score("api.py", 20)
    assert doc < src
    assert "文档" in reason


def test_a_huge_generated_file_does_not_dominate() -> None:
    """变更行数有封顶（200）。没有它，一个自动生成的大文件会压过一切 ——
    而那种文件恰恰是最不值得看的那类。"""
    huge, _ = risk_score("src/generated.py", 50_000)
    big, _ = risk_score("src/generated.py", 200)
    assert huge == big


# --------------------------------------------------------------------------- #
# 排序与截断
# --------------------------------------------------------------------------- #


def test_ranking_is_deterministic_for_equal_scores() -> None:
    """同分按路径升序。**这是「截断可复现」的全部机制。**"""
    patches = [_patch(f"src/m{i}.py", 10) for i in range(5)]
    first = [r.patch.path for r in rank_files(patches)]
    second = [r.patch.path for r in rank_files(list(reversed(patches)))]

    assert first == second == sorted(first)


def test_select_files_reports_whether_it_truncated() -> None:
    patches = [_patch(f"src/m{i}.py") for i in range(10)]

    kept, truncated = select_files(patches, 4)
    assert len(kept) == 4
    assert truncated is True

    kept_all, not_truncated = select_files(patches, 10)
    assert len(kept_all) == 10
    assert not_truncated is False


def test_the_limit_keeps_the_riskiest_files() -> None:
    """截断必须留下**最危险**的那些，而不是 diff 里最靠前的那些。

    按 diff 顺序取前 N 意味着「一个改了 200 个文件的 PR 恰好跳过哪些文件」
    取决于 git 的路径排序 —— 也就是最危险的那几个很可能被跳过，
    而**没有任何东西会提示这件事**。
    """
    patches = [_patch(f"src/mod{i}.py", 10) for i in range(10)]
    patches.append(_patch("src/auth/token.py", 3))

    kept, truncated = select_files(patches, 3)
    assert truncated is True
    assert "src/auth/token.py" in {p.path for p in kept}


def test_a_non_positive_limit_means_no_limit() -> None:
    patches = [_patch(f"src/m{i}.py") for i in range(10)]
    kept, truncated = select_files(patches, 0)

    assert len(kept) == 10
    assert truncated is False
