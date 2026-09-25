"""发现指纹 —— 全确定性、零 IO、零 LLM。

### 指纹是**快速路径的键**，不是去重的正确性机制

这一条必须先说清楚，因为反过来理解会导致一个静默的数据损失：
「两条发现指纹相同」是**充分不必要**条件。

    fingerprint(file, line//3, category, normalize(message))

* 指纹相同 → 一定是同一条发现，可以直接合并（O(1) 的字典查找）。
* 指纹不同 → **什么都不能推断**。两个 Worker 用不同措辞描述同一处 SQL 注入，
  或者模型这次报在第 10 行、下次报在第 12 行，指纹都不一样。

真正的去重在 ``cluster.py``（M9）：同路径 + ``|Δline| ≤ 3`` + 相似度阈值，
O(n²) 的并查集。指纹只是让它不必每次都走那条路。

### ``line//3`` 是一把**粗**筛子，别指望它精确

``line//3`` 把行号切成 ``[0,1,2] [3,4,5] [6,7,8] …``。注意它**不**保证「相差 3 行
以内落进同一个桶」：第 2 行和第 3 行只差 1，却分属两个桶。

这不是缺陷，是刻意的粒度选择 —— 桶的作用是把「完全同一条发现」和「漂了一点
的同一发现」区分开，后者交给 O(n²) 那一步。把它当成「±3 行漂移的精确匹配」
会在跨边界的那一对上得到意外结果，**而那个意外表现为「两条本来该合并的发现
没合并」—— 多一条评论，不会报错**。

### 为什么不把 severity 放进指纹

两个 Worker 对同一处代码给出不同严重度是常态，而**冲突消解（``conflicts.py``）
整节的存在就是为了裁决这件事**。把 severity 放进指纹，等于让「严重度有分歧」
看起来像「两个不同的问题」—— 于是冲突永远也发现不了。

### 为什么保守地归一化 message

归一化越狠，越容易把两条**不同的**发现压成同一个指纹 —— 那是误合并，
两条变一条，另一条静默消失。归一化不足只是少一次快速匹配，它们仍然会在
相似度那一步被合起来。代价不对称，所以下面只做不改变语义的整理。
"""

from __future__ import annotations

import unicodedata

from sfly_shared.contracts import Finding, normalize_path, stable_hash

#: 行号分桶宽度。见模块文档里关于「粗筛子」的说明。
_LINE_BUCKET = 3

#: markdown 装饰字符。去掉它们才能让 ``**缺少校验**`` 和 ``缺少校验`` 同指纹。
#: 这些字符不可能承载语义（不像数字、标识符、路径），所以删掉是安全的。
_MARKDOWN_CHARS = str.maketrans("", "", "`*_#~|")

#: 首尾要去掉的标点。中文标点也要列 —— 模型会混用，
#: 而 ``缺少校验`` 与 ``缺少校验。`` 显然是同一条。
_EDGE_PUNCTUATION = " .,;:!?、。，；：！？…·-—()[]{}<>'\"“”‘’"


def normalize_message(text: str) -> str:
    """把消息压成一个稳定的骨架。

    顺序有讲究：先 NFKC（全角折半角、兼容字符归一），再小写，再删 markdown 装饰，
    再折叠空白，最后才裁首尾标点 —— 折叠空白必须在删字符之后，否则被删字符
    留下的连续空格会拼进词里（``a ** b`` → ``a  b`` → ``a b`` 这步才对）。
    """
    s = unicodedata.normalize("NFKC", text).lower()
    # translate 只删字符不补位，所以 "a `b` c" → "a b c"（两个空格）→ 靠 split 收口
    s = " ".join(s.translate(_MARKDOWN_CHARS).split())
    return s.strip(_EDGE_PUNCTUATION)


def line_bucket(line: int) -> int:
    """行号落进哪个桶。负数按数学除算 —— 它本来就不该出现，
    ``Finding._norm_line`` 不拒绝负数，所以这里也不假装它会拒绝。"""
    return line // _LINE_BUCKET


def fingerprint(finding: Finding) -> str:
    """一条发现的指纹。跨进程稳定（用 ``stable_hash`` 而不是内置 ``hash()``）。

    ``file`` 先过 ``normalize_path``：模型会写 ``b/app/db.py``、``./app/db.py``、
    ``app\\db.py``，不归一的话同一条发现会有三种指纹。
    """
    return stable_hash(
        normalize_path(finding.file),
        str(line_bucket(finding.line)),
        # category 已经由契约层的校验器收敛到规范名（``SQL Injection`` →
        # ``sql_injection`` → ``sqli``），所以这里直接用。
        finding.category,
        normalize_message(finding.message),
    )
