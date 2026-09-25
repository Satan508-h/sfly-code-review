"""Worker 规格表 —— 「一套代码、三种部署」的落点。

整个项目里只有这一个文件定义「安全 / 性能 / 风格」有什么区别。
``runner.py`` 读这张表，对三个规格逐字相同地跑同一套消费循环。

这样做的理由：
  * **拓扑不变**：仍然是三个独立容器、独立失败域、独立伸缩
    （``--scale worker-security=3``）
  * **代码不抄三遍**：改消费循环、改重试逻辑、改幂等检查只改一处
  * 三个 prompt 主体的差异是**数据**，不是控制流

``prompt_body`` / ``few_shot`` 在 M1 填充。Step 0 只定结构。
"""

from __future__ import annotations

from dataclasses import dataclass

from sfly_shared.contracts import WorkerType


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """一个专业 Worker 的全部差异。"""

    worker_type: WorkerType
    #: Redis Streams 的消费者组名。同组内的多个副本竞争消费，Redis 保证
    #: 每条消息只投给组内一个成员 —— 这就是 ``--scale`` 能工作的机制。
    consumer_group: str
    stream: str
    #: 这个 Worker 负责的类目。**同时是冲突消解里 ``category_authority``
    #: 规则的依据**：属于本 Worker 职责域的发现，在与别的 Worker 冲突时直接胜出。
    categories: tuple[str, ...]
    #: 系统提示词。稳定前缀（规则、模式、示例）在前，变化的 diff 在后 ——
    #: 这个顺序让 DeepSeek 的自动前缀缓存在第 2、3 次调用时命中，
    #: 是可测量的成本优化，而不是玄学。
    system_prompt: str = ""
    #: 该 Worker 永远要看的核心规则 id（BM25 检索之外的保底项）
    core_rule_ids: tuple[str, ...] = ()
    #: 检索时取多少条规则
    top_k_rules: int = 8
    #: 若为 True，Worker 只做静态分析不调 LLM（留给 M11 的扩展位）
    deterministic_only: bool = False

    @property
    def name(self) -> str:
        return self.worker_type.value


SECURITY = WorkerSpec(
    worker_type=WorkerType.SECURITY,
    consumer_group="security-group",
    stream="review_tasks",
    categories=(
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
    core_rule_ids=("sec-sqli-001", "sec-secrets-001", "sec-authz-001"),
)

PERFORMANCE = WorkerSpec(
    worker_type=WorkerType.PERFORMANCE,
    consumer_group="performance-group",
    stream="review_tasks",
    categories=(
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
    core_rule_ids=("perf-nplus1-001", "perf-unbounded-001", "perf-blocking-001"),
)

STYLE = WorkerSpec(
    worker_type=WorkerType.STYLE,
    consumer_group="style-group",
    stream="review_tasks",
    categories=(
        "naming",
        "formatting",
        "docs",
        "dead_code",
        "complexity_readability",
        "error_handling",
        "magic_number",
        "duplication",
    ),
    core_rule_ids=("style-naming-001", "style-deadcode-001"),
)


SPECS: dict[WorkerType, WorkerSpec] = {
    WorkerType.SECURITY: SECURITY,
    WorkerType.PERFORMANCE: PERFORMANCE,
    WorkerType.STYLE: STYLE,
}

#: 类目 → 拥有它的 Worker。冲突消解规则的唯一数据来源，
#: 所以它从 spec 派生而不是另写一份 —— 两处定义迟早会漂移。
CATEGORY_OWNER: dict[str, WorkerType] = {
    cat: spec.worker_type for spec in SPECS.values() for cat in spec.categories
}


def spec_for(worker_type: WorkerType | str) -> WorkerSpec:
    """按名字取规格。``--spec security`` 和 ``WorkerType.SECURITY`` 都能用。

    未知名字要给出**可读的**报错而不是 ``ValueError: 'x' is not a valid WorkerType``
    —— 这个函数的主要调用点是命令行入口，用户打错字时最需要的是看到可选项列表。
    """
    try:
        key: WorkerType | None = (
            worker_type if isinstance(worker_type, WorkerType) else WorkerType(worker_type)
        )
    except ValueError:
        key = None
    if key is None or key not in SPECS:
        valid = ", ".join(sorted(s.value for s in SPECS))
        raise SystemExit(f"未知的 --spec {worker_type!r}；可选：{valid}")
    return SPECS[key]
