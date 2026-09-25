"""模型价格表 —— **成本是一条账单，不是一次估算**。

用途只有一个：把 provider 返回的 ``usage`` 换算成钱，写进 ``llm_calls`` 表。
评测报告里的「单 PR 成本」是整个表 SUM 出来的，不在这里算。

### 三件容易写错的事

**1. 缓存命中的 token 已经在 ``tokens_in`` 里了。**
OpenAI 兼容接口的 ``prompt_tokens`` 是**总数**，``prompt_cache_hit_tokens``
是其中命中的那部分。所以计费是

    (tokens_in - cached) × 未命中价 + cached × 命中价 + tokens_out × 输出价

写成 ``tokens_in × 未命中价 + cached × 命中价`` 会把命中的那部分算两遍 ——
而账单只会偏高，偏高没有任何东西会报警。DeepSeek 的命中价约为未命中的
1/4，这个错误在长提示词下能放大到 30%。

**2. 价格是有保质期的，和模型 id 一样。**
下面这些数字是**写下来那一刻**的公开价格。降价、涨价、改计费口径都发生过。
所以 :func:`price_for` 认不出模型时**返回零价**而不是猜一个 ——
猜出来的数字会一路进评测报告，而报告里没人看得出哪几个数是真的。

**3. ``mock`` 的价格是 0，那是真的 0。**
不是「还没填」：Mock 不产生任何调用，把它算成钱会让本地开发和 CI 的
成本列变成一串假数字，而那些数字会被人拿去和线上比。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

#: 每百万 token 的价格（美元）。
_TOKENS_PER_MTOK = 1_000_000


@dataclass(frozen=True, slots=True)
class Price:
    """一个模型的单价。三个数分别对应「未命中输入 / 命中输入 / 输出」。"""

    input_per_mtok: float
    cached_input_per_mtok: float
    output_per_mtok: float


#: 免费。**Mock 和真实模型走同一条记账路径**，只是单价为零 ——
#: 这样「成本记账有没有在工作」在本地就能验证（数字是 0，但行数是真的）。
FREE = Price(0.0, 0.0, 0.0)

#: 认不出的模型用它。见模块文档第 2 条。
UNKNOWN = FREE

#: ``(模型 id 的子串, 单价)``。**按顺序匹配，第一个命中的赢** ——
#: 所以更具体的 id 要写在前面（``gpt-4o-mini`` 必须在 ``gpt-4o`` 之前）。
_PRICES: tuple[tuple[str, Price], ...] = (
    ("mock", FREE),
    ("gpt-4o-mini", Price(0.15, 0.075, 0.60)),
    ("deepseek", Price(0.27, 0.07, 1.10)),
)


@lru_cache(maxsize=32)
def price_for(model: str | None) -> Price:
    """按模型 id 查单价。查不到返回 :data:`UNKNOWN`（零价）。

    ``lru_cache`` 不是优化，是**让调用点不必关心这件事有多便宜** ——
    它在每条结果的写入路径上，不该有人因为「查表会不会慢」而把它挪走。
    """
    if not model:
        return UNKNOWN
    lowered = model.lower()
    for needle, price in _PRICES:
        if needle in lowered:
            return price
    return UNKNOWN


def estimate_cost_usd(
    model: str | None,
    *,
    tokens_in: int,
    tokens_out: int,
    cached_tokens: int,
) -> float:
    """一次调用的成本（美元）。见模块文档第 1 条关于 ``tokens_in`` 的口径。

    ``cached_tokens`` 超过 ``tokens_in`` 时按 ``tokens_in`` 夹住：
    那种情况只可能来自 provider 的记账 bug 或者我们自己读错字段，
    而让它产生一个**负数**的未命中量会把整条 run 的成本拉低 ——
    一个负成本比一个偏高的成本更难解释。
    """
    price = price_for(model)
    cached = min(max(cached_tokens, 0), max(tokens_in, 0))
    uncached = max(tokens_in, 0) - cached

    return (
        uncached * price.input_per_mtok
        + cached * price.cached_input_per_mtok
        + max(tokens_out, 0) * price.output_per_mtok
    ) / _TOKENS_PER_MTOK
