"""Provider 注册表 —— **全代码库里唯一按 ``LLM_PROVIDER`` 分支的地方。**

和 ``sfly_bus/factory.py`` 是同一条约定（CLAUDE.md 约定 #4）：一旦某个节点
开始判断「我用的是 mock 还是 deepseek」，两种拓扑/两种后端共用一条链路的
说法就废了。想加 provider 就在 ``_build_primary`` 里加一个分支，
调用方一行都不用改。

Mock 不是「测试用的降级选项」而是**默认值**。这样 clone 下来不配任何密钥
就能跑通全链路，CI 也不会因为外部服务抖动而红。
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace

from sfly_agent.llm.base import LLMProvider, LLMResponse
from sfly_agent.llm.mock import MockLLM
from sfly_agent.llm.openai_compat import OpenAICompatLLM
from sfly_shared.config import Settings, get_settings
from sfly_shared.contracts import WorkerType
from sfly_shared.errors import SflyError
from sfly_shared.logging import get_logger

log = get_logger(__name__)


class MissingApiKeyError(SflyError):
    """选了真实 provider 但没配密钥。

    **不可重试，而且刻意在构造时就抛。** 这和「数据库连不上」是两类问题：
    后者是基础设施的瞬时故障，进程应该起来、然后在健康页上说明白；
    前者是配置错误，重启一万次也不会变，所以要在启动的第一秒就炸掉 ——
    一个安静跑着但每个任务都失败的 Worker 是最难排查的形态。
    """

    retryable = False


class FallbackLLM:
    """主 provider 失败时降级到 Mock。

    **只在开发环境开**（``ALLOW_MOCK_FALLBACK=true``）。降级必须留下痕迹，
    否则就是静默造假：PR 上会出现一批看起来很合理的 review 意见，
    而它们根本不是任何模型产生的。所以降级后的响应会把 ``model`` 改写成
    ``mock-1 (fallback from deepseek)`` —— 这个字符串会一路进
    ``worker_results.model``、进评测报表、进 UI。

    注意：真正的线上降级（超预算、超配额）走的是另一条路（M10 的限流器），
    它检查的是「还能不能花钱」而不是「这一次调用有没有失败」。
    """

    def __init__(self, primary: LLMProvider, fallback: LLMProvider) -> None:
        self._primary = primary
        self._fallback = fallback
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
        try:
            return await self._primary.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )
        except SflyError as exc:
            log.warning(
                "llm.fallback_to_mock",
                primary=self._primary.name,
                error_class=exc.error_class.value,
                error=str(exc)[:300],
                note="ALLOW_MOCK_FALLBACK 已开启；产物标记为 fallback，不要当真实模型结果用",
            )
            response = await self._fallback.complete(
                system=system, user=user, max_tokens=max_tokens, temperature=temperature
            )
            return replace(response, model=f"{response.model} (fallback from {self._primary.name})")

    async def aclose(self) -> None:
        closer = getattr(self._primary, "aclose", None)
        if closer is not None:
            await closer()


def _build_primary(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "mock":
        return MockLLM(
            model=settings.resolved_llm_model,
            failure_rate=settings.mock_llm_failure_rate,
            delay_ms=settings.mock_llm_delay_ms,
        )

    if not settings.llm_api_key:
        raise MissingApiKeyError(
            f"LLM_PROVIDER={settings.llm_provider} 但没有配 LLM_API_KEY。"
            "要么在 .env 里填上密钥，要么把 LLM_PROVIDER 改回 mock（默认值，不需要任何密钥）。"
        )

    return OpenAICompatLLM(
        provider=settings.llm_provider,
        model=settings.resolved_llm_model,
        api_key=settings.llm_api_key,
        base_url=settings.resolved_llm_base_url,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        connect_timeout_s=settings.llm_connect_timeout_s,
        read_timeout_s=settings.llm_read_timeout_s,
    )


def build_llm(
    settings: Settings | None = None,
    *,
    worker_types: Collection[WorkerType] | None = None,
) -> LLMProvider:
    """按配置造一个 provider。

    ``worker_types`` 只影响 Mock：它需要知道自己是哪个 Worker 才能只报本 lane
    的问题（真实模型不需要这个提示，它的 prompt 里已经写了）。精简模式下
    一个进程跑三种 Worker，那里传 ``None`` 表示三种都报。
    """
    s = settings or get_settings()
    primary = _build_primary(s)

    if isinstance(primary, MockLLM) and worker_types is not None:
        # Mock 的 lane 过滤必须在构造时给，所以这里重建一个而不是配置它
        primary = MockLLM(
            model=primary.model,
            worker_types=worker_types,
            failure_rate=s.mock_llm_failure_rate,
            delay_ms=s.mock_llm_delay_ms,
        )

    if isinstance(primary, MockLLM) or not s.allow_mock_fallback:
        return primary

    log.warning(
        "llm.mock_fallback_enabled",
        primary=primary.name,
        note="ALLOW_MOCK_FALLBACK 已开启：真实模型失败时会产出 Mock 结果，仅限开发环境",
    )
    return FallbackLLM(primary, MockLLM(model="mock-1-fallback", worker_types=worker_types))
