"""聚合算法 —— 主 Agent 的「集中式决策」那一半。

这是项目里唯一自带算法的一层，而且**全部是确定性的纯函数**：无 IO、无 LLM、
无随机。理由很直接 —— 评测要可复现。

    fingerprint  发现指纹（快速路径的键）
    cluster      并查集聚类 + 簇代表选举（M9）
    confidence   置信度重算（M9）
    conflicts    冲突消解规则引擎（M9）
    decision     阻断决策（M9）
    pipeline     串起来的入口（M9）

分层：``fingerprint`` 只依赖契约层，不认识其他模块；``pipeline`` 认识全部。
**反过来不行** —— 算法之间互相调用会让单测从「纯函数进、断言出」退化成
「要先搭好一半流水线」。
"""

from sfly_agent.aggregate.fingerprint import fingerprint, line_bucket, normalize_message

__all__ = ["fingerprint", "line_bucket", "normalize_message"]
