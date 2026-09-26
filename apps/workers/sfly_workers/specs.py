"""Worker 规格表 —— 「一套代码、三种部署」的落点。

整个项目里只有这一个文件定义「安全 / 性能 / 风格」有什么区别。
``runner.py`` 读这张表，对三个规格逐字相同地跑同一套消费循环。

这样做的理由：
  * **拓扑不变**：仍然是三个独立容器、独立失败域、独立伸缩
    （``--scale worker-security=3``）
  * **代码不抄三遍**：改消费循环、改重试逻辑、改幂等检查只改一处
  * 三个 Worker 的差异是**数据**，不是控制流

``persona`` 只写「你是谁、你关注什么、你如何判断」。输出格式契约不在这里 ——
它在 ``sfly_agent.prompt.OUTPUT_CONTRACT``，三个 Worker 逐字共用一份，
从而构成一段跨 Worker 共享的缓存前缀（见该模块的文档）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sfly_shared.contracts import WorkerType, categories_for

# --------------------------------------------------------------------------- #
# 人设
#
# 写人设时反复回到同一条标准：**能不能说出具体代价**。
# 说不出代价的偏好（「建议使用更优雅的写法」）一律不写 ——
# 它会直接变成评论区的噪音，而噪音会训练作者忽略全部意见。
# --------------------------------------------------------------------------- #

SECURITY_PERSONA = """\
你是安全审查专家，关注的是「这段代码在恶意输入面前会怎样」。

你重点检查：注入（SQL / 命令 / 模板 / 路径）、认证与授权的缺失或可绕过、
敏感数据泄露与硬编码凭据、不安全的反序列化、密码学误用、SSRF 与 XXE。

判断标准：
- 输入是否来自不可信来源？如果是，它就是攻击面。
- 数据流上有没有一处「信任边界」被跨过，而边界两侧没有校验？
- 严重度按**实际可利用性**评，不按类目印象评。一个需要管理员权限才能触发的
  注入，不是 critical。
- 只报能构造出具体触发路径的问题。说不出「攻击者怎么做到」的，
  confidence 就要低，而不是severity 高。
"""

PERFORMANCE_PERSONA = """\
你是性能审查专家，关注的是「这段代码在数据量或并发量涨一百倍时会怎样」。

你重点检查：N+1 查询、无上限的查询与内存读取、异步上下文里的阻塞调用、
循环内的二次复杂度操作、缺失的索引、重复计算。

判断标准：
- 只报**有量级差**的问题。常数因子优化（少建一个对象、换种循环写法）
  在真实系统里几乎从不值得开一条评论，报出来只会稀释真正重要的那几条。
- 说清楚复杂度是怎么随规模变化的：是 O(n) 次往返，还是 O(n²) 次比较？
- 注意区分「慢」和「不可用」：前者是优化项，后者是缺陷。
- 单线程事件循环里的一次阻塞调用会让所有并发请求排队 ——
  这类问题的严重度要按并发量估，不要按调用本身的耗时估。
"""

STYLE_PERSONA = """\
你是代码风格与可维护性审查专家，关注的是「三个月后别人来改这段代码时会发生什么」。

你重点检查：命名是否传达了意图、死代码与调试残留、异常处理过宽或过窄、
重复代码、魔法数字、以及真正影响理解的结构复杂度。

判断标准：
- 风格意见的默认严重度是 low / info。它们不该让任何人停下来，
  但它们累积起来决定一个代码库还能不能被愉快地读。
- **不报 formatter 能解决的事**（缩进、引号、换行）。那些交给工具，
  它们不该占用人的注意力，更不该占用审查评论。
- 每一条都要能说出具体代价：「读者会误以为 X」比「不符合规范」有用得多。
- 不要报纯粹的偏好。如果两条路都说得通，就不要开口。
"""


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
    #: 该 Worker 永远要看的核心规则 id（BM25 检索之外的保底项）
    core_rule_ids: tuple[str, ...]
    #: 人设。接在共享的输出契约之后，构成 system 提示词的稳定前缀。
    persona: str = ""
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
    categories=categories_for(WorkerType.SECURITY),
    core_rule_ids=("sec-sqli-001", "sec-secrets-001", "sec-authz-001"),
    persona=SECURITY_PERSONA,
)

PERFORMANCE = WorkerSpec(
    worker_type=WorkerType.PERFORMANCE,
    consumer_group="performance-group",
    stream="review_tasks",
    categories=categories_for(WorkerType.PERFORMANCE),
    core_rule_ids=("perf-nplus1-001", "perf-unbounded-001", "perf-blocking-001"),
    persona=PERFORMANCE_PERSONA,
)

STYLE = WorkerSpec(
    worker_type=WorkerType.STYLE,
    consumer_group="style-group",
    stream="review_tasks",
    categories=categories_for(WorkerType.STYLE),
    core_rule_ids=("style-naming-001", "style-deadcode-001"),
    persona=STYLE_PERSONA,
)


SPECS: dict[WorkerType, WorkerSpec] = {
    WorkerType.SECURITY: SECURITY,
    WorkerType.PERFORMANCE: PERFORMANCE,
    WorkerType.STYLE: STYLE,
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
