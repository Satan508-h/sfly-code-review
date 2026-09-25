"""LLM 抽象层 —— 一次补全调用的最小接口。

**为什么要有这层**：``LLM_PROVIDER=mock`` 必须能在不起任何外部服务的前提下
跑通全链路（开发和 CI 都靠它），而评测又必须在真实模型上跑。两者要是走不同的
代码路径，那评测测的就不是线上跑的东西了。所以差异只允许存在于 ``complete()``
的实现里，调用方拿到的东西逐字相同。

接口刻意**只有一次补全**，没有流式、没有多轮、没有工具调用：
批处理审查不需要逐 token 流式输出，而工具调用会引入非确定性的循环次数 ——
两者都会直接毁掉评测的可复现性。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """一次补全的结果。

    除了文本之外全部是**成本与可观测性字段** —— 它们是评测报告里每个 token
    数字的来源，所以从第一个 provider 开始就必须如实填写。Mock 也要填，
    而且填的是「如果换成真模型大概会花多少」，否则本地跑出来的成本报告全是 0。
    """

    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    #: 命中上下文缓存的输入 token。DeepSeek 的自动前缀缓存和 OpenAI 的
    #: prompt caching 都归到这里。它与 ``tokens_in`` 是**包含**关系，不是相加。
    cached_tokens: int = 0
    latency_ms: int = 0
    finish_reason: str | None = None

    @property
    def truncated(self) -> bool:
        """因为达到 ``max_tokens`` 而被截断。

        这个属性直接决定修复阶梯怎么走：截断的响应**一定**不是合法 JSON，
        但它里面可能有若干条完整可用的 finding。把它当成「解析失败」丢掉，
        代价是整条 finding 列表，而不是被截断的那一条。
        """
        return self.finish_reason == "length"

    @property
    def cache_hit_rate(self) -> float:
        return self.cached_tokens / self.tokens_in if self.tokens_in else 0.0


@runtime_checkable
class LLMProvider(Protocol):
    """一个 LLM 后端。

    ``name`` 用于日志和健康检查回显，``model`` 会写进 ``WorkerResult.model``
    （评测要按模型分组对比结果）。
    """

    name: str
    model: str

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        """发一次补全请求。

        失败时抛 ``LlmTimeoutError`` / ``LlmHttpError``（见 ``sfly_shared.errors``），
        **不要**返回一个 ``text=""`` 的响应假装成功 —— 上层靠异常类型决定重试策略，
        一个空响应会被当成「模型认为没有问题」，那是最坏的一种静默失败。
        """
        ...


def estimate_tokens(text: str) -> int:
    """粗略估算 token 数。

    只在 Mock 里用。**故意用「约 4 字符 1 token」这种糙算法**：它的用途是让
    本地跑出来的成本报告有正确的数量级，不是精确计费。中文实际约 1.5 字符 1 token，
    所以真实模型返回的 usage 一定和这个估算对不上 —— 那也没关系，
    有真实 usage 时一律以它为准。
    """
    return max(1, len(text) // 4)
