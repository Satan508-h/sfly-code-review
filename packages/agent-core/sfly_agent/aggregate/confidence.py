"""置信度重算 —— **全确定性，零 LLM 调用**。

模型自报的 ``confidence`` 有两个已知偏差，两个都不是它的错：

* **系统性偏高**：让它给一个 0-1 的数，它给的是「我有多想说这句话」，
  不是「这句话有多大概率是对的」。
* **跨 Worker 不可比**：安全 Worker 和风格 Worker 对 0.8 的理解不一样，
  于是把两边的数字放在一张表里排序，排的是措辞风格而不是可信度。

所以这里丢掉它的一部分权重，换成三样**可观测**的东西：

    c = SEV_PRIOR[severity] × (0.55 × c_llm + 0.25 × agreement + 0.20 × diversity)
        + 0.10 × grounded

* ``agreement``（一致度）—— 有几个来源报了同一件事。三个 Worker 独立地报出
  同一处问题，是最强的证据，而它恰好是「多 Agent」这个设计唯一能产出的东西。
* ``diversity``（跨 Worker 度）—— 是同一个 Worker 报了两遍，还是两个不同
  Worker 各报了一遍？后者值钱得多。
* ``grounded``（有据）—— 命中了手写规则库里的某一条。这是二值的：
  要么找到了一条白纸黑字的规范，要么没有。

``SEV_PRIOR`` 在契约层（``sfly_shared.contracts``），因为严重度排序和先验
是同一套秩序的两个侧面，放在一起才不会分叉。

### 阈值 0.35 之后做什么

低于 :data:`SUPPRESS_THRESHOLD` 的发现**入库但不发布**。
「入库」是关键：评测需要它们来测量这道闸砍掉了多少召回 —— 只存发布出去的，
阈值就只能盲调，而盲调出来的阈值没有说服力。
"""

from __future__ import annotations

from math import exp

from sfly_shared.contracts import SEVERITY_PRIOR, Finding, WorkerType

#: 低于它就只入库、不发布。见模块文档最后一段。
#:
#: ### 0.35 是**还没有被数据支持**的一个数，M9 量过了
#:
#: 这个值是最早拍下来的，当时没有任何评测集。M9 的离线层跑出来之后
#: （``reports/eval-<sha>.md`` 里的阈值扫描）它被 0.30 支配：
#:
#: | 阈值 | 严格精确率 | 严格召回率 |
#: |---|---|---|
#: | 0.20 | 88.0% | 95.7% |
#: | 0.30 | 95.0% | 82.6% |
#: | **0.35（当前）** | 94.7% | 78.3% |
#: | 0.40 | 100.0% | 60.9% |
#:
#: **但仍然没改**，理由是这个扫描跑在 Mock 上：Mock 的置信度是每条正则规则
#: 手写的常数（0.4–0.9），而真实模型自报的置信度分布完全是另一回事
#: （模型偏爱 0.8–0.9）。拿一批手写常数去定一个用来读模型自报值的门槛，
#: 是**用错误的分布调参数** —— 换到真实层上大概率是错的，
#: 而且换错了不会有任何信号。
#:
#: 所以它保持不动，等真实层的扫描（``python tasks.py eval --real``）出来再定。
#: 在那之前，上面这张表就是这个数字的**已知缺陷**，不是「还没量」。
SUPPRESS_THRESHOLD = 0.35

#: 三个权重之和必须是 1.0 —— 否则 ``SEV_PRIOR`` 那个乘子就不再是「先验」，
#: 而这个公式也就没有可解释性了（它不再是一个加权平均）。
_LLM_WEIGHT = 0.55
_AGREEMENT_WEIGHT = 0.25
_DIVERSITY_WEIGHT = 0.20

#: 命中规则库的二值加成。**在括号之外**（先算先验加权的部分，再加这一项）：
#: 它的含义是「有一条白纸黑字的规范支持它」，与严重度无关 ——
#: 一条 INFO 的发现命中了规则，也应该因此更可信。
_GROUNDED_BONUS = 0.10

#: 一致度的衰减尺度。``1 - exp(-(n-1)/1.5)`` 在这三档上是
#: ``1 个来源 → 0``、``2 个 → 0.49``、``3 个 → 0.74``。
#: 取 1.5 而不是 1.0，是为了让「第二个来源」的边际收益明显小于「第一个」——
#: 从 0 到 1 个印证是最值钱的一步，之后迅速饱和。
_AGREEMENT_SCALE = 1.5

#: 除 0 / 1 / 2 之外的 Worker 数不会出现（``WorkerType`` 只有三个），
#: 但公式不该假设这一点：将来加第四个 Worker 时，这个除法要自动跟上。
_MAX_CROSS_WORKERS = max(1, len(WorkerType) - 1)


def agreement_term(members: int) -> float:
    """有几个来源报了同一件事 → ``[0, 1)``。"""
    if members <= 1:
        return 0.0
    return 1.0 - exp(-(members - 1) / _AGREEMENT_SCALE)


def diversity_term(distinct_workers: int) -> float:
    """跨了几个不同的 Worker → ``[0, 1]``。"""
    if distinct_workers <= 1:
        return 0.0
    return min(1.0, (distinct_workers - 1) / _MAX_CROSS_WORKERS)


def adjusted_confidence(
    representative: Finding,
    *,
    member_count: int,
    distinct_workers: int,
    grounded: bool,
) -> float:
    """重算一条（合并后的）发现的置信度。

    ``representative`` 是簇的代表（见 ``pipeline.py`` 的选举规则），
    其余三个参数是簇的统计量。

    **为什么是三个标量而不是 ``Sequence[Finding]``**：``Finding`` 上
    没有 ``worker_type``（它属于 ``WorkerResult``），所以「有几个不同 Worker」
    这个信息在 finding 的列表里根本取不到 —— 得靠调用方把配对关系一起传进来。
    与其传一串 ``(worker_type, finding)`` 元组，不如在调用点算好三个数：
    这个公式本来就是在描述统计量，签名也应该说统计量。

    ``member_count == 1`` 是常态 —— 一个 Worker 独立报出的一条，此时公式退化成
    ``SEV_PRIOR × 0.55 × c_llm (+ grounded)``，也就是**比模型自报的更低**。
    这是刻意的：没有旁证的单条声称本来就不该有高置信度。
    """
    weighted = (
        _LLM_WEIGHT * representative.confidence
        + _AGREEMENT_WEIGHT * agreement_term(member_count)
        + _DIVERSITY_WEIGHT * diversity_term(distinct_workers)
    )
    prior = SEVERITY_PRIOR[representative.severity]
    return min(1.0, max(0.0, prior * weighted + _GROUNDED_BONUS * (1.0 if grounded else 0.0)))
