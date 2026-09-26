"""相似度聚类 —— 纯函数、无 IO、无 LLM、毫秒级。

这个文件里有两类测试，读法完全不同：

* **行为测试**：断言聚类的四条判据。它们错了就是代码错了。
* **表征测试**（characterization）：把「这个方法的能力边界在哪」量下来钉住。
  它们**不检查代码对不对**，检查的是**我们对它的认识有没有漂移** ——
  阈值、文档里的那张表、和真实的测量值必须始终是同一个东西。

第二类看着奇怪，但它是这个模块最需要的：词面相似度有一个很容易被忘掉的极限，
而忘掉它的方式恰恰是**报告里的数字越来越好看**（误合并让去重率上升、
漏合并让评论变多但没人量），没有任何东西会报错。
"""

from __future__ import annotations

import pytest

from factories import finding, result
from sfly_agent.aggregate.cluster import (
    CROSS_WORKER_THRESHOLD,
    SAME_WORKER_THRESHOLD,
    cluster_findings,
    similarity,
    tokenize,
)
from sfly_shared.contracts import Severity, WorkerType

pytestmark = pytest.mark.unit

# --------------------------------------------------------------------------- #
# 人工标注的措辞对
#
# 全部是「同一处代码上的两条发现」，标注的是**它们该不该被合并成一条**。
# 这不是从数据里采的，是照着三个 Worker 的真实措辞风格写的 —— 它是这个模块
# 唯一的一份 ground truth，所以宁可少而准。
# --------------------------------------------------------------------------- #

#: 该合并：同一个问题，措辞不同。
MERGE_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("用 f-string 拼接 SQL，用户输入没有参数化", "SQL 用 f-string 拼接，用户输入未参数化", "近义改写"),
    (
        "循环里逐个查询数据库，产生 N+1 次往返",
        "循环内逐个查询数据库，产生 N+1 次往返",
        "行号漂移、措辞几乎一样",
    ),
    ("这里没有做输入校验", "这里缺少输入校验", "极短句"),
    ("密码硬编码在源码中", "密码被硬编码在源码里", "短句、语序变化"),
    ("异常被裸 except 吞掉，失败静默", "裸 except 吞掉异常，失败被静默忽略", "语序变化"),
    (
        "SQL 语句用字符串拼接构造，用户输入可直接改写查询语义",
        "查询由 f-string 拼接，攻击者可以改写 SQL 语义",
        "跨 Worker 同义改写",
    ),
    (
        "硬编码的 API 密钥写在源码里，会随仓库泄露",
        "密钥被硬编码在源文件中，任何人都能从仓库读到它",
        "跨 Worker 同义改写",
    ),
    ("路径未做规范化，存在目录穿越风险", "文件名没有归一化，可以跳出目标目录", "跨 Worker 同义改写"),
    ("该查询没有分页上限，结果集可能撑爆内存", "缺少 LIMIT，返回行数没有上界", "跨 Worker 同义改写"),
    ("循环里每次迭代都发一次查询", "在循环体内调用数据库，往返次数随行数线性增长", "跨 Worker 同义改写"),
)

#: 不该合并：同一处代码，但是两件不同的事。
KEEP_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("缺少对 user_id 的类型校验", "缺少对 page_size 的上限校验", "同类目、不同校验对象"),
    ("这个变量名拼写错误", "这个函数缺少 docstring", "不同问题"),
    ("这里应该用常量而不是魔法数字", "这行超过 120 列，应折行", "不同问题"),
    ("SQL 注入：输入未参数化", "循环内查询，N+1 次往返", "不同类目（由类目闸否掉）"),
    ("函数过长，建议拆分", "函数命名不符合规范，建议改成动词开头", "同类目模板句"),
    ("这里有未使用的导入", "这里有未使用的变量", "模板句只差一个词"),
    ("该函数缺少类型标注", "该函数缺少错误处理", "模板句只差一个词"),
    ("这里应该加索引", "这里应该加超时", "模板句只差一个词"),
    ("这段代码没有处理超时", "这段代码没有处理重试", "模板句只差一个词"),
)


