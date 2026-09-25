"""RAG —— 手写规则库与检索。

**刻意不用向量库**（见 CLAUDE.md）：60–120 条精选规则本身就是一个可被审阅的
作品，而向量库解决的是「几万条规则里找相关的」—— 我们没有几万条。
词法检索在这个规模上完全够用，而且是确定性的，评测才能复现。

v0 的检索策略见 ``retriever.select_rules``：核心规则保底 + 本 lane 其余规则
按 id 稳定排序。BM25 会接在同一个签名后面（M9），调用方不用改。
"""

from sfly_agent.rag.loader import RuleCorpusError, RuleSet, corpus_stats, load_rules
from sfly_agent.rag.retriever import select_rules

__all__ = ["RuleCorpusError", "RuleSet", "corpus_stats", "load_rules", "select_rules"]
