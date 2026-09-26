"""线上成本闸 —— **超预算换扫描器，不报错**。

这个文件是「公网上的服务最多花多少钱」那句话的证据。四件事各自都要钉住：

* **超预算时真实 provider 一次都不能被调用。** 断言的是「没有被调用」而不是
  「结果来自 Mock」—— 后者在闸装反了的时候同样成立（先调用、再降级），
  而那时钱已经花掉了。
* **查不到花费时按超预算处理**（fail closed）。反过来的方向会在最不该花钱的
  时刻（基础设施半死）放行。
* **降级必须留痕**。模型名后缀是事后唯一能查出「哪几份结果是扫描器产的」
  东西 —— 报告里的 ``scanned_only`` 也读它。
* **``ENABLE_REAL_LLM=false`` 是总闸**，但**不能**顺手把「配了真实 provider
  却没配密钥」这个错误也吞掉：那是配置错误，要在启动第一秒炸掉（见
  ``MissingApiKeyError`` 的文档）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from sfly_agent.llm.base import LLMResponse
from sfly_agent.llm.budget import BudgetedLLM, start_of_day_utc
from sfly_agent.llm.mock import MockLLM, is_mock_model
from sfly_agent.llm.registry import MissingApiKeyError, build_llm
from sfly_shared.config import Settings
from sfly_shared.contracts import LlmSpend

pytestmark = pytest.mark.unit


class _Provider:
    """数自己被打过几次。**计数是这里唯一重要的东西** —— 见模块文档第一条。"""

    def __init__(self, *, model: str, name: str = "real") -> None:
        self.name = name
        self.model = model
        self.calls = 0

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        self.calls += 1
        return LLMResponse(text="{}", model=self.model, tokens_in=10, tokens_out=5)


class _Ledger:
    """假的账本。``boom`` 用来演「数据库查不通」。"""

    def __init__(self, spend: LlmSpend | None = None, *, boom: bool = False) -> None:
        self.spend = spend or LlmSpend()
        self.boom = boom
        self.asked: list[datetime] = []

    async def llm_spend_since(self, since: datetime) -> LlmSpend:
        self.asked.append(since)
        if self.boom:
            raise RuntimeError("数据库连不上（假装的）")
        return self.spend


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "llm_provider": "deepseek",
        "llm_api_key": "fake-key",
        "daily_llm_call_budget": 3,
        "daily_llm_cost_budget_usd": 1.0,
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def _budgeted(
    *, spend: LlmSpend, boom: bool = False, **overrides: object
) -> tuple[BudgetedLLM, _Provider, _Provider]:
    real = _Provider(model="deepseek-chat", name="deepseek")
    scanner = _Provider(model="mock-1", name="mock")
    llm = BudgetedLLM(real, scanner, ledger=_Ledger(spend, boom=boom), settings=_settings(**overrides))
    return llm, real, scanner


async def _complete(llm: BudgetedLLM) -> LLMResponse:
    return await llm.complete(system="s", user="u")


# --------------------------------------------------------------------------- #
# 闸的门槛
# --------------------------------------------------------------------------- #


async def test_under_budget_the_real_provider_is_used() -> None:
    """没到上限就正常花。**这条是防「闸装得太紧」的** ——
    一个永远降级的成本闸在指标上和「代码根本跑不通」无法区分。"""
    llm, real, scanner = _budgeted(spend=LlmSpend(calls=2, cost_usd=0.01))

    response = await _complete(llm)

    assert real.calls == 1
    assert scanner.calls == 0
    assert response.model == "deepseek-chat", "没降级就不该有后缀"


async def test_over_the_call_budget_the_real_provider_is_never_called() -> None:
    """到了次数上限 —— 真实 provider **一次都不能被碰**。"""
    llm, real, scanner = _budgeted(spend=LlmSpend(calls=3, cost_usd=0.001))

    response = await _complete(llm)

    assert real.calls == 0, "先调用再降级 = 钱已经花了，闸就白装了"
    assert scanner.calls == 1
    assert response.model == "mock-1 (budget: 3/3 calls today)"


async def test_over_the_cost_budget_the_real_provider_is_never_called() -> None:
    """到了金额上限。**和次数是两道独立的闸** —— 单价高的模型可能几次就超钱。"""
    llm, real, scanner = _budgeted(
        spend=LlmSpend(calls=1, cost_usd=1.5),
        daily_llm_call_budget=100,
        daily_llm_cost_budget_usd=1.0,
    )

    response = await _complete(llm)

    assert real.calls == 0
    assert scanner.calls == 1
    assert "$1.50/$1.00" in response.model


async def test_a_zero_budget_means_no_limit_not_no_spending() -> None:
    """``0`` 是「不限制」而不是「一次都不许」。

    这条值得单独钉住，因为两种理解都说得通，而选错的那个方向是
    **服务完全不工作**（配额写 0 之后一个真实调用都发不出去），
    且症状是「模型好像坏了」而不是「配置写错了」。
    """
    llm, real, _scanner = _budgeted(
        spend=LlmSpend(calls=9999, cost_usd=999.0),
        daily_llm_call_budget=0,
        daily_llm_cost_budget_usd=0.0,
    )

    await _complete(llm)

    assert real.calls == 1


# --------------------------------------------------------------------------- #
# 账本读不到的时候
# --------------------------------------------------------------------------- #


async def test_an_unreadable_ledger_fails_closed() -> None:
    """查不到花了多少 → 按超预算处理。

    这是刻意选的方向：闸的目的是「最多花这么多」，而数据库查不通时放行，
    恰好会在基础设施半死的时候把钱花光。降级的代价只是一份来自扫描器的
    报告，而它是诚实的（``scanned_only`` 会标出来）。
    """
    llm, real, scanner = _budgeted(spend=LlmSpend(), boom=True)

    response = await _complete(llm)

    assert real.calls == 0
    assert scanner.calls == 1
    assert response.model == "mock-1 (budget: check failed)"


async def test_the_ledger_is_asked_about_the_current_utc_day() -> None:
    """问的是「今天（UTC）」，不是「最近 24 小时」。

    两者的区别在跨日那一刻：滚动窗口会让昨天的消费继续压着今天，
    而用户和运维对「每日配额」的理解都是自然日。
    """
    ledger = _Ledger()
    llm = BudgetedLLM(
        _Provider(model="deepseek-chat"), _Provider(model="mock-1"), ledger=ledger, settings=_settings()
    )

    await _complete(llm)

    assert len(ledger.asked) == 1
    assert ledger.asked[0] == start_of_day_utc()
    assert (ledger.asked[0].hour, ledger.asked[0].minute, ledger.asked[0].second) == (0, 0, 0)


# --------------------------------------------------------------------------- #
# 纯粹的判据
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        # 北京时间 9 月 26 日 23:59 == UTC 15:59，所以「今天」是 9 月 26 日
        (
            datetime(2026, 9, 26, 23, 59, tzinfo=timezone(timedelta(hours=8))),
            datetime(2026, 9, 26, tzinfo=UTC),
        ),
        # 北京时间 9 月 27 日 00:01 == UTC 9 月 26 日 16:01，仍是 9 月 26 日
        (datetime(2026, 9, 27, 0, 1, tzinfo=timezone(timedelta(hours=8))), datetime(2026, 9, 26, tzinfo=UTC)),
        # 北京时间 9 月 27 日 08:00 == UTC 9 月 27 日 00:00，翻篇了
        (datetime(2026, 9, 27, 8, 0, tzinfo=timezone(timedelta(hours=8))), datetime(2026, 9, 27, tzinfo=UTC)),
    ],
)
def test_the_daily_window_is_a_utc_calendar_day(now: datetime, expected: datetime) -> None:
    assert start_of_day_utc(now) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("mock-1", True),
        ("mock-1-fallback", True),
        ("mock-1 (budget: 3/3 calls today)", True),
        ("mock-1-fallback (fallback from deepseek)", True),
        ("deepseek-chat", False),
        # 「不知道」不是「扫描器」—— 没填模型名不该让报告自称是扫描器产的
        (None, False),
        ("", False),
    ],
)
def test_only_scanner_models_count_as_scanner_models(model: str | None, expected: bool) -> None:
    assert is_mock_model(model) is expected


# --------------------------------------------------------------------------- #
# 接线：注册表
# --------------------------------------------------------------------------- #


def test_the_registry_wraps_the_provider_when_a_ledger_is_given() -> None:
    """给了账本就装闸，没给就不装 —— 这是 ``build_llm`` 的接线约定。

    没给的那条路（本地 CLI、评测、单测）本来就不花钱或有人盯着，
    所以不该因为它没装闸就报错。
    """
    ledger = _Ledger()

    assert isinstance(build_llm(_settings(), ledger=ledger), BudgetedLLM)
    assert not isinstance(build_llm(_settings()), BudgetedLLM)


def test_mock_is_never_wrapped_in_a_budget_gate() -> None:
    """Mock 的单价是零，包一层闸只会让每次调用多一次数据库查询。"""
    llm = build_llm(_settings(llm_provider="mock"), ledger=_Ledger())

    assert isinstance(llm, MockLLM)
    assert not isinstance(llm, BudgetedLLM)


def test_disabling_real_llm_yields_a_scanner_without_raising() -> None:
    """``ENABLE_REAL_LLM=false`` 返回扫描器而不是抛异常 —— 它表达的是
    「照样能演示，只是一分钱不花」，而抛出去的表现是 Worker 起不来。"""
    llm = build_llm(_settings(enable_real_llm=False))

    assert isinstance(llm, MockLLM)
    assert llm.name == "mock"


def test_disabling_real_llm_does_not_swallow_a_missing_api_key() -> None:
    """**总闸管的是「要不要花」，不是「配置对不对」。**

    配了 deepseek 却忘了密钥仍然是启动错误（``MissingApiKeyError`` 的文档
    写得很清楚：重启一万次也不会变，所以要在第一秒炸掉）。如果总闸顺手把
    它吞掉，那么在 ``ENABLE_REAL_LLM=false`` 期间部署一个密钥没配好的实例
    会静默成功，然后在你打开总闸的那一刻开始每个任务都失败。
    """
    with pytest.raises(MissingApiKeyError):
        build_llm(_settings(llm_api_key=""))
