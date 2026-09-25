"""阻断决策 —— **规则引擎，不用 LLM**。

和冲突消解是同一个理由：LLM 裁判引入非确定性，直接毁掉评测的可复现性。
一个「这次阻断了、下次没阻断」的合并门禁，比一个偏保守的门禁糟糕得多 ——
人很快就会学会忽略它。

### 永不发 APPROVE

机器人审批人类 PR 是一个策略漏洞：一个被绕过的模型（prompt 注入、
规则库没覆盖的类目、恰好没发现的 bug）会拿到一个绿色的通过标记，
而那个标记的权威性和人工审批长得一模一样。

所以这个模块的输出只有两种：``block_merge=True``（对应 REQUEST_CHANGES）
或者 ``False``（对应 COMMENT）。**没有第三种。**

### 策略是可配置的，规则不是

M5 把阈值写在这里；M9 会把它们搬进 ``ReviewPolicy`` 表（按仓库可覆盖）。
搬的只是**数据的存放位置**，这套顺序匹配的结构不变 —— 顺序匹配是关键，
因为它让 ``decision_reason`` 能精确说出「哪一条规则触发了」，
而那是人在追问「为什么拦我这个 PR」时唯一想知道的。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import NamedTuple

from sfly_shared.contracts import AggregatedFinding, Finding, Severity

#: 命中这些类目的一条 ``critical`` 就阻断，不需要看条数。
#: 它们共同的特点是「一旦成立，代价不可逆」：凭据泄露、注入、越权。
#:
#: M9 会把它搬进 ``ReviewPolicy`` 表（按仓库覆盖）。现在写成常量而不是
#: 环境变量：环境变量适合**部署差异**，而这些是**审查策略** ——
#: 它们该和代码一起被评审、被 git 记录。
BLOCKING_CATEGORIES: frozenset[str] = frozenset(
    {
        "secrets",
        "sqli",
        "command_injection",
        "deserialization",
        "path_traversal",
        "ssrf",
        "xxe",
    }
)

#: 单条 critical 的阻断门槛。比 ``SUPPRESS_THRESHOLD``（0.35）高得多 ——
#: 「值得说一句」和「值得拦住这一次合并」是两件事。
CRITICAL_BLOCK_CONFIDENCE = 0.60

#: 多条高危的阻断门槛与条数。
HIGH_SEVERITY_BLOCK_CONFIDENCE = 0.70
HIGH_SEVERITY_BLOCK_COUNT = 3


class Decision(NamedTuple):
    """``reason`` 是短 slug 而不是句子：它进数据库（``review_runs.decision_reason``
    没有这一列，但 ``review_reports.report`` 里有），要能被统计和分组。
    人读的长句由 ``render.py`` 从 slug 生成。"""

    block_merge: bool
    reason: str


#: slug → 人读的中文说明。``render.py`` 用它，测试也用它（保证每个 slug 都有说明）。
REASON_TEXT: dict[str, str] = {
    "secrets_found": "发现凭据泄露：这类问题一旦合入就可能已经不可逆（密钥需要轮换）",
    "blocking_critical": "发现高危类目的严重问题，且置信度足够高",
    "too_many_high": f"高危及以上问题的数量达到 {HIGH_SEVERITY_BLOCK_COUNT} 条",
    "below_threshold": "没有达到阻断门槛，仅供作者参考",
}


def decide(findings: Sequence[AggregatedFinding | Finding]) -> Decision:
    """按顺序匹配四条规则，返回第一条命中的。

    **只看 ``findings``（要发布的那些）**，不看 ``suppressed`` ——
    被置信度闸砍掉的发现既不该展示也不该阻断合并，否则那道闸就形同虚设。
    """
    if not findings:
        return Decision(block_merge=False, reason="below_threshold")

    # 规则 1：凭据泄露。**不看严重度也不看置信度** ——
    # 一条低置信度的 secrets 声称仍然值得拦住，因为代价不对称：
    # 误报的代价是作者多看一眼，漏报的代价是密钥进了 git 历史。
    if any(f.category == "secrets" for f in findings):
        return Decision(block_merge=True, reason="secrets_found")

    # 规则 2：高危类目的 critical。
    if any(
        f.severity is Severity.CRITICAL
        and _confidence(f) >= CRITICAL_BLOCK_CONFIDENCE
        and f.category in BLOCKING_CATEGORIES
        for f in findings
    ):
        return Decision(block_merge=True, reason="blocking_critical")

    # 规则 3：高危问题扎堆。单看每一条都不足以阻断，但它们同时出现时，
    # 「这个 PR 需要人认真看一遍」这件事本身是确定的。
    serious = [
        f
        for f in findings
        if f.severity in (Severity.CRITICAL, Severity.HIGH)
        and _confidence(f) >= HIGH_SEVERITY_BLOCK_CONFIDENCE
    ]
    if len(serious) >= HIGH_SEVERITY_BLOCK_COUNT:
        return Decision(block_merge=True, reason="too_many_high")

    return Decision(block_merge=False, reason="below_threshold")


def _confidence(f: AggregatedFinding | Finding) -> float:
    """聚合后的用重算值，没聚合过的用模型自报值。

    写成 ``getattr`` 而不是统一转成 ``AggregatedFinding``：``decide`` 在
    测试里会被直接喂 ``Finding``（纯函数的好处），而为一个可选字段做一次
    ``model_validate`` 是不必要的开销。
    """
    adjusted = getattr(f, "adjusted_confidence", None)
    return float(adjusted) if adjusted is not None else float(f.confidence)


def decision_text(reason: str) -> str:
    """slug → 人读说明。查不到时**回显 slug 本身**而不是「未知原因」——
    回显至少能让人去 grep，而「未知」只能让人来问作者。

    和 ``REASON_TEXT`` 放在同一个文件：两者必须一起改，分开就会漂移
    （新增一条规则却忘了写说明，症状是评论里出现一个英文 slug）。
    """
    return REASON_TEXT.get(reason, reason)
