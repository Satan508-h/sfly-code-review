"""指纹的测试。

指纹是**去重快速路径的键**：相同 ⇒ 一定是同一条发现。所以两边都要测：

* 必须**相同**的（路径前缀、markdown 装饰、大小写、标点）
* 必须**不同**的（不同的类目、不同的措辞）

第二类更容易写错，也更贵：把两条不同的发现压成同一个指纹，等于其中一条
静默消失。所以下面「必须不同」的用例数量刻意多于「必须相同」的。
"""

from __future__ import annotations

import pytest

from sfly_agent.aggregate import fingerprint, line_bucket, normalize_message
from sfly_shared.contracts import Finding, Severity


def _f(**over: object) -> Finding:
    base: dict[str, object] = {
        "file": "app/db.py",
        "line": 12,
        "severity": Severity.HIGH,
        "category": "sqli",
        "message": "用 f-string 拼接 SQL，用户输入没有参数化",
    }
    base.update(over)
    return Finding.model_validate(base)


# --------------------------------------------------------------------------- #
# 稳定性
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_same_finding_gives_the_same_fingerprint() -> None:
    assert fingerprint(_f()) == fingerprint(_f())


@pytest.mark.unit
def test_fingerprint_is_a_sha1_hex_digest() -> None:
    value = fingerprint(_f())
    assert len(value) == 40
    assert all(c in "0123456789abcdef" for c in value)


@pytest.mark.unit
def test_fingerprint_uses_a_salted_free_hash() -> None:
    """**钉死一个具体值**，这是唯一能抓住「有人把 ``stable_hash`` 换成 ``hash()``」
    的方法。

    内置 ``hash()`` 对字符串加了随机盐，所以它在同一个进程里自洽、跨进程不一致 ——
    而 Worker 和 orchestrator 是两个进程。换错的后果是「同一个问题在两次运行里
    被判成两条」，不会有任何报错，只是去重率悄悄掉下去。

    这条值本身没有含义，它只是一把尺子。改动归一化逻辑时它**会**失败 ——
    那时请确认改动是有意的，然后更新这个常量。
    """
    assert fingerprint(_f()) == "5d1ac364eeab73fe16b730e2e9a5c7c0d5efb5af"


# --------------------------------------------------------------------------- #
# 路径：模型写出来的各种形态
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "path",
    [
        "app/db.py",
        "b/app/db.py",  # diff 的新侧前缀
        "a/app/db.py",  # 旧侧前缀
        "./app/db.py",
        "app\\db.py",  # Windows 风格
        "APP/DB.PY",
    ],
)
def test_path_spelling_does_not_change_the_fingerprint(path: str) -> None:
    """同一个文件的不同写法必须归到同一个指纹。

    不归一的话，同一条发现会因为「模型这次写了 b/ 前缀」而变成两条 ——
    于是 PR 上出现两条一样的评论。
    """
    assert fingerprint(_f(file=path)) == fingerprint(_f(file="app/db.py"))


@pytest.mark.unit
def test_different_files_give_different_fingerprints() -> None:
    assert fingerprint(_f(file="app/db.py")) != fingerprint(_f(file="app/api.py"))


# --------------------------------------------------------------------------- #
# 行号：分桶的粒度（以及它的边界）
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_lines_in_the_same_bucket_agree() -> None:
    assert fingerprint(_f(line=12)) == fingerprint(_f(line=14))


@pytest.mark.unit
def test_lines_across_a_bucket_boundary_differ() -> None:
    """**跨桶边界的相邻行会得到不同的指纹，这是刻意的。**

    分桶是 ``[0,1,2] [3,4,5] [6,7,8]``，所以第 2 行和第 3 行只差 1 却分属两桶。

    这不是缺陷也不是疏漏：指纹只负责「完全同一条」这个快路径，漂了几行的
    交给 O(n²) 的相似度那一步（它会带着 ``|Δline| ≤ 3`` 把它们合起来）。
    把它当精确判据用的人会在这里得到意外，而那个意外表现为「本该合并的
    两条没合并」—— 多一条评论，不报错。这条测试就是钉住这个边界。
    """
    assert line_bucket(2) == 0
    assert line_bucket(3) == 1
    assert fingerprint(_f(line=2)) != fingerprint(_f(line=3))


