"""显示层的中文标签 —— **一张表，两个消费方**（Worker 的 CLI 摘要、PR 评论正文）。

契约里的取值（``critical`` / ``security`` / ``ok``）是**标识符**：它们进 JSON、
进数据库、进 API 响应，一个字都不能改。但人读的那两处输出不该出现英文 ——
命令行上的读者和 PR 里的作者都不需要为了看懂「这条要不要紧」而先学会一套英文词表。

所以这里做的是**翻译**，不是重命名：JSON 里仍然是 ``critical``，摘要里是「严重」。

### 为什么放在 agent-core 而不是各自一份

这张表原本只有 Worker 的 CLI 在用，于是它住在 ``sfly_workers/__main__.py`` 里。
M5 的评论渲染也要用它 —— 而**两处各写一份的结果一定是漂移**：
某天有人觉得「中危」不如「中等」准确，改了一处，PR 评论和命令行摘要
从此对同一个 ``medium`` 用两个词。这种不一致没有任何东西会报错。

放在 agent-core：两个消费方（``sfly_workers`` 和 orchestrator 的评论渲染）
都已经依赖它，不需要新增依赖边。
"""

from __future__ import annotations

from sfly_shared.contracts import Severity

#: 严重度从重到轻。摘要、评论正文、排序都用它 —— 顺序只有一份。
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CRITICAL,
    Severity.HIGH,
    Severity.MEDIUM,
    Severity.LOW,
    Severity.INFO,
)

SEVERITY_LABEL: dict[Severity, str] = {
    Severity.CRITICAL: "严重",
    Severity.HIGH: "高危",
    Severity.MEDIUM: "中危",
    Severity.LOW: "低危",
    Severity.INFO: "提示",
}

#: 评论正文用的标记。**只有严重度这一个维度有表情** ——
#: 每条发现都挂一个 emoji 会让整段正文变成一排花花绿绿的点，
#: 反而看不出哪一条要紧。CLI 摘要不用它（终端里 emoji 宽度不一致，会串行）。
SEVERITY_EMOJI: dict[Severity, str] = {
    Severity.CRITICAL: "🔴",
    Severity.HIGH: "🟠",
    Severity.MEDIUM: "🟡",
    Severity.LOW: "🔵",
    Severity.INFO: "⚪",
}

WORKER_LABEL: dict[str, str] = {
    "security": "安全",
    "performance": "性能",
    "style": "风格",
}

STATUS_LABEL: dict[str, str] = {
    "ok": "正常",
    "partial": "部分成功（有条目未通过校验）",
    "failed": "失败",
}


def worker_label(worker_type: object) -> str:
    """取 Worker 的中文名。**认不出时回显原值** ——
    加第四个 Worker 时这里忘了登记，症状应该是一个英文单词出现在摘要里，
    而不是一个空白或者 ``None``。"""
    key = str(getattr(worker_type, "value", worker_type))
    return WORKER_LABEL.get(key, key)
