"""规则库加载器。

规则库是 ``corpus/*.yaml`` 里**手写的**数据，不是从代码仓库里索引出来的。
这是刻意的选择（见 CLAUDE.md「容易被直觉带错的技术决定」）：60–120 条精选规则
本身就是一个可被审阅的作品，而向量库解决的是「几万条规则里找相关的」这个问题 ——
我们没有几万条。

**加载时严格校验，出错就炸。** 规则库是我们自己的数据文件，一条拼错的
``worker_type`` 会让这条规则永远检索不到，而且**不会报任何错** ——
表现是「某个类目的问题从来没人报」。这类静默失效比加载失败难发现得多，
所以宁可在这里炸掉。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from sfly_shared.contracts import Rule, WorkerType
from sfly_shared.logging import get_logger

log = get_logger(__name__)

CORPUS_DIR = Path(__file__).parent / "corpus"


class RuleCorpusError(RuntimeError):
    """规则库有问题。**不要吞掉它** —— 一个加载不全的规则库会让评测数字
    整体偏移，而你不会有任何察觉。"""


@dataclass(frozen=True, slots=True)
class RuleSet:
    """加载好的规则库。不可变，可以安全地跨协程共享。"""

    rules: tuple[Rule, ...]

    @property
    def by_id(self) -> Mapping[str, Rule]:
        return {r.id: r for r in self.rules}

    def for_worker(self, worker_type: WorkerType) -> list[Rule]:
        return [r for r in self.rules if r.worker_type == worker_type]

    def for_language(self, worker_type: WorkerType, language: str) -> list[Rule]:
        return [r for r in self.for_worker(worker_type) if r.matches_language(language)]

    def select(self, ids: Iterable[str]) -> list[Rule]:
        """按 id 取规则，**顺序按传入的 ids**。

        ``specs.core_rule_ids`` 里的顺序是有意排的（最核心的在前），
        按字典序返回会让提示词里规则段的顺序每次都不一样 —— 那会打掉
        DeepSeek 的前缀缓存。
        """
        index = self.by_id
        return [index[i] for i in ids if i in index]

    @property
    def ids(self) -> set[str]:
        return {r.id for r in self.rules}

    def __len__(self) -> int:
        return len(self.rules)


def _load_file(path: Path) -> list[Rule]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RuleCorpusError(f"{path.name} 不是合法的 YAML：{exc}") from exc

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise RuleCorpusError(f"{path.name} 的顶层必须是规则列表，实际是 {type(raw).__name__}")

    rules: list[Rule] = []
    problems: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            problems.append(f"第 {index + 1} 项不是映射")
            continue
        try:
            rules.append(Rule.model_validate(entry))
        except ValidationError as exc:
            first = exc.errors()[0]
            loc = ".".join(str(x) for x in first.get("loc", ()))
            problems.append(f"{entry.get('id', f'第 {index + 1} 项')} 的 {loc}：{first.get('msg', '')}")

    if problems:
        # 一次报出全部问题，而不是修一个跑一次。规则库编辑是批量操作。
        raise RuleCorpusError(
            f"{path.name} 有 {len(problems)} 条规则不合法：\n  - " + "\n  - ".join(problems)
        )
    return rules


@lru_cache(maxsize=1)
def load_rules(corpus_dir: Path | None = None) -> RuleSet:
    """加载整个规则库。进程内缓存 —— 规则文件在运行时不会变。"""
    directory = corpus_dir or CORPUS_DIR
    if not directory.is_dir():
        raise RuleCorpusError(f"规则库目录不存在：{directory}")

    files = sorted(directory.glob("*.yaml"))
    all_rules: list[Rule] = []
    for path in files:
        all_rules.extend(_load_file(path))

    seen: dict[str, str] = {}
    duplicates: list[str] = []
    for rule in all_rules:
        if rule.id in seen:
            duplicates.append(rule.id)
        seen[rule.id] = rule.id
    if duplicates:
        # 重复 id 会让 ``RuleSet.by_id`` 静默丢掉一条，而检索和回填都按 id 走 ——
        # 症状是「这条规则明明写了却不生效」。
        raise RuleCorpusError(f"规则 id 重复：{', '.join(sorted(set(duplicates)))}")

    log.info("rag.corpus_loaded", files=len(files), rules=len(all_rules))
    return RuleSet(tuple(all_rules))


def corpus_stats() -> dict[str, Any]:
    """每个 Worker 各有多少条规则。CLI 和评测报告都用它做一次合理性检查。"""
    ruleset = load_rules()
    per_worker = {w.value: len(ruleset.for_worker(w)) for w in WorkerType}
    return {"total": len(ruleset), "per_worker": per_worker}