# --------------------------------------------------------------------------- #
# 表征测试：这个方法的极限
# --------------------------------------------------------------------------- #


def _best_threshold_errors() -> tuple[float, int]:
    """遍历所有阈值，返回错误最少的那一档 ``(阈值, 错误数)``。"""
    best: tuple[float, int] | None = None
    for step in range(1, 100):
        threshold = step / 100
        errors = sum(1 for a, b, _ in MERGE_PAIRS if similarity(a, b) < threshold) + sum(
            1 for a, b, _ in KEEP_PAIRS if similarity(a, b) >= threshold
        )
        if best is None or errors < best[1]:
            best = (threshold, errors)
    assert best is not None
    return best


def test_lexical_similarity_cannot_separate_paraphrase_from_two_unrelated_issues() -> None:
    """**已知极限**：词面相似度分不开「同义改写」和「两件不同的事」。

    这条断言的是**缺陷的存在**，而不是修复它。它挂掉有两种可能：

    * 上面那两组标注写错了（先检查这个）
    * 词元化或相似度真的变强了 —— 那是好事，但**必须同时更新** ``cluster.py``
      里那张表、两档阈值、以及 README 的已知限制一节。
      让它悄悄变绿，等于让文档继续描述一个已经不存在的世界。
    """
    merges = sorted(similarity(a, b) for a, b, _ in MERGE_PAIRS)
    keeps = sorted(similarity(a, b) for a, b, _ in KEEP_PAIRS)

    assert merges[0] < keeps[-1], "两个区间不再重叠 —— 该去更新文档和阈值了"
    assert keeps[0] < merges[-1], "两个区间不再重叠 —— 该去更新文档和阈值了"

    threshold, errors = _best_threshold_errors()
    # 19 对里错 7 对：比抛硬币好一点，但远谈不上可用。
    # 数值写成范围而不是等号，是为了让「变好了一点点」不至于立刻挂 ——
    # 但「变好到能用了」一定会挂。
    assert errors >= 5, f"阈值 {threshold} 只错 {errors}/19 —— 极限被突破了，去更新文档"


def test_the_merge_and_keep_ranges_are_what_the_docstring_claims() -> None:
    """文档里那张表必须是**量出来的**，不是写上去的。

    下面这几个数是 ``cluster.py`` 文档表格的来源。它们变了，表就得跟着改 ——
    这是唯一能防止「文档描述一个已经不存在的算法」的机制。
    """
    merges = sorted(similarity(a, b) for a, b, _ in MERGE_PAIRS)
    keeps = sorted(similarity(a, b) for a, b, _ in KEEP_PAIRS)

    assert 0.25 <= merges[0] <= 0.35, f"该合并的最小值 {merges[0]:.3f} 变了"
    assert 0.70 <= merges[-1] <= 0.95, f"该合并的最大值 {merges[-1]:.3f} 变了"
    assert keeps[0] <= 0.05, f"不该合并的最小值 {keeps[0]:.3f} 变了"
    assert 0.82 <= keeps[-1] <= 0.92, f"不该合并的最大值 {keeps[-1]:.3f} 变了"


def test_cross_worker_paraphrase_scores_far_below_the_threshold() -> None:
    """钉住最反直觉的那一条：**两个 Worker 说同一件事时，分数反而低**。

    直觉上「跨 Worker 印证」应该是相似度最高的那一档。实测正好相反 ——
    不同人设的措辞差异大，共享的汉字少；反倒是同一个 Worker 复述自己
    分数最高。这条测试就是把这个直觉纠正钉在这里。
    """
    same_issue = similarity(
        "该查询没有分页上限，结果集可能撑爆内存",
        "缺少 LIMIT，返回行数没有上界",
    )
    same_worker_repeat = similarity(
        "循环里逐个查询数据库，产生 N+1 次往返",
        "循环内逐个查询数据库，产生 N+1 次往返",
    )
    assert same_issue < CROSS_WORKER_THRESHOLD
    assert same_worker_repeat > SAME_WORKER_THRESHOLD
    assert same_worker_repeat > same_issue * 2


