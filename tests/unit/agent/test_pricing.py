"""成本换算 —— 纯函数。

这个文件里最值钱的一条是 :func:`test_cached_tokens_are_not_counted_twice`。
缓存口径写错不会报错，只会让账单偏高 —— 而偏高没有任何东西会报警。
"""

from __future__ import annotations

import pytest

from sfly_agent.llm.pricing import FREE, UNKNOWN, estimate_cost_usd, price_for

pytestmark = pytest.mark.unit


def test_mock_is_genuinely_free() -> None:
    """Mock 的零价是**真的 0**，不是「还没填」。

    把它算成钱会让本地开发和 CI 的成本列变成一串假数字，
    而那些数字会被人拿去和线上比。
    """
    assert price_for("mock-1") is FREE
    assert estimate_cost_usd("mock-1", tokens_in=10_000, tokens_out=5_000, cached_tokens=0) == 0.0


def test_an_unknown_model_costs_nothing_rather_than_a_guess() -> None:
    """认不出的模型返回零价。

    猜一个数字的话，它会一路进评测报告，而**报告里没人看得出哪几个数是真的**。
    """
    assert price_for("some-model-from-2027") is UNKNOWN
    assert (
        estimate_cost_usd("some-model-from-2027", tokens_in=1_000_000, tokens_out=0, cached_tokens=0) == 0.0
    )


def test_cached_tokens_are_not_counted_twice() -> None:
    """``tokens_in`` 是**总数**，缓存命中的那部分已经在里面了。

    写成 ``tokens_in × 未命中价 + cached × 命中价`` 会把命中的那部分算两遍，
    而账单只会偏高。DeepSeek 的命中价约为未命中的 1/4，
    在长提示词下这个错误能放大到 30%。

    这里用价格本身来验证：两万 token 全部命中，应当正好等于
    「两万 token × 命中价」。
    """
    price = price_for("deepseek")
    all_cached = estimate_cost_usd("deepseek", tokens_in=1_000_000, tokens_out=0, cached_tokens=1_000_000)

    assert all_cached == pytest.approx(price.cached_input_per_mtok)
    # 真正的守门断言：全部命中一定**严格便宜于**全部未命中。
    all_uncached = estimate_cost_usd("deepseek", tokens_in=1_000_000, tokens_out=0, cached_tokens=0)
    assert all_cached < all_uncached


def test_output_tokens_are_priced_separately() -> None:
    price = price_for("deepseek")
    cost = estimate_cost_usd("deepseek", tokens_in=0, tokens_out=1_000_000, cached_tokens=0)

    assert cost == pytest.approx(price.output_per_mtok)
    assert price.output_per_mtok > price.input_per_mtok


def test_cached_tokens_above_tokens_in_do_not_produce_a_negative_cost() -> None:
    """provider 的记账 bug 或者我们读错字段时，不能让成本变成负数 ——
    一个负成本比一个偏高的成本更难解释。"""
    cost = estimate_cost_usd("deepseek", tokens_in=100, tokens_out=0, cached_tokens=999_999)

    assert cost >= 0.0


def test_negative_tokens_are_clamped() -> None:
    assert estimate_cost_usd("deepseek", tokens_in=-5, tokens_out=-5, cached_tokens=-5) == 0.0


def test_the_lookup_is_cached_but_not_stale_per_call() -> None:
    """``lru_cache`` 只是让调用点不必关心它有多便宜 —— 它不该改变结果。"""
    assert price_for("deepseek-flash") is price_for("deepseek-chat")
    assert price_for(None) is UNKNOWN
    assert price_for("") is UNKNOWN
