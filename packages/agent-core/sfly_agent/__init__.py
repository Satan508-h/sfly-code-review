"""sfly_agent —— LLM 抽象、结构化输出、RAG、聚合算法、GitHub 客户端。

**import 这个包不会触发任何 IO**：规则库、HTTP 客户端、数据库连接都是
用到才建。这一点让 ``sfly_agent`` 可以被单测直接引用，而不需要 Docker
或任何密钥 —— 分层约定（``pytest -m unit`` 在裸机上也能绿）靠的就是它。

对外的主入口：

* ``sfly_agent.diff`` —— unified diff 解析（``changed_lines`` 的来源）
* ``sfly_agent.prompt`` —— 提示词组装（稳定前缀 + 变化后缀）
* ``sfly_agent.llm`` —— provider 抽象与修复阶梯
* ``sfly_agent.rag`` —— 手写规则库与检索
"""

from sfly_agent.diff import DiffLine, DiffParseResult, iter_added_lines, iter_diff_lines, parse_unified_diff
from sfly_agent.prompt import build_system_prompt, build_user_prompt, dominant_language

__all__ = [
    "DiffLine",
    "DiffParseResult",
    "build_system_prompt",
    "build_user_prompt",
    "dominant_language",
    "iter_added_lines",
    "iter_diff_lines",
    "parse_unified_diff",
]
