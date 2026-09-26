"""聚合算法 —— 主 Agent 的「集中式决策」那一半。

这是项目里唯一自带算法的一层，而且**全部是确定性的纯函数**：无 IO、无 LLM、
无随机。理由很直接 —— 评测要可复现。

    fingerprint  发现指纹（落库标识；合并判断已由 cluster 承担）
    cluster      并查集相似度聚类 + 词元化 + 相似度
    confidence   置信度重算
    conflicts    冲突消解规则引擎
    decision     阻断决策
    render       评论正文渲染
    pipeline     串起来的入口

分层：``fingerprint`` / ``cluster`` 只依赖契约层，互不认识；``pipeline`` 认识全部。
**反过来不行** —— 算法之间互相调用会让单测从「纯函数进、断言出」退化成
「要先搭好一半流水线」。
"""

from sfly_agent.aggregate.cluster import cluster_findings, similarity, tokenize
from sfly_agent.aggregate.conflicts import resolve_conflicts
from sfly_agent.aggregate.fingerprint import fingerprint, line_bucket, normalize_message

__all__ = [
    "cluster_findings",
    "fingerprint",
    "line_bucket",
    "normalize_message",
    "resolve_conflicts",
    "similarity",
    "tokenize",
]
