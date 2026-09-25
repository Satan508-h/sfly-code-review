"""Provider 的测试：请求契约、用量字段映射、错误翻译、注册表分支。

这三件事都不需要网络，但都需要**真的被钉住**：

* **请求契约** —— ``response_format={"type": "json_object"}`` 一旦漏掉，
  模型就会开始返回散文，而修复阶梯只能救回一部分。这是「provider 可换」
  的物理基础，值得一条测试守着。
* **用量字段映射** —— DeepSeek 和 OpenAI 把「命中缓存的输入 token」放在
  不同字段里。取错了不会报错，只会让缓存命中率永远是 0 ——
  而那个数字要写进评测报告。
* **错误翻译** —— 超时、连不上、HTTP 错误必须分成三类，因为它们的处置
  完全不同（重试 / 换地址 / 改配置）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast

import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

from sfly_agent.llm.base import LLMResponse
from sfly_agent.llm.mock import MockLLM
from sfly_agent.llm.openai_compat import OpenAICompatLLM
from sfly_agent.llm.registry import FallbackLLM, MissingApiKeyError, build_llm
from sfly_shared.config import Settings
from sfly_shared.contracts import WorkerType
from sfly_shared.errors import LlmHttpError, LlmTimeoutError

# --------------------------------------------------------------------------- #
# 假的 SDK 对象
#
# 它们只需要长得像 SDK 的返回对象 —— OpenAICompatLLM 只读几个属性。
# 这样就不必真的发一次 HTTP 请求，也就不必在测试里引入 httpx2。
# --------------------------------------------------------------------------- #


@dataclass
class _Msg:
    content: str | None


@dataclass
class _Choice:
    message: _Msg
    finish_reason: str | None = "stop"


@dataclass
class _Usage:
    prompt_tokens: int = 100
    completion_tokens: int = 20
    prompt_cache_hit_tokens: int | None = None
    prompt_tokens_details: Any = None


@dataclass
class _Response:
    choices: list[_Choice] = field(default_factory=lambda: [_Choice(_Msg('{"findings": []}'))])
    usage: _Usage | None = field(default_factory=_Usage)
    model: str = "deepseek-chat"


class _FakeClient:
    """记录每次 ``create`` 的入参，从而把**请求契约**变成可断言的东西。"""

    def __init__(self, response: _Response | Exception) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.base_url = "https://api.deepseek.com"

    async def _create(self, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def close(self) -> None:
        self.closed = True


def _llm(response: _Response | Exception = _Response(), **over: Any) -> tuple[OpenAICompatLLM, _FakeClient]:
    client = _FakeClient(response)
    base: dict[str, Any] = {
        "provider": "deepseek",
        "model": "deepseek-chat",
        "api_key": "sk-test",
        "temperature": 0.1,
        "max_tokens": 4096,
    }
    base.update(over)
    return OpenAICompatLLM(client=cast("AsyncOpenAI", client), **base), client


# --------------------------------------------------------------------------- #
# 请求契约
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_request_asks_for_a_json_object() -> None:
    """``response_format`` 必须显式要求 json object。

    不要求的话，模型会**有时候**返回散文 —— 而「有时候」意味着失败率随
    提示词内容漂移，你会以为是某条规则写得不好，实际是格式没约束。
    这也是不选 ``json_schema`` 的原因：它在两个 provider 上分别未文档化和需要 beta。
    """
    llm, client = _llm()
    await llm.complete(system="你是审查助手", user="审查这段 diff")
    assert client.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.unit
async def test_request_sends_system_and_user_as_separate_messages() -> None:
    """system 与 user 必须分开。

    合成一条会毁掉前缀缓存：DeepSeek 按**消息前缀**缓存，
    而 system 是三个 Worker 共用的稳定段，混进 user 里就再也不稳定了。
    """
    llm, client = _llm()
    await llm.complete(system="SYSTEM", user="USER")
    assert client.calls[0]["messages"] == [
        {"role": "system", "content": "SYSTEM"},
        {"role": "user", "content": "USER"},
    ]


@pytest.mark.unit
async def test_defaults_are_used_and_overridable() -> None:
    llm, client = _llm()
    await llm.complete(system="s", user="u")
    assert client.calls[0]["model"] == "deepseek-chat"
    assert client.calls[0]["temperature"] == 0.1
    assert client.calls[0]["max_tokens"] == 4096

    await llm.complete(system="s", user="u", temperature=0.9, max_tokens=128)
    assert client.calls[1]["temperature"] == 0.9
    assert client.calls[1]["max_tokens"] == 128


# --------------------------------------------------------------------------- #
# 用量字段映射
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_usage_tokens_are_mapped() -> None:
    llm, _ = _llm(_Response(usage=_Usage(prompt_tokens=1234, completion_tokens=567)))
    response = await llm.complete(system="s", user="u")
    assert response.tokens_in == 1234
    assert response.tokens_out == 567


@pytest.mark.unit
async def test_deepseek_cache_field_is_read() -> None:
    """DeepSeek 把命中缓存的输入 token 放在 ``prompt_cache_hit_tokens``。

    取不到就返回 0 而不报错 —— 所以取错字段的**唯一症状**是评测报告里的
    缓存命中率永远是 0，而那一栏本来是要用来证明成本优化的。
    """
    llm, _ = _llm(_Response(usage=_Usage(prompt_tokens=1000, prompt_cache_hit_tokens=800)))
    response = await llm.complete(system="s", user="u")
    assert response.cached_tokens == 800
    assert response.cache_hit_rate == pytest.approx(0.8)


@pytest.mark.unit
async def test_openai_nested_cache_field_is_read() -> None:
    """OpenAI 把同一个东西放在 ``prompt_tokens_details.cached_tokens``。"""
    usage = _Usage(prompt_tokens=1000, prompt_tokens_details=SimpleNamespace(cached_tokens=640))
    llm, _ = _llm(_Response(usage=usage))
    response = await llm.complete(system="s", user="u")
    assert response.cached_tokens == 640


@pytest.mark.unit
async def test_missing_usage_reports_zero_not_an_estimate() -> None:
    """取不到就报 0，**绝不回退成估算**。

    缓存命中率是要写进评测报告的数字，一个编出来的值比 0 有害得多 ——
    0 会让人去查，编出来的值会让人相信。
    """
    llm, _ = _llm(_Response(usage=None))
    response = await llm.complete(system="s", user="u")
    assert response.tokens_in == 0
    assert response.cached_tokens == 0


@pytest.mark.unit
async def test_null_content_becomes_empty_string() -> None:
    """模型只调工具不产出内容时 ``content`` 是 ``None``。

    让它流进解析器会在 ``text.strip()`` 上抛 AttributeError ——
    一个和「模型返回了空内容」毫无关系的报错。
    """
    llm, _ = _llm(_Response(choices=[_Choice(_Msg(None))]))
    assert (await llm.complete(system="s", user="u")).text == ""


@pytest.mark.unit
async def test_finish_reason_length_is_surfaced_as_truncated() -> None:
    """``truncated`` 直接决定修复阶梯怎么走（少写几条 vs 重新格式化）。"""
    llm, _ = _llm(_Response(choices=[_Choice(_Msg('{"findings": ['), finish_reason="length")]))
    assert (await llm.complete(system="s", user="u")).truncated is True


# --------------------------------------------------------------------------- #
# 错误翻译：三类必须分开
# --------------------------------------------------------------------------- #


@pytest.mark.unit
async def test_timeout_is_not_misreported_as_a_connection_error() -> None:
    """``APITimeoutError`` 是 ``APIConnectionError`` 的子类。

    except 的顺序反了的话，所有超时都会被报成「连不上」——
    而两者的排查方向完全相反：一个是网络/地址，一个是模型太慢或 prompt 太长。
    """
    llm, _ = _llm(APITimeoutError(request=cast(Any, SimpleNamespace())))
    with pytest.raises(LlmTimeoutError):
        await llm.complete(system="s", user="u")


@pytest.mark.unit
async def test_connection_error_names_the_base_url() -> None:
    """报错要带上地址。少了它，排查只剩「连不上」三个字。"""
    llm, _ = _llm(APIConnectionError(request=cast(Any, SimpleNamespace())))
    with pytest.raises(LlmHttpError, match=r"api\.deepseek\.com"):
        await llm.complete(system="s", user="u")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "hint_word"),
    [
        (401, "鉴权失败"),
        (402, "余额不足"),
        (429, "限流"),
        (400, "模型名"),
    ],
)
async def test_http_status_carries_an_actionable_hint(status: int, hint_word: str) -> None:
    """同一个 provider 的不同状态码处置方式完全不同，所以必须区分。

    只说「请求失败」等于什么都没说 —— 而 401 和 429 的区别是
    「去改配置」和「等一会儿」的区别。
    """
    # APIStatusError 会去读 response.request，所以这个假响应要带上它
    response = SimpleNamespace(
        status_code=status,
        text='{"error":"bad key"}',
        request=SimpleNamespace(),
        headers={},
    )
    llm, _ = _llm(APIStatusError("boom", response=cast(Any, response), body=None))
    with pytest.raises(LlmHttpError) as exc:
        await llm.complete(system="s", user="u")
    message = str(exc.value)
    assert str(status) in message
    assert hint_word in message


@pytest.mark.unit
async def test_client_is_closable() -> None:
    """不关会漏 socket：跑几百个测试之后会开始报「too many open files」——
    一个和代码逻辑毫无关系的失败。"""
    llm, client = _llm()
    await llm.aclose()
    assert client.closed is True


# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_default_provider_is_mock_and_needs_no_key() -> None:
    """默认必须零密钥可跑。

    这不是「方便」—— 而是 CI 和面试官 clone 下来第一分钟的体验，
    也是「pytest 在裸机上也能绿」这条分层约定的前提。
    """
    llm = build_llm(Settings(llm_provider="mock"))
    assert isinstance(llm, MockLLM)


@pytest.mark.unit
def test_mock_lane_filter_is_applied_at_build_time() -> None:
    llm = build_llm(Settings(llm_provider="mock"), worker_types=(WorkerType.STYLE,))
    assert isinstance(llm, MockLLM)
    assert llm.worker_types == frozenset({WorkerType.STYLE})


@pytest.mark.unit
def test_real_provider_without_a_key_fails_at_construction() -> None:
    """**在构造时就炸，而不是在第一次调用时。**

    这和「数据库连不上」是两类问题：后者是瞬时故障，进程应该起来并在
    健康页上说明白；前者是配置错误，重启一万次也不会变。
    一个安静跑着但每个任务都失败的 Worker 是最难排查的形态。
    """
    with pytest.raises(MissingApiKeyError, match="LLM_API_KEY"):
        build_llm(Settings(llm_provider="deepseek", llm_api_key=""))


@pytest.mark.unit
def test_real_provider_is_built_when_a_key_is_present() -> None:
    llm = build_llm(Settings(llm_provider="deepseek", llm_api_key="sk-x"))
    assert isinstance(llm, OpenAICompatLLM)
    assert llm.name == "deepseek"
    assert llm.model == "deepseek-chat"


@pytest.mark.unit
def test_fallback_is_opt_in() -> None:
    """默认**不**降级：静默造假比直接失败糟得多。"""
    assert not isinstance(build_llm(Settings(llm_provider="deepseek", llm_api_key="sk-x")), FallbackLLM)


@pytest.mark.unit
def test_fallback_wraps_only_when_explicitly_enabled() -> None:
    llm = build_llm(Settings(llm_provider="deepseek", llm_api_key="sk-x", allow_mock_fallback=True))
    assert isinstance(llm, FallbackLLM)


@pytest.mark.unit
async def test_fallback_marks_its_output_so_it_cannot_pass_as_real() -> None:
    """**降级必须留下痕迹。**

    否则 PR 上会出现一批看起来很合理的意见，而它们根本不是任何模型产生的。
    把来源写进 ``model`` 字段，这个字符串会一路进数据库、进评测报表、进 UI。
    """

    class _Boom:
        name = "primary"
        model = "primary-1"

        async def complete(self, **kw: Any) -> LLMResponse:
            raise LlmTimeoutError("上游超时")

    llm = FallbackLLM(_Boom(), MockLLM(model="mock-1"))
    response = await llm.complete(system="s", user="u")
    assert "fallback from primary" in response.model


@pytest.mark.unit
async def test_fallback_does_not_swallow_programming_errors() -> None:
    """只接 ``SflyError``。

    ``except Exception`` 会把「我代码写错了」也变成一次安静的降级 ——
    于是 bug 会以「模型质量不稳定」的形式表现，永远不会被修。
    """

    class _Buggy:
        name = "primary"
        model = "primary-1"

        async def complete(self, **kw: Any) -> LLMResponse:
            raise RuntimeError("我写错了")

    llm = FallbackLLM(_Buggy(), MockLLM())
    with pytest.raises(RuntimeError):
        await llm.complete(system="s", user="u")
