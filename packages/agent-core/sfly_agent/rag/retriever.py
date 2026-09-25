"""规则检索。

**v0 是刻意的极简版**：核心规则保底 + 本 lane 其余规则按 id 稳定排序取前 N。
唯一的检索决策就是「取哪几条」，而排序必须是**确定的** —— 提示词里规则段的
顺序变化会让前缀缓存在每次调用时失效，而缓存的单价差约十倍。

BM25 会在 M9 接在这个签名后面（``plan`` 节点调用它，把结果写进 TaskMessage）。
调用方不需要知道用的是哪种检索：这正是「规则检索是编排层的决策」这句话的
落地方式 —— Worker 只负责消费已经选好的规则。
"""

from __future__ import annotations

from collections.abc import Iterable

from sfly_agent.rag.loader import RuleSet
from sfly_shared.contracts import Rule, WorkerType


def select_rules(
    ruleset: RuleSet,
    *,
    worker_type: WorkerType,
    language: str = "",
    core_ids: Iterable[str] = (),
    top_k: int = 8,
) -> list[Rule]:
    """挑出这次要送给 Worker 的规则。

    核心规则**永远包含**，且排在最前面：它们是这个 Worker 的地基，
    不能被检索结果挤掉。真正的检索只负责填满剩下的名额。
    """
    core = ruleset.select(core_ids)
    core_set = {r.id for r in core}

    rest = [r for r in ruleset.for_worker(worker_type) if r.id not in core_set]
    if language:
        # 语言过滤是**降级而非排除**：与语言无关的规则（languages 为空）
        # 始终保留，而标注了其它语言的规则才被排除。
        rest = [r for r in rest if r.matches_language(language)]

    rest.sort(key=lambda r: r.id)  # 确定性，见模块文档

    remaining = max(0, top_k - len(core))
    return [*core, *rest[:remaining]]
