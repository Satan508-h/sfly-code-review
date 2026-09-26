"""OpenAI 兼容后端的真实实现 —— DeepSeek / OpenAI / vLLM 共用这一份。

**为什么是「OpenAI 兼容」而不是「DeepSeek 客户端」**：DeepSeek 的接口就是
OpenAI 协议的一个子集，写死 DeepSeek 会让「换个 provider」变成改代码，
而它应该只是改一个环境变量。这也是 ``LLM_PROVIDER`` 能成为三个取值的东西的原因。

关于输出格式，有一个刻意的取舍：用 ``response_format={"type": "json_object"}``
而**不是** ``json_schema``。后者在 DeepSeek 上没有文档、在 OpenAI 上要让模型
进 strict 模式，两者都会把核心契约绑在一个可能变的接口上。
代价是模型可能返回结构不对的 JSON —— 这个代价由修复阶梯（``structured.py``）扛，
而不是由「换 provider 就得重写解析」扛。
"""

from __future__ import annotations

import time
from typing import Any

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, Timeout

from sfly_agent.llm.base import LLMResponse
from sfly_shared.errors import LlmHttpError, LlmTimeoutError
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 报错信息里保留的响应体长度。够看出「余额不足」和「模型名写错」的区别就行。
_BODY_SNIPPET = 300


class OpenAICompatLLM:
    """走 OpenAI 协议的 provider。实现 ``LLMProvider``。"""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str,
        base_url: str = "",
        temperature: float = 0.1,
        max_tokens: int = 4096,
        reasoning_effort: str = "",
        connect_timeout_s: int = 10,
        read_timeout_s: int = 120,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self.name = provider
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        #: 空字符串 = **不发这个参数**。见 ``complete`` 里那段说明。
        self.reasoning_effort = reasoning_effort
        self._client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None,
            # 连接超时和读超时必须分开设：一次 60 秒的审查里，连不上应当在
            # 10 秒内失败，而模型思考两分钟是正常的。一个笼统的 120 秒超时
            # 会让「DNS 挂了」表现为「模型很慢」。
            #
            # 用 ``openai.Timeout`` 而不是 ``httpx.Timeout``：openai SDK 从 3.x 起
            # 换到了 httpx2，两者的 Timeout 是不同的类。传错了**不会报错**，
            # 客户端会安静地持有一个外来对象 —— 这类 bug 只会在某个超时
            # 真的触发时才现形，而那时你根本不会怀疑到这里。
            # 从 SDK 自己那里取类型，就永远跟着它的选择走。
            timeout=Timeout(read_timeout_s, connect=connect_timeout_s),
            # **SDK 自带的重试必须关掉。** Worker 在更上层已经有重试和死信
            # 计数，两层重试叠加会得到 3×3=9 次调用和一份对不上的 attempt 计数 ——
            # 而 attempt 是死信判定的依据。
            max_retries=0,
        )

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        # ``reasoning_effort`` 只在配了的时候才发出去。**不能发一个空串** ——
        # 严格一点的 provider 会直接 400，而那是一个只在特定配置下才出现的
        # 启动期故障。空 = 不提这件事，让 provider 用它自己的默认值。
        extra: dict[str, Any] = {}
        if self.reasoning_effort:
            extra["reasoning_effort"] = self.reasoning_effort
        try:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=self.temperature if temperature is None else temperature,
                max_tokens=max_tokens or self.max_tokens,
                response_format={"type": "json_object"},
                **extra,
            )
        except APITimeoutError as exc:
            # 必须排在 APIConnectionError 前面 —— 它是后者的子类，
            # 顺序反了会让所有超时都被报成连接失败，排查方向直接跑偏。
            raise LlmTimeoutError(f"{self.name} 在读取超时内没有返回：{exc}") from exc
        except APIConnectionError as exc:
            raise LlmHttpError(f"连不上 {self.name}（{self._client.base_url}）：{exc}") from exc
        except APIStatusError as exc:
            raise LlmHttpError(_describe_status(self.name, self.model, exc)) from exc

        return self._to_response(response, started)

    def _to_response(self, response: Any, started: float) -> LLMResponse:
        choice = response.choices[0]
        usage = response.usage
        text = choice.message.content or ""
        if not text:
            # 正文一个字都没有 —— 只可能是「预算全花在思维链上」。
            # 这是 M10 实测里最难查的一段：上游看到的是「解析不出 JSON」，
            # 于是去查 JSON、查 prompt、查修复阶梯，而真正的原因在这一层。
            _warn_if_reasoning_ate_the_budget(self.name, self.model, usage, choice.finish_reason)
        return LLMResponse(
            text=text,
            model=response.model or self.model,
            tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
            tokens_out=getattr(usage, "completion_tokens", 0) or 0,
            cached_tokens=_cached_tokens(usage),
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=choice.finish_reason,
        )

    async def aclose(self) -> None:
        """关闭连接池。

        不关的话，测试里每建一个实例就漏一组 socket，跑几百个测试之后
        会开始报「too many open files」—— 一个和代码逻辑毫无关系的失败。
        """
        await self._client.close()


