"""领域契约 — 全项目唯一的真相来源。

5 个 app 都消费这些类型：api / orchestrator / workers / lite / 评测脚本。
改动顺序永远是：先改这里 → 跑 ``pytest tests/unit/contracts`` → 再改消费方。
反过来做会产生静默的字段丢失，因为 Pydantic 默认忽略多余字段。

设计约束：
  * ``Finding`` 是 **LLM 输出契约**，字段必须保持最小 —— 它是逐元素校验的目标。
    聚合阶段新增的字段放在 ``AggregatedFinding`` 子类里，不要污染这一层。
  * Redis Streams 无法存嵌套 map，所有跨进程消息都提供
    ``to_stream_fields()`` / ``from_stream_fields()`` 做平铺编解码。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --------------------------------------------------------------------------- #
# 枚举
# --------------------------------------------------------------------------- #


class Severity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class WorkerType(StrEnum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    STYLE = "style"


class ResultStatus(StrEnum):
    """WorkerResult 的三态。``partial`` 是一等公民，不是错误 —— 它表示
    「部分 finding 通过了校验，部分被逐元素校验丢弃」或「token 预算耗尽」。
    前端用降级徽章展示它，评测用它区分「全错」和「部分对」。"""

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"


class RunStatus(StrEnum):
    QUEUED = "queued"
    DISPATCHED = "dispatched"
    WAITING = "waiting"
    AGGREGATING = "aggregating"
    PUBLISHED = "published"
    PUBLISH_FAILED = "publish_failed"
    FAILED = "failed"
    SKIPPED = "skipped"


#: run 的终态。到了这里就**没有任何东西需要被唤醒**了，图的消费协程会直接
#: 丢弃这条 bootstrap，SSE 也可以收流了。
#:
#: ``publish_failed`` 也在里面：那是一次失败的**投递**，报告已经落库，
#: 由「重新发布」入口处理，不该再让图跑一遍。
#:
#: 放在契约层而不是编排层，是因为**它有三个消费方**：图（要不要跑）、
#: 协调协程（要不要唤醒）、SSE（要不要收流）。三处各写一份的话，
#: 加一个新状态时漏改一处的症状是「事件流永远不结束」或者「图被反复唤醒」。
TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {
        RunStatus.PUBLISHED,
        RunStatus.PUBLISH_FAILED,
        RunStatus.FAILED,
        RunStatus.SKIPPED,
    }
)


class DeliveryStatus(StrEnum):
    """一次 webhook 投递的处置结果。见 ``migrations/002_webhook_deliveries.sql``。

    ``RECEIVED`` 是**唯一可以被接管的状态**：它表示「记了账、还没干完」，
    所以下一次重投会重新处理它。其余四个都是终态，重投只会如实报告
    「这条投递之前已经处理过了」。
    """

    RECEIVED = "received"
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    REJECTED = "rejected"


class ErrorClass(StrEnum):
    """错误分类决定重试策略。不可重试的类别直接进死信，不浪费三次尝试。"""

    TRANSIENT = "transient"
    LLM_TIMEOUT = "llm_timeout"
    LLM_HTTP_ERROR = "llm_http_error"
    DB_UNAVAILABLE = "db_unavailable"
    # 以下四类不可重试
    SCHEMA_UNRECOVERABLE = "schema_unrecoverable"
    DIFF_TOO_LARGE = "diff_too_large"
    REPO_NOT_FOUND = "repo_not_found"
    AUTH_REVOKED = "auth_revoked"


NON_RETRYABLE_ERRORS: frozenset[ErrorClass] = frozenset(
    {
        ErrorClass.SCHEMA_UNRECOVERABLE,
        ErrorClass.DIFF_TOO_LARGE,
        ErrorClass.REPO_NOT_FOUND,
        ErrorClass.AUTH_REVOKED,
    }
)

#: 严重度排序。聚合阶段「取 max」和聚类代表选举都依赖它，所以放在契约层。
SEVERITY_RANK: dict[Severity, int] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}

#: 严重度先验概率，用于置信度重算。高危声称的出错率高于低危声称，
#: 所以一条 CRITICAL 需要更多佐证才能达到同样的置信度。
SEVERITY_PRIOR: dict[Severity, float] = {
    Severity.CRITICAL: 1.00,
    Severity.HIGH: 0.90,
    Severity.MEDIUM: 0.80,
    Severity.LOW: 0.70,
    Severity.INFO: 0.60,
}

#: 每个 Worker 负责的类目。**类目归属是领域事实，不是某个 app 的实现细节。**
#:
#: 在这里而不是在 Worker 规格里，理由和上面两张表一样：消费者横跨两边 ——
#: Worker 拿它筛规则，主 Agent 拿它做冲突消解的「职责域优先」（``category_authority``），
#: 契约层拿它校验类目别名表有没有指到不存在的地方。而**包不能反向依赖 app**，
#: 所以它必须在两边都能依赖的那一层。
#: （先例是 ``diff.py``：因为网关也要用，从 ``sfly_agent`` 搬到了 ``sfly_shared``。）
_WORKER_CATEGORIES: dict[WorkerType, tuple[str, ...]] = {
    WorkerType.SECURITY: (
        "sqli",
        "xss",
        "secrets",
        "auth",
        "crypto",
        "deserialization",
        "path_traversal",
        "ssrf",
        "insecure_random",
        "command_injection",
        "xxe",
        "open_redirect",
    ),
    WorkerType.PERFORMANCE: (
        "n_plus_one",
        "unbounded_query",
        "memory",
        "blocking_io",
        "quadratic",
        "missing_index",
        "repeated_work",
        "unnecessary_allocation",
        "sync_in_async",
    ),
    WorkerType.STYLE: (
        "naming",
        "formatting",
        "docs",
        "dead_code",
        "complexity_readability",
        "error_handling",
        "magic_number",
        "duplication",
    ),
}

#: 类目 → 拥有它的 Worker。**冲突消解「职责域优先」规则的唯一数据来源。**
#:
#: 一个类目**只能有一个主人** —— 冲突消解的 ``category_authority`` 靠它回答
#: 「谁在这个类目上有发言权」，两个主人会让那条规则变成随机的。
#: 这条不变量有测试钉着（``tests/unit/contracts/test_contracts.py``）。
CATEGORY_OWNER: dict[str, WorkerType] = {
    category: worker for worker, categories in _WORKER_CATEGORIES.items() for category in categories
}


def categories_for(worker: WorkerType) -> tuple[str, ...]:
    """某个 Worker 负责的全部类目。给 Worker 规格表用（它从这里取，不另写一份）。"""
    return _WORKER_CATEGORIES[worker]


#: LLM 写类目名时会用各种同义写法。**必须在契约层收敛到规范名**，否则
#: ``CATEGORY_OWNER``（就在上面）查不到，冲突消解的「职责域优先」规则
#: （category_authority）会静默失效 —— 表现是安全 Worker 报的 SQLi 被风格
#: Worker 的 LOW 拉平，而且日志里看不出任何异常。这是实测会踩到的坑，不是假想。
_CATEGORY_ALIASES: dict[str, str] = {
    # 安全
    "sql_injection": "sqli",
    "sql": "sqli",
    "injection": "sqli",
    "cross_site_scripting": "xss",
    "hardcoded_secret": "secrets",
    "hardcoded_secrets": "secrets",
    "hardcoded_credentials": "secrets",
    "secret": "secrets",
    "credential_leak": "secrets",
    "authentication": "auth",
    "authorization": "auth",
    "authz": "auth",
    "access_control": "auth",
    "cryptography": "crypto",
    "weak_crypto": "crypto",
    "insecure_deserialization": "deserialization",
    "unsafe_deserialization": "deserialization",
    "directory_traversal": "path_traversal",
    "server_side_request_forgery": "ssrf",
    "os_command_injection": "command_injection",
    "shell_injection": "command_injection",
    "xml_external_entity": "xxe",
    "insecure_randomness": "insecure_random",
    "weak_random": "insecure_random",
    "open_redirect": "open_redirect",
    "unvalidated_redirect": "open_redirect",
    "url_redirection": "open_redirect",
    # 性能
    "n+1": "n_plus_one",
    "n+1_query": "n_plus_one",
    "n_plus_1": "n_plus_one",
    "nplusone": "n_plus_one",
    "query_in_loop": "n_plus_one",
    "sync_io": "blocking_io",
    "synchronous_io": "blocking_io",
    "sync_call_in_async": "blocking_io",
    "blocking_call": "blocking_io",
    "sync_in_async": "sync_in_async",
    # 注意：slug 归一化会把 "O(n^2)" 变成 "o(n^2)"（括号保留），
    # 所以下面这几种带括号的形态要单独列，不能只写 o_n_squared
    "o(n^2)": "quadratic",
    "o(n²)": "quadratic",
    "o(n^2)_complexity": "quadratic",
    "on^2": "quadratic",
    "o_n_squared": "quadratic",
    "quadratic_complexity": "quadratic",
    "unbounded": "unbounded_query",
    "missing_pagination": "unbounded_query",
    "memory_leak": "memory",
    "memory_usage": "memory",
    "excessive_allocation": "unnecessary_allocation",
    "unnecessary_copy": "unnecessary_allocation",
    "repeated_computation": "repeated_work",
    "no_index": "missing_index",
    # 风格
    "naming_convention": "naming",
    "naming_conventions": "naming",
    "poor_naming": "naming",
    "documentation": "docs",
    "missing_docs": "docs",
    "comments": "docs",
    "unused_code": "dead_code",
    "unused_variable": "dead_code",
    "unused_import": "dead_code",
    "readability": "complexity_readability",
    "complexity": "complexity_readability",
    "code_duplication": "duplication",
    "duplicate_code": "duplication",
    "magic_numbers": "magic_number",
    "exception_handling": "error_handling",
    "code_formatting": "formatting",
}

#: LLM 偶尔把 confidence 写成枚举而非浮点。按这个映射强转。
_CONFIDENCE_WORDS: dict[str, float] = {
    "critical": 0.95,
    "high": 0.85,
    "medium": 0.60,
    "low": 0.35,
    "info": 0.25,
    "very high": 0.95,
    "very low": 0.20,
    "certain": 0.98,
    "uncertain": 0.30,
}


def _coerce_confidence(v: Any) -> Any:
    """把 LLM 可能吐出的各种形态统一成 float。

    实测会遇到：``"high"``、``"High"``、``"0.85"``、``85``（百分数）、``None``。
    在契约层做掉，聚合和评测就不必各自防御。

    **关于 1 < v < 2 的区间**：不按百分数处理，而是夹到 1.0。
    模型吐出 ``1.7`` 时表达的是「非常确定」，不是「1.7%」——
    按百分数算会得到 0.017，直接跌破 0.35 的抑制阈值，
    与模型本意完全相反。而 ``85`` 这种量级只可能是百分数误写。
    两种误读的代价不对称，所以阈值取 2 而不是 1。
    """
    if v is None:
        return 0.5  # 中性默认；标记为不可信但不至于被静默丢弃
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _CONFIDENCE_WORDS:
            return _CONFIDENCE_WORDS[s]
        try:
            v = float(s.rstrip("%"))
            if s.endswith("%"):
                v /= 100.0
        except ValueError:
            return 0.5
    if isinstance(v, bool):  # bool 是 int 的子类，但 True/False 不构成置信度
        return 0.5
    if isinstance(v, (int, float)):
        v = float(v)
        if v >= 2.0:  # 百分数误写，如 85
            v /= 100.0
        return min(max(v, 0.0), 1.0)
    return 0.5


def _coerce_line(v: Any) -> Any:
    """``line`` / ``end_line`` 常见形态：``42``、``"42"``、``"L42"``、
    ``"42-45"``（取起始行）、``"db.py:42"``（带文件名）。

    ``None`` 必须原样放行 —— ``end_line`` 是可选字段，把它当解析失败会让
    每一条没有区间的 finding 在序列化往返时炸掉。
    """
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"无法解析行号: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        s = v.strip().lstrip("Ll")
        if ":" in s:  # "db.py:42" 或 Windows 路径 "C:\\a.py:42"
            s = s.rsplit(":", 1)[-1]
        s = s.split("-")[0].strip()  # "42-45" 取起始行
        try:
            return int(s)
        except ValueError:
            raise ValueError(f"无法解析行号: {v!r}") from None
    raise ValueError(f"无法解析行号: {v!r}")


class _Contract(BaseModel):
    """所有契约的基类：禁止未声明字段，枚举大小写不敏感。"""

    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        use_enum_values=False,
        frozen=False,
    )


# --------------------------------------------------------------------------- #
# 输入侧：文件与规则
# --------------------------------------------------------------------------- #


class FilePatch(_Contract):
    """单个文件的 diff。``changed_lines`` 必须是新文件中的行号集合 ——
    GitHub 会拒绝锚定在未变更行上的 inline 评论，这个集合是发布阶段的依据。"""

    path: str
    language: str = "unknown"
    patch: str
    additions: int = 0
    deletions: int = 0
    changed_lines: list[int] = Field(default_factory=list)
    is_new_file: bool = False
    is_deleted_file: bool = False
    truncated: bool = False

    @field_validator("changed_lines")
    @classmethod
    def _dedupe_lines(cls, v: list[int]) -> list[int]:
        return sorted(set(v))


class Rule(_Contract):
    """一条代码规范。来自 ``packages/agent-core/sfly_agent/rag/corpus/*.yaml``。

    这些是**手写**的，不是从仓库里索引出来的 —— 规则库本身是可被审阅的作品。
    命中规则时 Worker 会在 finding 上回填 ``rule_id``，从而获得置信度
    ``+0.10 * grounded`` 加成，并在 PR 评论里附上参考链接。
    """

    id: str
    title: str
    worker_type: WorkerType
    category: str
    severity_hint: Severity | None = None
    languages: list[str] = Field(default_factory=list)
    cwe: str | None = None
    owasp: str | None = None
    body: str
    references: list[str] = Field(default_factory=list)

    def matches_language(self, language: str) -> bool:
        """空 ``languages`` 表示与语言无关。"""
        return not self.languages or language.lower() in {x.lower() for x in self.languages}


# --------------------------------------------------------------------------- #
# Worker 输出契约（LLM 逐元素校验的目标）
# --------------------------------------------------------------------------- #


class Finding(_Contract):
    """一条审查发现。

    **这是 LLM 的输出契约**，所以字段保持最小 —— 聚合阶段新增的字段放在
    ``AggregatedFinding`` 里。在这里加必填字段会直接提高 LLM 的失败率。
    """

    file: str
    line: int
    end_line: int | None = None
    severity: Severity
    category: str
    message: str
    evidence: str | None = None
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    suggestion: str | None = None
    rule_id: str | None = None
    #: ``line`` 是否落在输入的变更行集合内。由 Worker 在解析后回填；
    #: 为 False 时发布阶段会降级成文件级评论而不是 inline 评论。
    source_line_verified: bool = False
    fingerprint: str | None = None

    _norm_conf = field_validator("confidence", mode="before")(_coerce_confidence)
    _norm_line = field_validator("line", "end_line", mode="before")(_coerce_line)

    @field_validator("severity", mode="before")
    @classmethod
    def _norm_severity(cls, v: Any) -> Any:
        if isinstance(v, str):
            s = v.strip().lower()
            aliases = {"error": "high", "warning": "medium", "warn": "medium", "note": "info"}
            return aliases.get(s, s)
        return v

    @field_validator("category", mode="before")
    @classmethod
    def _norm_category(cls, v: Any) -> Any:
        # 先做形态归一（"SQL Injection" / "sql-injection" → "sql_injection"），
        # 再过别名表收敛到规范名（"sql_injection" → "sqli"）。
        # 规范名必须与 CATEGORY_OWNER 的键一致（有测试钉着）。
        slug = str(v).strip().lower().replace(" ", "_").replace("-", "_")
        return _CATEGORY_ALIASES.get(slug, slug)

    @field_validator("rule_id", "evidence", "suggestion", "end_line", mode="before")
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        """LLM 常把「没有」写成空串而非 null。契约层统一成 None。"""
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @model_validator(mode="after")
    def _end_after_start(self) -> Self:
        if self.end_line is not None and self.end_line < self.line:
            self.end_line = self.line
        return self


class AggregatedFinding(Finding):
    """聚合后的发现。比 ``Finding`` 多出的字段全部来自确定性计算，不来自 LLM。"""

    adjusted_confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    #: 独立报出这一条的所有 Worker。跨 Worker 印证是去重最值钱的产物，前端要展示。
    sources: list[WorkerType] = Field(default_factory=list)
    corroboration_count: int = 1
    cluster_id: int | None = None
    needs_human_review: bool = False
    #: ``raw`` = Worker 原始输出；``clustered`` = 去重后保留；``suppressed`` = 置信度闸砍掉
    stage: Literal["raw", "clustered", "suppressed"] = "raw"
    conflict: ConflictRecord | None = None


class ConflictRecord(_Contract):
    """两条高危评估互相矛盾时的裁决记录。前端有独立的冲突面板展示它。"""

    file: str
    line: int
    winner_worker: WorkerType
    loser_worker: WorkerType
    winner_severity: Severity
    loser_severity: Severity
    #: 命中的规则名：category_authority / out_of_lane_downgrade /
    #: evidence_adjudication / unresolved
    resolution_rule: str
    rationale: str


# --------------------------------------------------------------------------- #
# 跨进程消息
# --------------------------------------------------------------------------- #


def idempotency_key_for(repo_id: str, pr_number: int, head_sha: str) -> str:
    """幂等键 = ``repo_id:pr_number:head_sha``。

    同一个 PR 的同一个 head commit 永远映射到同一个 run。
    GitHub 在 PR 更新时会换 head_sha，所以新提交自然会触发新审查。
    """
    return f"{repo_id}:{pr_number}:{head_sha}"


def assert_idempotency_key(repo_id: str, pr_number: int, head_sha: str, key: str) -> None:
    """校验显式传入的幂等键与三元组一致。

    **不能在契约层用计算属性悄悄修正它。** 键不匹配意味着上游某处算错了，
    而错误的幂等键有两个方向都很糟的后果：算宽了会让不同的提交被误判成
    同一个 run（漏审），算窄了会让同一个 PR 被反复审查（重复评论 + 重复花钱）。
    两者都是静默的，所以宁可在这里硬失败。
    """
    expected = idempotency_key_for(repo_id, pr_number, head_sha)
    if key != expected:
        raise ValueError(f"idempotency_key 与 repo/pr/head_sha 不一致：期望 {expected!r}，得到 {key!r}")


class BootstrapMessage(_Contract):
    """api → orchestrator。``review_bootstrap`` 流。

    注意：API **不直接写 review_tasks**。文件风险排序和规则检索由编排层的
    ``plan`` 节点负责，所以 Worker 收到的 TaskMessage 已经带上规则，
    Worker 因而保持无状态且不需要 RAG 依赖。
    """

    task_id: str
    idempotency_key: str
    repo_id: str
    repo_node_id: str
    pr_number: int
    head_sha: str
    base_sha: str
    installation_id: int | None = None
    #: 由 webhook 载荷带来的原始文件列表；plan 节点会排序与截断
    file_patches: list[FilePatch] = Field(default_factory=list)
    pr_title: str = ""
    pr_author: str = ""
    requested_workers: list[WorkerType] = Field(default_factory=list)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _check_key(self) -> Self:
        assert_idempotency_key(self.repo_id, self.pr_number, self.head_sha, self.idempotency_key)
        return self

    def to_stream_fields(self) -> dict[str, str]:
        """平铺字段。除 ``payload`` 外都是给**过滤与运维**用的 ——
        ``XRANGE review_bootstrap`` 时不必反序列化整包就能看出「这是哪个 PR 的」。"""
        return {
            "payload": self.model_dump_json(),
            "task_id": self.task_id,
            "repo_id": self.repo_id,
            "pr_number": str(self.pr_number),
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_stream_fields(cls, fields: dict[str, str]) -> Self:
        return cls.model_validate_json(fields["payload"])


class TaskMessage(_Contract):
    """orchestrator → worker。``review_tasks`` 流。"""

    task_id: str
    worker_type: WorkerType
    idempotency_key: str
    repo_id: str
    repo_node_id: str
    pr_number: int
    head_sha: str
    base_sha: str
    file_patches: list[FilePatch]
    language: str = "unknown"
    #: plan 阶段检索好的规则。Worker 不做检索 —— 这让检索决策在
    #: 图状态中可见，评测才能测量「检索到的规则是否提升了精确率」。
    rules: list[Rule] = Field(default_factory=list)
    #: 从 1 开始。重派时递增，用于死信判定。
    attempt: int = 1
    dispatched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _check_key(self) -> Self:
        # TaskMessage 是由编排器从 BootstrapMessage 派生出来的，键理应一致。
        # 校验它不是多余 —— 「派生时漏传一个字段」正是这类代码最常见的静默 bug。
        assert_idempotency_key(self.repo_id, self.pr_number, self.head_sha, self.idempotency_key)
        return self

    # -- Redis Streams 平铺编解码 ------------------------------------------ #
    # Streams 无法存嵌套结构，所以 payload 整体序列化成单个 JSON 字段，
    # 另外平铺几个字段供消费者按需过滤（避免反序列化整包才能筛选）。

    def to_stream_fields(self) -> dict[str, str]:
        return {
            "payload": self.model_dump_json(),
            "task_id": self.task_id,
            "worker_type": self.worker_type.value,
            "repo_id": self.repo_id,
            "attempt": str(self.attempt),
        }

    @classmethod
    def from_stream_fields(cls, fields: dict[str, str]) -> Self:
        return cls.model_validate_json(fields["payload"])


class WorkerResult(_Contract):
    """worker → orchestrator。``review_results`` 流 + Postgres。

    **失败也是结果**：Worker 放弃前必须先发一条 ``status=FAILED`` 的
    WorkerResult 再 ack，否则 ``wait`` 节点的屏障永远闭合不了。
    """

    task_id: str
    worker_type: WorkerType
    status: ResultStatus = ResultStatus.OK
    findings: list[Finding] = Field(default_factory=list)
    error: str | None = None
    error_class: ErrorClass | None = None
    # 成本与可观测性。评测的每一个 token 数字都是这些字段的 SUM。
    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    latency_ms: int = 0
    model: str | None = None
    #: 解析彻底失败时保留原文（截断至 8KB）。没有它就无法改进 prompt。
    raw_response: str | None = None
    #: 被逐元素校验丢弃的条目数。12 条里坏 1 条的代价应该是 1 条而非整个 Worker。
    dropped_findings: int = 0
    attempt: int = 1
    finished_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def failed(
        cls,
        task_id: str,
        worker_type: WorkerType,
        error: str,
        error_class: ErrorClass = ErrorClass.TRANSIENT,
        *,
        attempt: int = 1,
    ) -> Self:
        """构造一条「失败结果」。屏障闭合与超时兜底都靠它。"""
        return cls(
            task_id=task_id,
            worker_type=worker_type,
            status=ResultStatus.FAILED,
            error=error[:2000],
            error_class=error_class,
            attempt=attempt,
        )

    @model_validator(mode="after")
    def _status_consistent(self) -> Self:
        # 有 finding 却报 failed，或没 finding 又没 error，都是上游逻辑写错了。
        # 这里只做归一，不抛错 —— Worker 不该因为元数据不一致而丢掉已产出的结果。
        if self.findings and self.status is ResultStatus.FAILED:
            self.status = ResultStatus.PARTIAL
        if self.error and self.status is ResultStatus.OK:
            self.status = ResultStatus.PARTIAL
        return self

    def to_stream_fields(self) -> dict[str, str]:
        return {
            "payload": self.model_dump_json(),
            "task_id": self.task_id,
            "worker_type": self.worker_type.value,
            "status": self.status.value,
            "attempt": str(self.attempt),
        }

    @classmethod
    def from_stream_fields(cls, fields: dict[str, str]) -> Self:
        return cls.model_validate_json(fields["payload"])


# --------------------------------------------------------------------------- #
# 主 Agent 输出
# --------------------------------------------------------------------------- #


class RunTotals(_Contract):
    """单次 run 的成本与耗时汇总。UI 和评测报告都读它。"""

    tokens_in: int = 0
    tokens_out: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    llm_calls: int = 0
    duration_ms: int = 0
    per_worker_ms: dict[str, int] = Field(default_factory=dict)

    @property
    def cache_hit_rate(self) -> float:
        """DeepSeek 的自动前缀缓存命中率。报告里要和控制开销比一起出现 ——
        重复的输入 token 不等于浪费的 token。"""
        return self.cached_tokens / self.tokens_in if self.tokens_in else 0.0


class ReviewReport(_Contract):
    """主 Agent 的最终输出。``finalize`` 节点生成，``publish`` 节点消费。"""

    task_id: str
    repo_id: str
    repo_node_id: str
    pr_number: int
    head_sha: str
    base_sha: str
    #: 去重 + 冲突消解 + 置信度重算之后的发现
    findings: list[AggregatedFinding] = Field(default_factory=list)
    #: 置信度 < 0.35 被闸掉的发现。**入库但不发布** ——
    #: 评测需要用它们测量被这道闸砍掉的召回，否则阈值只能盲调。
    suppressed: list[AggregatedFinding] = Field(default_factory=list)
    conflicts: list[ConflictRecord] = Field(default_factory=list)
    block_merge: bool = False
    decision_reason: str = "below_threshold"
    #: 有 Worker 失败或超时。前端显示降级徽章。
    degraded: bool = False
    missing_workers: list[WorkerType] = Field(default_factory=list)
    #: 这次的发现全部来自**确定性扫描器**，不是模型（今日配额用完、
    #: 或者 ``ENABLE_REAL_LLM=false``）。前端显示另一种徽章。
    #:
    #: **它和 ``degraded`` 是两件事，不能合并。** ``degraded`` 说的是
    #: 「报告不完整」（有 Worker 没交结果），这里说的是「报告完整，但它的
    #: 来源不是模型」。混在一起的后果是把「今天配额用完了」显示成「系统坏了」，
    #: 而访客看到的是一份看起来完全正常的审查结果 —— 这正是要标出来的原因：
    #: 扫描器的 finding 和模型的 finding 在报告里长得一模一样。
    scanned_only: bool = False
    files_total: int = 0
    files_reviewed: int = 0
    diff_truncated: bool = False
    totals: RunTotals = Field(default_factory=RunTotals)
    #: 由 finalize 生成的 Markdown 正文。publish 只负责投递，不再生成内容。
    comment_body: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunEvent(_Contract):
    """SSE 事件，同时写入 ``run_events`` 表。

    表是权威来源，SSE 只是快路径。这样客户端带 ``Last-Event-ID`` 重连时
    可以从 ``seq`` 补齐，断流不会留下缺口；轮询也成为免费的降级方案。
    """

    seq: int
    task_id: str
    kind: Literal[
        "run.created",
        "run.status",
        "node.started",
        "node.finished",
        # 去 GitHub 拉这个 PR 的文件失败了（限流、网络、没配 token）。
        # **它不是「没有可审的文件」** —— 那条路走 ``skipped``，而这条会重试，
        # 重试用完则整个 run 判 failed。分开是因为两者的含义正好相反：
        # 一个是「看过了，没问题」，一个是「根本没看到」。
        "plan.fetch_failed",
        "worker.dispatched",
        "worker.result",
        "worker.failed",
        "aggregate.done",
        "publish.done",
        "publish.failed",
        "run.finished",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class RunRow(_Contract):
    """``review_runs`` 表的一行。``deadline_at`` 是所有恢复逻辑的主干 ——
    任何系统可能卡住的状态，都必须是一行带 deadline 的记录。"""

    task_id: str
    idempotency_key: str
    repo_id: str
    repo_node_id: str
    pr_number: int
    head_sha: str
    base_sha: str
    status: RunStatus
    attempt: int = 1
    files_total: int = 0
    files_reviewed: int = 0
    diff_truncated: bool = False
    planned_workers: list[WorkerType] = Field(default_factory=list)
    missing_workers: list[WorkerType] = Field(default_factory=list)
    deadline_at: datetime
    dispatched_at: datetime | None = None
    published_at: datetime | None = None
    #: publish 节点发帖前先查这个字段 —— 第二道防重复评论的闸
    github_comment_id: int | None = None
    block_merge: bool | None = None
    degraded: bool = False
    totals: RunTotals | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class DeliveryRow(_Contract):
    """``webhook_deliveries`` 表的一行 —— 一次 webhook 投递的账。

    **这是 API 侧唯一的持久化写入。** 它不建 run（那是编排层 ``ingest`` 的事），
    所以这张表里的 ``task_id`` 是「这次投递转给了谁」，可空、也不加外键。
    """

    delivery_id: str
    event: str = ""
    repo_id: str = ""
    pr_number: int | None = None
    status: DeliveryStatus = DeliveryStatus.RECEIVED
    task_id: str | None = None
    reason: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    @property
    def is_settled(self) -> bool:
        """已经了结了吗。未了结的投递可以被下一次重投接管。"""
        return self.status is not DeliveryStatus.RECEIVED


class LlmSpend(_Contract):
    """一段时间内花掉的模型调用 —— **线上成本闸的唯一输入**。

    它是从 ``llm_calls`` 表 SUM 出来的，不是内存里的计数器：精简模式跑在
    Render 免费档上，**15 分钟没人访问就休眠，下次访问重新拉起一个进程**。
    用内存计数的话，那个「每日上限」每天会被重置几十次，看起来在保护，
    实际不保护任何东西 —— 而它失败的方向是**多花钱**，没有任何东西会报警。

    ``cost_usd`` 是 ``numeric`` 列 SUM 出来的，所以它是 ``Decimal`` 的字符串
    形态。这里只当数字用（比较、展示），不参与需要精确到分的结算。
    """

    calls: int = 0
    cost_usd: float = 0.0


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def normalize_path(path: str) -> str:
    """路径归一化。指纹计算和去重都必须先过这一步。

    处理真实会遇到的各种形态：``a/src/x.py`` / ``b/src/x.py``（diff 前缀）、
    ``./src/x.py``、``src\\x.py``（Windows 反斜杠）、大小写差异。
    """
    p = path.strip().replace("\\", "/").lstrip("./")
    for prefix in ("a/", "b/"):
        if p.startswith(prefix):
            p = p[2:]
            break
    return p.lower()


def stable_hash(*parts: str) -> str:
    """跨进程稳定的哈希。不要用内置 ``hash()`` —— 它对字符串加了随机盐，
    不同进程结果不同，会让指纹在 Worker 和 orchestrator 之间对不上。"""
    h = hashlib.sha1(usedforsecurity=False)
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")  # 分隔符，避免 "ab"+"c" 与 "a"+"bc" 碰撞
    return h.hexdigest()