# --------------------------------------------------------------------------- #
# 词元化
# --------------------------------------------------------------------------- #


def test_tokenize_splits_chinese_into_bigrams() -> None:
    """中文按双字组切 —— 空白分词在这里会退化成「整句一个词」。"""
    tokens = tokenize("缺少输入校验")
    assert {"缺少", "少输", "输入", "入校", "校验"} <= tokens


def test_tokenize_keeps_code_identifiers_whole() -> None:
    """ASCII 片段必须**整词**保留：``cursor.execute`` 断成 ``cursor``/``execute``
    是对的，断成单个字母就丢了信息。"""
    tokens = tokenize("调用 cursor.execute 时用了 f-string，见 CWE-89")
    assert {"cursor", "execute", "string", "cwe", "89"} <= tokens


def test_tokenize_separates_clauses_at_chinese_punctuation() -> None:
    """中文标点是天然的断句点 —— 不切的话双字组会跨句拼接，
    造出一堆两个句子共有的假词元，把相似度整体抬高。"""
    assert "接构" in tokenize("语句用字符串拼接构造，用户输入")
    assert "接用" not in tokenize("语句用字符串拼接构造，用户输入")
    assert "构用" not in tokenize("语句用字符串拼接构造，用户输入")


def test_similarity_of_empty_messages_is_zero() -> None:
    """空消息不相似 —— 而不是「两个都为空所以相同」。

    把空集当相同的后果是：两条**空消息**的发现会被合并，而空消息通常是
    模型返回坏 JSON 时的降级产物，它们之间没有任何关系。
    """
    assert similarity("", "") == 0.0
    assert similarity("", "SQL 注入") == 0.0
    assert similarity("!!!", "???") == 0.0


# --------------------------------------------------------------------------- #
# 行为：四条判据
# --------------------------------------------------------------------------- #


def test_the_same_worker_repeating_itself_is_one_cluster() -> None:
    """同一个 Worker 换句话说同一处 —— 这是最常见的真实去重场景。"""
    clusters = cluster_findings(
        [
            result(
                "t1",
                worker_type=WorkerType.SECURITY,
                findings=[
                    finding(message="用 f-string 拼接 SQL，用户输入没有参数化", line=12),
                    finding(message="SQL 用 f-string 拼接，用户输入未参数化", line=13),
                ],
            )
        ]
    )
    assert len(clusters) == 1
    assert len(clusters[0]) == 2


def test_line_drift_beyond_the_tolerance_does_not_merge() -> None:
    """差 4 行就不合并。同一句话在 4 行之外出现，多半是两处独立的代码。"""
    message = "循环里逐个查询数据库，产生 N+1 次往返"
    clusters = cluster_findings(
        [result("t1", findings=[finding(message=message, line=10), finding(message=message, line=14)])]
    )
    assert len(clusters) == 2


def test_a_different_category_never_merges() -> None:
    """**类目不同的两条永不合并 —— 哪怕措辞一模一样、行号也一样。**

    这是给冲突消解让路：同路径 + 邻近行 + 不同 Worker + 严重度差 ≥ 2
    正是冲突的定义（``conflicts.py``）。在这里合并掉它们，冲突消解就永远不会
    触发，而整节功能的失效看起来会像「这段代码没问题」。
    """
    message = "这一行同时有问题"
    clusters = cluster_findings(
        [
            result(
                "t1",
                worker_type=WorkerType.SECURITY,
                findings=[finding(message=message, line=42, category="sqli")],
            ),
            result(
                "t1",
                worker_type=WorkerType.PERFORMANCE,
                findings=[finding(message=message, line=42, category="n_plus_one")],
            ),
        ]
    )
    assert len(clusters) == 2