def _cached_tokens(usage: Any) -> int:
    """取命中前缀缓存的输入 token，兼容两种字段名。

    * DeepSeek：``usage.prompt_cache_hit_tokens``
    * OpenAI：``usage.prompt_tokens_details.cached_tokens``

    取不到就返回 0。**绝不回退成一次估算** —— 缓存命中率是要写进评测报告的数字，
    一个编出来的值比 0 有害得多。
    """
    direct = getattr(usage, "prompt_cache_hit_tokens", None)
    if isinstance(direct, int):
        return direct
    details = getattr(usage, "prompt_tokens_details", None)
    nested = getattr(details, "cached_tokens", None)
    return nested if isinstance(nested, int) else 0


def _reasoning_tokens(usage: Any) -> int:
    """思维链花掉的输出 token（``completion_tokens`` 的**子集**，不是另算的）。"""
    details = getattr(usage, "completion_tokens_details", None)
    nested = getattr(details, "reasoning_tokens", None)
    return nested if isinstance(nested, int) else 0


def _warn_if_reasoning_ate_the_budget(
    provider: str, model: str, usage: Any, finish_reason: str | None
) -> None:
    """正文是空的，而预算被思维链吃光了 —— 把原因**说出来**。

    M10 实测：``deepseek-flash`` 是推理模型，``max_tokens`` 同时管住思维链和
    正文，而它会先想后写。style 那一路在一个 4 文件的 PR 上想了 **28525 个字符**
    还没开始写正文，``completion_tokens=8192`` / ``reasoning_tokens=8192`` /
    ``content=""``。上游看到的现象是「解析不出 JSON」，于是排查方向全在
    JSON、prompt 和修复阶梯上，而真正的原因在这个字段里。

    所以这里什么都不改，只**把名字点出来** —— 一行日志就能省掉那次排查。
    """
    reasoning = _reasoning_tokens(usage)
    if not reasoning:
        return
    log.error(
        "llm.empty_content",
        provider=provider,
        model=model,
        reasoning_tokens=reasoning,
        completion_tokens=getattr(usage, "completion_tokens", 0),
        finish_reason=finish_reason,
        hint="推理模型把输出预算全用在思维链上，正文一个字都没写。"
        "把它关掉：LLM_REASONING_EFFORT=none（见 .env.example）",
    )


def _describe_status(provider: str, model: str, exc: APIStatusError) -> str:
    """把 HTTP 错误压成一行有用的信息。

    状态码必须带上，因为**同一个 provider 的不同状态码处置方式完全不同**：
    401 是密钥问题（改配置），402/429 是余额或限流（等或换），
    400/404 通常是模型名已停用或写错。只说「请求失败」等于什么都没说。

    **模型名也要带上。** 厂商会改名和下线模型 id（DeepSeek 的 ``deepseek-chat``
    别名就在 2026-07 停用了），而那总是表现为一条没头没尾的 400/404 ——
    报错里不写出「我发的是哪个模型」，排查就只能靠猜。
    """
    body = ""
    try:
        body = str(exc.response.text)[:_BODY_SNIPPET]
    except Exception:  # 响应体读不出来不该盖掉原始错误
        body = ""
    hint = {
        400: "请求被拒（常见原因：模型名已停用或写错、prompt 超过上下文长度）",
        401: "鉴权失败（LLM_API_KEY 无效或已撤销）",
        402: "余额不足",
        403: "无权限访问该模型",
        404: "模型不存在（该 id 可能已被厂下线，用 GET /models 确认当前有效的 id）",
        429: "触发限流",
    }.get(exc.status_code, "")
    suffix = f" —— {hint}" if hint else ""
    return f"{provider}（model={model}）返回 HTTP {exc.status_code}{suffix}；响应体：{body}"
