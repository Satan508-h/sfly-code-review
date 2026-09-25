"""规则库的完整性与检索测试。

**规则库最危险的故障是「静默缺失」。** 一条 ``worker_type`` 拼错的规则
不会让任何东西报错，它只是永远检索不到 —— 表现是「某个类目的问题从来没人报」。
所以这里的多数断言是在**交叉检查内部一致性**：类目有没有主人、
核心规则 id 有没有对应的规则、有没有重复 id。

这几条断言的跨度有意跨到 ``sfly_workers.specs`` —— 那正是它们要防的：
两个文件各自都正确、合起来错了。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sfly_agent.rag.loader import RuleCorpusError, RuleSet, corpus_stats, load_rules
from sfly_agent.rag.retriever import select_rules
from sfly_shared.contracts import WorkerType
from sfly_workers.specs import CATEGORY_OWNER, SPECS, spec_for


@pytest.fixture(scope="module")
def corpus() -> RuleSet:
    return load_rules()


# --------------------------------------------------------------------------- #
# 加载
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_corpus_loads_and_is_not_empty(corpus: RuleSet) -> None:
    assert len(corpus) > 0
    stats = corpus_stats()
    assert stats["total"] == len(corpus)
    # 每个 Worker 都要有规则。某个 lane 一条规则都没有时，它的 Worker
    # 会在没有规则的情况下裸奔 —— 而这件事不会报任何错。
    assert all(count > 0 for count in stats["per_worker"].values()), stats


@pytest.mark.unit
def test_every_rule_category_has_an_owning_worker(corpus: RuleSet) -> None:
    """**跨模块的一致性检查。**

    规则的 ``category`` 与 ``specs.CATEGORY_OWNER`` 是两套独立维护的数据。
    一个不在任何 Worker 职责域里的类目，会让冲突消解的
    ``category_authority`` 规则静默失效 —— 表现是安全 Worker 报的 SQLi
    被风格 Worker 的低危判定拉平，而日志里看不出任何异常。
    """
    orphans = sorted({r.category for r in corpus.rules if r.category not in CATEGORY_OWNER})
    assert orphans == [], f"这些类目不属于任何 Worker：{orphans}"


@pytest.mark.unit
def test_rule_worker_type_matches_the_category_owner(corpus: RuleSet) -> None:
    """规则的 ``worker_type`` 必须和该类目的归属一致。

    不一致时，``select_rules`` 会把这条规则送给另一个 Worker ——
    一个安全规则被塞进风格 Worker 的提示词，它只会照着自己的类目乱报。
    """
    mismatched = [
        (r.id, r.worker_type.value, CATEGORY_OWNER[r.category].value)
        for r in corpus.rules
        if r.category in CATEGORY_OWNER and CATEGORY_OWNER[r.category] != r.worker_type
    ]
    assert mismatched == [], f"规则归属与类目归属不一致：{mismatched}"


@pytest.mark.unit
@pytest.mark.parametrize("worker", list(SPECS))
def test_every_core_rule_id_exists(worker: WorkerType, corpus: RuleSet) -> None:
    """``core_rule_ids`` 是硬编码在 spec 里的引用，必须条条有落。

    悬空的核心规则最坏：它本该是「这个 Worker 的地基」，
    而检索时会被 ``select()`` 静默跳过，于是地基少了一块而没人知道。
    """
    spec = spec_for(worker)
    missing = [rid for rid in spec.core_rule_ids if rid not in corpus.ids]
    assert missing == [], f"{worker.value} 的核心规则不存在：{missing}"


@pytest.mark.unit
def test_no_duplicate_ids(corpus: RuleSet) -> None:
    """重复 id 会让 ``by_id`` 静默丢掉一条，而检索与回填都按 id 走。"""
    ids = [r.id for r in corpus.rules]
    assert len(ids) == len(set(ids))


@pytest.mark.unit
def test_every_rule_has_a_body_that_explains_why(corpus: RuleSet) -> None:
    """规则必须解释「为什么」，不能只写「禁止使用 X」。

    一条只会说禁止的规则，作者的第一反应是找绕过去的方法。"""
    for rule in corpus.rules:
        assert len(rule.body.strip()) > 20, f"{rule.id} 的 body 太短，等于没说"


@pytest.mark.unit
def test_loading_is_cached(corpus: RuleSet) -> None:
    """规则文件运行时不会变，每次检索都重读磁盘是没有意义的开销。"""
    assert load_rules() is corpus


# --------------------------------------------------------------------------- #
# 坏数据的报错必须可读
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_malformed_rule_file_raises_with_the_field_name(tmp_path: Path) -> None:
    """加载失败要给到「哪个字段错了」，而不是一句 ValidationError。

    规则库编辑是批量操作，一次报全比修一个跑一次快得多 ——
    所以下面同时断言了「一次报出全部问题」。
    """
    (tmp_path / "bad.yaml").write_text(
        "- id: r1\n  title: 缺 worker_type\n  category: x\n  body: b\n"
        "- id: r2\n  title: t\n  worker_type: 不存在的类型\n  category: x\n  body: b\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleCorpusError) as exc:
        load_rules(tmp_path)
    message = str(exc.value)
    assert "r1" in message and "r2" in message
    assert "worker_type" in message


@pytest.mark.unit
def test_duplicate_ids_across_files_are_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        "- id: same\n  title: t\n  worker_type: security\n  category: sqli\n  body: b\n",
        encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        "- id: same\n  title: t\n  worker_type: security\n  category: sqli\n  body: b\n",
        encoding="utf-8",
    )
    with pytest.raises(RuleCorpusError, match="重复"):
        load_rules(tmp_path)


# --------------------------------------------------------------------------- #
# 检索
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_core_rules_come_first_and_are_never_dropped(corpus: RuleSet) -> None:
    spec = spec_for("security")
    picked = select_rules(corpus, worker_type=spec.worker_type, core_ids=spec.core_rule_ids, top_k=2)
    assert [r.id for r in picked[: len(spec.core_rule_ids)]] == list(spec.core_rule_ids)


@pytest.mark.unit
def test_top_k_is_respected(corpus: RuleSet) -> None:
    picked = select_rules(corpus, worker_type=WorkerType.SECURITY, top_k=3)
    assert len(picked) == 3


@pytest.mark.unit
def test_selection_order_is_deterministic(corpus: RuleSet) -> None:
    """顺序必须稳定 —— 每次调用换一个顺序会让前缀缓存永远失效，
    而缓存单价差约十倍，且这件事在功能上完全看不出来。
    """
    first = select_rules(corpus, worker_type=WorkerType.SECURITY, top_k=6)
    second = select_rules(corpus, worker_type=WorkerType.SECURITY, top_k=6)
    assert [r.id for r in first] == [r.id for r in second]


@pytest.mark.unit
def test_language_filter_excludes_other_languages_but_keeps_agnostic_rules(
    tmp_path: Path,
) -> None:
    """语言过滤是**降级而非排除**：``languages`` 为空的规则与语言无关，
    永远保留。把它们一起过滤掉，会让通用规则在几乎所有调用里消失。
    """
    (tmp_path / "r.yaml").write_text(
        "- id: py-only\n  title: t\n  worker_type: security\n  category: sqli\n"
        "  languages: [python]\n  body: b\n"
        "- id: go-only\n  title: t\n  worker_type: security\n  category: sqli\n"
        "  languages: [go]\n  body: b\n"
        "- id: agnostic\n  title: t\n  worker_type: security\n  category: sqli\n  body: b\n",
        encoding="utf-8",
    )
    ruleset = load_rules(tmp_path)
    picked = {r.id for r in select_rules(ruleset, worker_type=WorkerType.SECURITY, language="python")}
    assert picked == {"py-only", "agnostic"}


@pytest.mark.unit
def test_select_ignores_unknown_ids_instead_of_raising(corpus: RuleSet) -> None:
    """``core_rule_ids`` 里有一个悬空 id 时，检索不该崩 ——
    但那条规则也确实拿不到（这个不一致由上面那条测试负责抓出来）。"""
    picked = select_rules(corpus, worker_type=WorkerType.SECURITY, core_ids=["不存在的规则"], top_k=2)
    assert all(r.id != "不存在的规则" for r in picked)
