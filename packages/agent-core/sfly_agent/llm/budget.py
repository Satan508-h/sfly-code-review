"""线上成本闸 —— **超预算就换成扫描器，而不是报错。**

公网上放一个真花钱的服务，第一件要回答的事是「最多花多少」。这个模块就是
那个答案：每次调用真实模型之前先问一句「今天还能花吗」，不能就改用
``MockLLM``（确定性正则扫描器，零成本），并在结果上留下痕迹。

### 为什么是「降级」而不是「拒绝」

拒绝的表现是访客点开链接、看到 429 或者一份空报告 —— 而配额用尽是
**运维状态**，不是访客的错。降级之后流程照样跑完、时间线照样有 12 条事件、
报告照样有 findings，只是来源从模型换成了扫描器；报告里 ``scanned_only``
为真，前端显示另一种徽章。**访客看到的东西是诚实的**，这是唯一的要求。

### 为什么判据是 LLM 调用次数，不是「审查次数」

闸必须卡在**真正花钱的那一行**（``complete()``）上，因为那是唯一一个
「所有花钱的路径都会经过」的位置 —— 包括我们自己反复跑评测、GitHub 重投
webhook、CLI 手动跑单次审查。按「审查次数」在入口处卡，挡不住这些。
一次审查三个 Worker，所以调用预算是审查配额的三倍（见 ``.env.example``）。

### 为什么阈值读的是数据库而不是内存

精简模式跑在 Render 免费档上，**15 分钟没人访问就休眠、下次访问重新拉起
一个进程**。进程内计数器每天会被重置几十次，于是「每日上限」看起来在保护、
实际不保护 —— 而它失败的方向是**多花钱**，没有任何东西会报警。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from sfly_agent.llm.base import LLMProvider, LLMResponse
from sfly_shared.config import Settings
from sfly_shared.contracts import LlmSpend
from sfly_shared.logging import get_logger

log = get_logger(__name__)


class SpendLedger(Protocol):
    """问「从某个时刻起花了多少」。

    ``PostgresRunStore`` 结构上就是它 —— 这里用 Protocol 而不是直接依赖
    ``sfly_bus``，因为 ``sfly_agent`` 不该为了一个查询拖进整个传输层
    （依赖方向：agent-core 只知道契约，不知道队列和仓储）。
    """

    async def llm_spend_since(self, since: datetime) -> LlmSpend: ...


def start_of_day_utc(now: datetime | None = None) -> datetime:
    """今天（UTC）的零点。

    **UTC 而不是本地时区**：部署在哪个区域是 Render 的设置，改一次就会让
    「今天」平移几个小时 —— 而那个平移只在某天的边界上看得出来，排查时会
    先怀疑计数器坏了。
    """
    moment = now or datetime.now(UTC)
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


class BudgetedLLM:
    """真实 provider 外面套一层「今天还花得起吗」。

    形状和 :class:`~sfly_agent.llm.registry.FallbackLLM` 一样，但**触发条件
    完全不同**，两者不能合并：

    * ``FallbackLLM`` 管的是「**这一次**调用失败了」（网络、超时、坏 JSON），
      它需要 ``ALLOW_MOCK_FALLBACK=true`` 才开，因为把失败悄悄吞掉就是造假。
    * 这里管的是「**今天**不能再花了」—— 它和这一次调用成不成功无关，
      而且它在线上**默认开着**（不然就没有成本上限）。

    降级在结果上留痕的方式也一致：``model`` 后面缀上原因
    （``mock-1 (budget: 45/45 calls today)``）。那个字符串会一路进
    ``worker_results.model`` → ``llm_calls.model`` → 报告，所以事后能查出
    「哪几份结果是扫描器产的」。
    """

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider,
        *,
        ledger: SpendLedger,
        settings: Settings,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._ledger = ledger
        self._settings = settings
        # 名字和模型取主 provider 的：健康页和日志里显示的应该是「本来打算用
        # 什么」，降级是每次调用各自的属性，不是这个对象的属性。
        self.name = primary.name
        self.model = primary.model

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        reason = await self._over_budget()
        if reason is None:
            return await self._primary.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )
        response = await self._fallback.complete(
            system=system, user=user, max_tokens=max_tokens, temperature=temperature
        )
        return replace(response, model=f"{response.model} (budget: {reason})")

    async def aclose(self) -> None:
        closer = getattr(self._primary, "aclose", None)
        if closer is not None:
            await closer()

    async def _over_budget(self) -> str | None:
        """返回超预算的原因（作为降级标记），还能花就返回 ``None``。"""
        try:
            spend = await self._ledger.llm_spend_since(start_of_day_utc())
        except Exception:
            # **查不到就按超预算处理（fail closed）。** 方向是刻意的：
            # 闸的目的是「最多花这么多」，而数据库查不通的时候放行，
            # 恰好会在最不该花钱的时刻（基础设施半死）把钱花掉。
            # 降级的代价只是一份来自扫描器的报告，而它是诚实的。
            log.exception(
                "llm.budget_check_failed",
                hint="查不到今日花费 —— 按超预算处理（宁可降级，不要失控花钱）",
            )
            return "check failed"

        budget_calls = self._settings.daily_llm_call_budget
        budget_usd = self._settings.daily_llm_cost_budget_usd
        if budget_calls > 0 and spend.calls >= budget_calls:
            log.warning(
                "llm.budget_exhausted",
                spent_calls=spend.calls,
                budget_calls=budget_calls,
                spent_usd=round(spend.cost_usd, 4),
                hint="今日调用次数用完 —— 本次改用扫描器，报告里会标注",
            )
            return f"{spend.calls}/{budget_calls} calls today"
        if budget_usd > 0 and spend.cost_usd >= budget_usd:
            log.warning(
                "llm.budget_exhausted",
                spent_usd=round(spend.cost_usd, 4),
                budget_usd=budget_usd,
                spent_calls=spend.calls,
                hint="今日成本用完 —— 本次改用扫描器，报告里会标注",
            )
            return f"${spend.cost_usd:.2f}/${budget_usd:.2f} today"
        return None
