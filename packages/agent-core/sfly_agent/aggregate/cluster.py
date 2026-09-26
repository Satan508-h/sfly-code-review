"""相似度聚类 —— 把「同一个问题的不同说法」合并成一个簇。

### 指纹合不掉的那两类

``fingerprint`` 只能合并**完全相同的声称**（同路径、同行号桶、同类目、
同一句话）。真实数据里有两类它一定合不掉：

* 两个 Worker 用不同措辞说同一处问题（「SQL 语句用字符串拼接构造」vs
  「查询由 f-string 拼接，可被输入改写」）
* 模型这次报第 10 行、下次报第 12 行 —— 行号桶错开，指纹就不同了

这两类只能靠**相似度**判断，也就是这个文件。做法是并查集 + 单链接聚类：
只要有一对满足条件，整个连通分量就是一簇（A~B、B~C 而 A≁C 时三条也会合成一簇，
这是单链接的固有行为，不是 bug —— 代价是簇可能「串」起来，收益是不会漏合并）。

### 合并条件（四条全满足）

1. 归一化路径相同
2. ``|Δline| ≤ 3``
3. **类目相同**
4. ``sim ≥ T``（同 Worker ``0.75`` / 跨 Worker ``0.55``）

### 第 3 条值得单独解释，它不是原设计里的

原设计只有前两条加第 4 条。加「类目必须相同」是因为**两类失败的代价不对称**：

* **误合并**：两条发现变一条，另一条**静默消失**。没有任何东西会报错，
  评测里的召回率会掉，而你会以为是模型没发现。
* **漏合并**：评论区多一条。仅此而已。

而模糊相似度天然会误合并 —— 同一个文件里两条不同的中文发现共享大量汉字，
字符级相似度很容易越过 0.55。所以凡是能提前否掉的，都要提前否掉。

还有一条更硬的理由：**类目不同但同一处的两条，正是「冲突」的定义**
（``conflicts.py``：同路径 + 邻近行 + 不同 Worker + 严重度差 ≥ 2）。
在这里把它们合并掉，冲突消解就永远不会触发 —— 一个整节的功能被上游静默吃掉，
而且看起来像是「这段代码没问题」。

### 相似度是两个口径取大

``max(词元 Jaccard, rapidfuzz.token_set_ratio / 100)``。

Jaccard 惩罚长度差异（一句话是另一句的子集时得分很低），而 ``token_set_ratio``
处理包含关系很好。两个都算再取大，是因为它们在不同的失败方向上互补：
短消息 vs 长消息靠后者，措辞接近但词序不同靠前者。

### 实测：**词面相似度分不开「同义改写」和「两件不同的事」**

这一节是量出来的，不是推出来的。在一组 19 对人工标注的措辞上
（同一处代码、标注「该合并 / 不该合并」，见 ``tests/unit/agent/test_cluster.py``）：

| | 最小值 | 中位数 | 最大值 |
|---|---|---|---|
| 该合并（10 对） | 0.289 | 0.744 | 0.917 |
| 不该合并（9 对） | 0.000 | 0.600 | 0.870 |

**两个区间完全重叠，不存在能分开它们的阈值** —— 遍历所有阈值，最好的一档
也要错 7/19。原因很直白：中文同义改写可以做到几乎不共享汉字
（「该查询没有分页上限，结果集可能撑爆内存」vs「缺少 LIMIT，返回行数没有上界」
= 0.289），而模板化的不同问题会共享大量汉字
（「该函数缺少类型标注」vs「该函数缺少错误处理」= 0.800）。

所以这个模块的定位必须说清楚：**它是近似重复过滤器，不是语义去重器。**

* 抓得住：同一个 Worker 换句话说同一处（0.74–0.92）、模型复述、行号漂移。
  这才是真实数据里的常见情况 —— 而且它正是 `agreement` 置信度项的输入。
* 抓不住：跨 Worker 的同义改写（0.29–0.53 那一档）。**「跨 Worker 印证」
  因此比原设计设想的稀薄得多** —— 两个专家只有在用了几乎相同的措辞时才会被合并。
  要真正抓住它得上 embedding，而那会牺牲确定性（评测要可复现），
  已列入 M11 的可选项。

阈值仍然分了同 Worker / 跨 Worker 两档，但**跨 Worker 那一档的实际作用是
「两个 Worker 用了几乎一样的措辞」**，不是「两个 Worker 说了同一件事」。
这个区别在向别人介绍这个项目时必须讲清楚，否则就是在把一次字符串匹配
说成一次语义理解。

**阈值取值的依据是失败代价不对称**（见上一节）：误合并会静默吃掉一条，
漏合并只是多一条评论。所以两档都往「宁可不合」的方向偏。

### 词元化必须对中文有效，这一条是实测踩出来的

``rapidfuzz`` 和 Jaccard 都按**空白**切词。而这个项目里 Worker 的输出是中文，
一句话里最多只有 ``SQL``、``f-string`` 几个空白分隔的片段 ——
直接把整句扔进去，两条讲同一件事的中文发现会得到接近 0 的相似度，
于是聚类**在有数据的情况下一次也不触发**，而精确率召回率看起来都正常
（只是「跨 Worker 印证」永远是空的）。

所以 :func:`tokenize` 自己做词元化：ASCII 片段按 ``[a-z0-9_]+`` 取词，
中日韩字符取**双字组**（``语句``/``句用``/``用字``…）—— 双字组是中文里
不需要词典就能拿到的最短有义片段，也是搜索引擎里最常用的那一档。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import NamedTuple

from rapidfuzz import fuzz

from sfly_agent.aggregate.fingerprint import normalize_message
from sfly_shared.contracts import Finding, WorkerResult, WorkerType, normalize_path

#: 行号漂移容忍度。**与指纹的行号桶（``line // 3``）是同一个数**，
#: 但含义不同：桶是「粗筛」，这里是「精确判据」。两者取同一个值是因为
#: 用户看到的差异就是「差几行」，而不是因为模板代码要一致。
MAX_LINE_DRIFT = 3

#: 两个 Worker 说同一处问题 —— 措辞差异天然更大，所以阈值更低。
#: **跨 Worker 印证是去重最值钱的产物**：两个独立专家各说一遍，比一个专家说两遍
#: 可信得多，所以这里宁可放宽。
CROSS_WORKER_THRESHOLD = 0.55

#: 同一个 Worker 说两遍。同一个模型在同一处重复自己时措辞通常高度接近，
#: 所以阈值反而更高 —— 低于它就当成两个不同的问题，宁可多一条评论。
SAME_WORKER_THRESHOLD = 0.75

#: ASCII / 代码标识符片段。先小写（``normalize_message`` 已经做过），
#: 所以不必带 ``A-Z``。
_ASCII_TOKEN = re.compile(r"[a-z0-9_]+")

#: 连续的中日韩字符。标点（``，。；``）不在这个区间里，于是天然起到断句作用 ——
#: 这也正是「按标点分句再取双字组」想要的效果。
_CJK_RUN = re.compile(r"[㐀-䶿一-鿿]+")


def tokenize(message: str) -> frozenset[str]:
    """把一条 message 切成词元集合。中文取双字组，见模块文档最后一节。"""
    normalized = normalize_message(message)
    tokens = set(_ASCII_TOKEN.findall(normalized))
    for run in _CJK_RUN.findall(normalized):
        if len(run) == 1:
            # 单字成词（「慢」「空」）。丢掉它会让短消息得到空词元集，
            # 而空集在 ``_similarity`` 里是 0 —— 一条真实的中文短消息
            # 于是永远无法与任何东西合并。
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    return frozenset(tokens)


def similarity(left: str, right: str) -> float:
    """两条 message 的相似度，``[0, 1]``。"""
    return _similarity(tokenize(left), tokenize(right))


def _similarity(left: frozenset[str], right: frozenset[str]) -> float:
    """词元集合之间的相似度。**输入已经是词元集**，不是原文。

    O(n²) 的循环里每个发现会被比较几十次，所以词元化只做一次
    （见 :func:`cluster_findings` 里的 ``_Item``），这里只做集合运算。
    """
    if not left or not right:
        return 0.0
    shared = left & right
    if not shared:
        return 0.0
    jaccard = len(shared) / len(left | right)
    # ``sorted`` 不是装饰：``frozenset`` 的迭代顺序取决于字符串哈希，
    # 而 CPython 的字符串哈希**每个进程都不一样**（PYTHONHASHSEED）。
    # 不排序的话，同一份输入在不同进程里可能得到略微不同的相似度，
    # 而评测要的正是「跑一百遍得到一样的数字」。
    ratio = fuzz.token_set_ratio(" ".join(sorted(left)), " ".join(sorted(right))) / 100.0
    return max(jaccard, ratio)


class _Item(NamedTuple):
    """一个发现 + 它被反复用到的派生量。

    ``tokens`` 和 ``path`` 在这里算一次就够了 —— 放到 O(n²) 的循环里算，
    就是每个发现算几十遍。
    """

    worker: WorkerType
    finding: Finding
    path: str
    tokens: frozenset[str]


class _Union:
    """并查集（路径压缩 + 按秩合并）。标准写法，没有项目特有的地方。"""

    __slots__ = ("_parent", "_rank")

    def __init__(self, size: int) -> None:
        self._parent = list(range(size))
        self._rank = [0] * size

    def find(self, node: int) -> int:
        root = node
        while self._parent[root] != root:
            root = self._parent[root]
        # 第二趟做路径压缩。写成递归更短，但这里不递归 ——
        # 链长由「比较顺序」决定，而比较顺序由输入顺序决定，
        # 递归深度会跟着输入走。n 只有几百，但没理由留一个能爆栈的形状。
        while self._parent[node] != root:
            self._parent[node], node = root, self._parent[node]
        return root

    def union(self, left: int, right: int) -> None:
        root_left, root_right = self.find(left), self.find(right)
        if root_left == root_right:
            return
        if self._rank[root_left] < self._rank[root_right]:
            root_left, root_right = root_right, root_left
        self._parent[root_right] = root_left
        if self._rank[root_left] == self._rank[root_right]:
            self._rank[root_left] += 1


def _mergeable(left: _Item, right: _Item) -> bool:
    """两条发现是不是同一个问题。

    **判断顺序是从便宜到贵**，而且这个顺序不是随意排的：路径是纯字符串比较，
    类目是枚举比较，行号是整数减法，最后才是相似度（要排序 + 走 rapidfuzz）。
    真实数据里绝大多数配对在前三步就被否掉，所以实际只算了很少几次相似度。
    """
    if left.path != right.path:
        return False
    if left.finding.category != right.finding.category:
        return False
    if abs(left.finding.line - right.finding.line) > MAX_LINE_DRIFT:
        return False
    threshold = SAME_WORKER_THRESHOLD if left.worker is right.worker else CROSS_WORKER_THRESHOLD
    return _similarity(left.tokens, right.tokens) >= threshold


def _items(results: Sequence[WorkerResult]) -> list[_Item]:
    """把所有上报摊平成一串 ``_Item``。

    **``(worker_type, Finding)`` 配对，不是裸的 Finding** —— ``Finding`` 上没有
    ``worker_type``（它属于 ``WorkerResult``），而合并阈值取决于「是不是同一个
    Worker 说的」，这个信息在 finding 列表里根本取不到。
    """
    return [
        _Item(
            worker=result.worker_type,
            finding=finding,
            path=normalize_path(finding.file),
            tokens=tokenize(finding.message),
        )
        for result in results
        for finding in result.findings
    ]


def cluster_findings(results: Sequence[WorkerResult]) -> list[list[tuple[WorkerType, Finding]]]:
    """把全部 Worker 的发现聚成簇。每个簇是 ``(worker_type, Finding)`` 的列表。

    返回的顺序**不保证稳定**（取决于输入顺序）；调用方要的是「哪些属于同一簇」，
    展示顺序由 ``pipeline.merge_findings`` 排序后决定。

    结果与输入顺序无关：并查集求的是**连通分量**，而连通分量与「先比较哪一对」
    无关。这一条是评测可复现的前提 —— 三条结果从 Redis 来的顺序不固定。
    """
    items = _items(results)
    union = _Union(len(items))
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if _mergeable(items[i], items[j]):
                union.union(i, j)

    groups: dict[int, list[tuple[WorkerType, Finding]]] = {}
    for index, item in enumerate(items):
        groups.setdefault(union.find(index), []).append((item.worker, item.finding))
    return list(groups.values())