@pytest.mark.unit
def test_line_bucket_boundaries() -> None:
    assert [line_bucket(n) for n in (0, 1, 2, 3, 4, 5, 6)] == [0, 0, 0, 1, 1, 1, 2]


# --------------------------------------------------------------------------- #
# 字段取舍
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_severity_is_deliberately_not_part_of_the_fingerprint() -> None:
    """**严重度不参与指纹。**

    两个 Worker 对同一处代码给出不同严重度是常态，而冲突消解（``conflicts.py``）
    整节的存在就是为了裁决它。把 severity 放进指纹，等于让「严重度有分歧」
    看起来像「两个不同的问题」—— 于是冲突永远发现不了，裁决结果永远不产生，
    而那正是这个项目想做的那件事。
    """
    assert fingerprint(_f(severity=Severity.CRITICAL)) == fingerprint(_f(severity=Severity.LOW))


@pytest.mark.unit
def test_category_is_part_of_the_fingerprint() -> None:
    """同一行上的 SQL 注入和 XSS 是两回事 —— 合并它们会丢掉一条。"""
    assert fingerprint(_f(category="sqli")) != fingerprint(_f(category="xss"))


@pytest.mark.unit
def test_category_aliases_are_collapsed_before_hashing() -> None:
    """别名由契约层的校验器收敛，指纹直接用规范名。

    ``"SQL Injection"`` 和 ``"sqli"`` 是同一个类目，走契约层时会变成同一个
    字符串，所以指纹自然一致 —— 这条测试守的是「别在指纹这层再引入一套
    类目归一化」。
    """
    assert fingerprint(_f(category="SQL Injection")) == fingerprint(_f(category="sqli"))


@pytest.mark.unit
def test_rule_id_is_not_part_of_the_fingerprint() -> None:
    """``rule_id`` 是「引用了哪条规则」，不是「发现了什么」——
    同一处问题有没有命中规则，不该改变它的身份。"""
    assert fingerprint(_f(rule_id="sec-sqli-001")) == fingerprint(_f())


# --------------------------------------------------------------------------- #
# 消息文本：保守归一化
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "message",
    [
        "用 f-string 拼接 SQL，用户输入没有参数化",
        "用 f-string 拼接 SQL，用户输入没有参数化。",  # 尾部标点
        "**用 f-string 拼接 SQL，用户输入没有参数化**",  # markdown 加粗
        "  用 f-string   拼接 SQL，用户输入没有参数化  ",  # 多余空白
        "用 F-String 拼接 SQL，用户输入没有参数化",  # 大小写
        "用 Ｆ-string 拼接 SQL，用户输入没有参数化",  # 全角字母（中文输入法的产物）
    ],
)
def test_message_decoration_does_not_change_the_fingerprint(message: str) -> None:
    assert fingerprint(_f(message=message)) == fingerprint(_f())


@pytest.mark.unit
def test_different_wording_gives_different_fingerprints() -> None:
    """措辞不同 → 指纹不同。**这是正确的**，不是缺陷。

    归一化到「措辞无关」需要理解语义，而那件事（embedding / 相似度）本来就
    在 O(n²) 那一步做。指纹只做「一字不差」这一档，把两档的活分开，
    各自才测得清楚。
    """
    a = _f(message="用 f-string 拼接 SQL")
    b = _f(message="SQL 语句由用户输入直接拼成")
    assert fingerprint(a) != fingerprint(b)


@pytest.mark.unit
def test_numbers_are_not_stripped() -> None:
    """**绝不抹掉数字和标识符。**

    把数字归一掉是「两条不同的发现长得一样」的最短路径：``第 3 行漏了校验``
    和 ``第 30 行漏了校验`` 会被合并成一条，其中一条静默消失。
    归一化保守一点，代价只是多跑一次相似度比较。
    """
    assert fingerprint(_f(message="第 3 行漏了边界校验")) != fingerprint(_f(message="第 30 行漏了边界校验"))


@pytest.mark.unit
def test_normalize_message_only_trims_the_edges() -> None:
    """首尾标点去掉、**中间的原样保留**。

    中间那些字符（括号、加号、点）是代码本身的形状，抹掉它们就是把
    ``dict.get()`` 和 ``dict get`` 变成同一件事。
    """
    assert normalize_message("(A + B).") == "a + b"
    assert normalize_message("缺少校验，见 db.py:42。") == "缺少校验,见 db.py:42"
