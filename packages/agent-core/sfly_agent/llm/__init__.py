"""LLM 抽象层。

对外只需要两个入口：

* ``build_llm()`` —— 按配置造一个 provider，调用方不关心它背后是谁
* ``complete_structured()`` —— 发一次补全并拿到**逐元素校验过的**条目

其余的名字都是给测试和排查用的。特别是 ``extract_json`` 和 ``salvage_truncated``：
它们是纯函数，``tests/unit/agent/test_structured.py`` 用表驱动把各种坏输出喂进去，
这个文件是全项目测试价值最高的地方之一。
"""

from sfly_agent.llm.base import LLMProvider, LLMResponse, estimate_tokens
from sfly_agent.llm.mock import MockLLM
from sfly_agent.llm.openai_compat import OpenAICompatLLM
from sfly_agent.llm.registry import FallbackLLM, MissingApiKeyError, build_llm
from sfly_agent.llm.structured import (
    L0_EXACT,
    L1_BALANCED,
    L2_CLEANED,
    L3_REPAIRED,
    L4_GAVE_UP,
    LEVEL_NAMES,
    Extraction,
    StructuredOutcome,
    complete_structured,
    extract_json,
    items_from_payload,
    salvage_truncated,
    validate_items,
)

__all__ = [
    "L0_EXACT",
    "L1_BALANCED",
    "L2_CLEANED",
    "L3_REPAIRED",
    "L4_GAVE_UP",
    "LEVEL_NAMES",
    "Extraction",
    "FallbackLLM",
    "LLMProvider",
    "LLMResponse",
    "MissingApiKeyError",
    "MockLLM",
    "OpenAICompatLLM",
    "StructuredOutcome",
    "build_llm",
    "complete_structured",
    "estimate_tokens",
    "extract_json",
    "items_from_payload",
    "salvage_truncated",
    "validate_items",
]