def test_a_different_file_never_merges() -> None:
    """路径先过 ``normalize_path`` 再比 —— ``b/app/db.py`` 和 ``app/db.py`` 是同一个文件。"""
    message = "循环里逐个查询数据库，产生 N+1 次往返"
    same_file_spelled_differently = cluster_findings(
        [
            result(
                "t1",
                findings=[
                    finding(message=message, line=10, file="app/db.py"),
                    finding(message=message, line=11, file="./app/db.py"),
                ],
            )
        ]
    )
    assert len(same_file_spelled_differently) == 1

    different_file = cluster_findings(
        [
            result(
                "t1",
                findings=[
                    finding(message=message, line=10, file="app/db.py"),
                    finding(message=message, line=10, file="app/api.py"),
                ],
            )
        ]
    )
    assert sorted(len(cluster) for cluster in different_file) == [1, 1]


def test_clustering_is_transitive() -> None:
    """A~B、B~C 而 A≁C 时，三条合成一簇 —— 单链接聚类的固有行为。

    写成测试是因为它有一个反直觉的后果：**簇可以「串」起来**。
    一行里连续五条措辞相近的发现会连成一大簇，而首尾两条其实差得挺远。
    这是刻意接受的代价（换成把 A 排除出去就需要指定「簇心」，
    而簇心又得先有簇才能定 —— 循环定义）。
    """
    clusters = cluster_findings(
        [
            result(
                "t1",
                findings=[
                    finding(message="缺少对 user_id 的类型校验", line=10),
                    finding(message="缺少对 user_id 的类型和范围校验", line=11),
                    finding(message="缺少对 user_id 的类型和范围校验，以及长度校验", line=12),
                ],
            )
        ]
    )
    assert len(clusters) == 1
    assert len(clusters[0]) == 3


def test_clustering_does_not_depend_on_arrival_order() -> None:
    """三条结果从 Redis 来的顺序不固定，而聚类结果必须与顺序无关。

    并查集求的是**连通分量**，而连通分量与「先比较哪一对」无关 ——
    这正是用并查集而不是「贪心配对所有」的理由。
    """
    a = finding(message="用 f-string 拼接 SQL，用户输入没有参数化", line=12)
    b = finding(message="SQL 用 f-string 拼接，用户输入未参数化", line=13)
    c = finding(message="密码硬编码在源码中", line=40, category="secrets")

    def run(order: list[object]) -> list[int]:
        return sorted(len(cluster) for cluster in cluster_findings([result("t1", findings=order)]))  # type: ignore[arg-type]

    assert run([a, b, c]) == run([c, a, b]) == run([b, c, a]) == [1, 2]


def test_severity_does_not_affect_clustering() -> None:
    """严重度不参与合并判断 —— 它只影响谁当选簇代表，以及是不是一次冲突。

    把严重度放进合并判据，等于让「两个 Worker 对严重度有分歧」看起来像
    「两个不同的问题」，于是冲突永远发现不了。
    """
    message = "这里没有做输入校验"
    clusters = cluster_findings(
        [
            result(
                "t1",
                worker_type=WorkerType.SECURITY,
                findings=[finding(message=message, line=10, severity=Severity.CRITICAL)],
            ),
            result(
                "t1",
                worker_type=WorkerType.STYLE,
                findings=[finding(message=message, line=11, severity=Severity.LOW)],
            ),
        ]
    )
    assert len(clusters) == 1


def test_a_worker_that_reported_nothing_contributes_no_cluster() -> None:
    """失败的上报（没有 findings）不该产出空簇 —— 空簇会让 ``_representative``
    在空列表上取 ``ranked[0]``，报 ``IndexError`` 而不是「这个 Worker 没结果」。"""
    clusters = cluster_findings(
        [
            result("t1", worker_type=WorkerType.SECURITY, findings=[]),
            result("t1", worker_type=WorkerType.PERFORMANCE, findings=[finding()]),
        ]
    )
    assert len(clusters) == 1
