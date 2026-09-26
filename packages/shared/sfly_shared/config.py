"""集中配置。全部从环境变量读，全部有可用默认值。

**默认值必须保证「零密钥可跑通」**：``LLM_PROVIDER=mock`` + 本地 Docker 的
Postgres/Redis 时，不需要任何外部凭据就能跑完整链路。这是刻意的 —— 开发和
CI 不该依赖外部服务，面试官 clone 下来也应该能直接 ``docker compose up``。

唯一的例外是 ``QUEUE_BACKEND`` / ``LOCK_BACKEND``：这两个名字只允许出现在
``sfly_bus/factory.py`` 里。见 CLAUDE.md 约定 #4。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Mode = Literal["full", "lite"]
QueueBackend = Literal["redis", "memory"]
LockBackend = Literal["redis", "memory"]
WaitStrategy = Literal["interrupt", "poll"]
ConflictResolver = Literal["rules", "llm"]
LlmProvider = Literal["mock", "deepseek", "openai"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- 运行模式 ---------------------------------------------------------- #

    mode: Mode = "full"
    """``full`` = 7 容器分布式；``lite`` = Render 单容器。"""

    log_level: str = "INFO"
    log_json: bool = True
    """生产环境输出 JSON 日志；本地调试可以设 false 换成彩色可读格式。"""

    # -- 存储 -------------------------------------------------------------- #

    database_url: str = "postgresql://sfly:sfly@localhost:5432/sfly"
    """psycopg v3 连接串。**不是 asyncpg** —— LangGraph 的 Postgres
    checkpointer 用 psycopg，混用两个驱动会带来两份连接池。"""

    redis_url: str = "redis://localhost:6379/0"

    db_pool_min: int = 1
    db_pool_max: int = 5
    """Neon 免费版会主动掐连接，池子必须开 ``check_connection``。
    超过 10 条并发连接在免费版上会被拒。"""

    db_connect_timeout_s: int = 10
    """libpq 的连接超时。**默认值 0 是「交给操作系统」**，在某些网络下表现为
    挂起数分钟，所以这里显式给一个数。

    它还间接决定关机的耗时：池子关闭时会等待正在进行的那次连接尝试结束。"""

    db_ping_timeout_s: float = 5.0
    """``/api/health`` 探测 Postgres 的等待上限。

    它直接决定「数据库挂了的时候健康页有多慢」—— 数据库不可达时池子拿不到
    可用连接，这个等待会**走满**（实测：对着一台不可达的库，``ping()`` 耗时
    正好等于这个值）。设小了健康页响应更快，但数据库只是**慢**（不是挂了）
    的时候会误报 DOWN。"""

    # -- 传输（只在 factory.py 里读！） ------------------------------------ #

    queue_backend: QueueBackend = "redis"
    lock_backend: LockBackend = "redis"

    # -- 队列行为 ---------------------------------------------------------- #

    claim_idle_ms: int = 180_000
    """``XAUTOCLAIM`` 的空闲阈值。低于 60s 会偷走在跑的活；高于 300s
    会让 Worker 猝死后恢复变慢。回收早了只浪费 token（重复结果被主键吸收），
    不产出错数据，所以这个值可以实测调。"""

    reclaim_interval_s: int = 30
    max_attempts: int = 3
    """投递次数**达到**这个值还没有成功 → 进死信。

    是「达到」不是「超过」：``attempt`` 从 1 开始计数，所以 3 表示三次机会。

    计数由队列层负责（Redis 用 ``INCR sfly:attempts:{stream}:{id}`` 显式记，
    不解析 ``XPENDING`` —— 那个数字的含义随回收次数变化，不能拿来当重试计数），
    Worker 只是读 ``handle.attempt`` 做判定。"""

    stream_maxlen_tasks: int = 10_000
    stream_maxlen_results: int = 10_000
    """近似裁剪可能删掉「已投递未 ACK」的消息，让 XAUTOCLAIM 取回 None payload。
    消费者必须把空 payload 当「已裁剪」处理并 ack 掉。"""

    # -- 编排 -------------------------------------------------------------- #

    wait_strategy: WaitStrategy = "interrupt"
    """``interrupt`` 用 LangGraph 的 ``interrupt()`` 暂停图（优雅）。
    ``poll`` 是约 20 行的轮询循环（土但绝对能跑）。
    节点签名完全相同 —— M5 卡住超过一天就切过去，先跑通优于先优雅。"""

    run_deadline_s: int = 600
    sweeper_interval_s: int = 15
    conflict_resolver: ConflictResolver = "rules"

    # -- 审查规模上限 ------------------------------------------------------ #

    pr_max_files: int = 40
    """超出按风险排序取前 N，并标 ``diff_truncated=True``，评论里说明。"""

    pr_max_patch_chars: int = 200_000
    per_file_patch_chars: int = 8_000
    run_token_budget: int = 200_000
    worker_concurrency: int = 4

    # -- LLM --------------------------------------------------------------- #

    llm_provider: LlmProvider = "mock"
    llm_api_key: str = ""
    llm_base_url: str = ""
    """留空则用 provider 的默认地址。DeepSeek 是
    ``https://api.deepseek.com``，走 OpenAI 兼容接口。"""

    llm_model: str = ""
    llm_temperature: float = 0.1

    llm_reasoning_effort: str = "none"
    """推理模型的思维链预算。**``none`` 是量出来的，不是拍的。**

    新一点的模型默认会「先想后写」，而 ``max_tokens`` 同时管住两件事 ——
    于是思维链可以把输出预算整个吃掉，正文一个字都不剩。实测
    （``deepseek-flash``，``fixtures/webhook_pr.json``，同一个 4 文件的 PR）：

    | 配置 | security | performance | style |
    |---|---|---|---|
    | 默认（思维链开着） | 12 条 / 7389 tok | 3 条 / 3363 tok | **预算耗尽，0 条** |
    | ``reasoning_effort=none`` | 11 条 / 1578 tok | **12 条** / 1442 tok | **16 条** / 1710 tok |

    两个结论：**关掉之后三个 lane 都出得来**（style 从彻底失败变成 16 条），
    而且**更便宜**（约 1/3 的输出 token）。这个负载上思维链没有换来更多发现 ——
    performance 反而是关掉之后找得更多。原因不难理解：审查的输出是一份结构化
    列表，不是一道需要多步推理的题，而思维链把它变成了「想很久、写不完」。

    **留空 = 不发这个参数**，交给 provider 的默认值 —— 换到不认这个词的
    provider 时设空即可（发一个空串会 400，见 ``openai_compat.complete``）。
    """
    llm_max_tokens: int = 8192
    """单次响应的输出上限。

    **这个数同时管住思维链和正文**（见 :attr:`llm_reasoning_effort`），所以它
    必须容得下「想完 + 写完」，而不只是「写完」。实测：同一份 4 文件的 PR，
    关掉思维链之后三个 Worker 各写 1400–1700 个 token，开着的时候 security
    写了 7389、style 想满 16384 还没开始写正文。

    调到 8192 对正常负载是纯粹的余量（40 个文件的大 PR 会需要它），而不是
    为了兜住思维链 —— 那件事的解法是把思维链关掉。
    """
    llm_connect_timeout_s: int = 10
    llm_read_timeout_s: int = 120
    llm_max_repairs: int = 1
    """修复阶梯里 L3 的调用次数。超过 1 次就是纯粹的烧钱。"""

    # -- Mock LLM（离线开发用） -------------------------------------------- #

    mock_llm_failure_rate: float = 0.0
    """设成 0.3 会让 Mock 吐出带围栏 / 截断 / 裸标识符的响应，
    用来验证修复阶梯和逐元素校验真的在工作。"""

    mock_llm_delay_ms: int = 0
    """设成 200000 可以模拟卡住的 Worker，配合短 deadline 验证超时兜底。"""

    allow_mock_fallback: bool = False
    """LLM 故障时降级到 Mock。**只允许在开发环境开** —— 开到生产就是静默造假。"""

    # -- 线上限流（lite 模式，公网链接防刷） -------------------------------- #

    enable_real_llm: bool = True
    """线上总闸：设 false 则永远走扫描器，一分钱不花。

    它和下面那条预算是两道独立的闸：这条是**人按的开关**（面试季结束、
    发现有人在刷、想临时冻结成本），预算是**自动的闸**。
    """

    demo_access_key: str = ""
    """**未接线。** 见 :attr:`rate_limit_per_ip_per_hour` —— 同一条理由。"""

    rate_limit_per_ip_per_hour: int = 3
    """**未接线，M10 实测发现它现在保护不到任何东西。**

    它当初是为「公网访客手动触发一次审查」设计的，而那个入口**不存在**：
    M8 定下的前端是只读仪表盘，全项目唯一能建 run 的入口是 GitHub webhook
    （由 GitHub 调用，不是访客）。所以「按 IP 限流」在这里限不到任何人。

    更关键的一点：它本来也保护不了钱。未验签的请求在**写库之前**就被拒了
    （约定 #8），一次数据库写都不会产生 —— 一个挡不住花钱的入口闸，
    会让人以为成本被管住了。

    成本真正由 :attr:`daily_llm_call_budget` 管，而且卡在**真正花钱的那一行**
    （Worker 里的 ``complete()``）：那里是唯一的必经之路，包括我们自己跑评测、
    GitHub 重投 webhook、以及将来任何新入口。

    要让它活起来：加一个访客能触发的接口（贴 diff 那种），然后在那个路由上
    按 IP 计数。在那之前留着字段是因为设计意图还在，但**别以为它在工作**。
    """

    daily_llm_call_budget: int = 45
    """每天允许的**真实模型调用**次数。超了自动降级成扫描器，不是报错。

    一次审查有三个 Worker，所以「每天 15 次审查」= 45 次调用。

    数的是调用而不是 run：一次审查的 run 只有一条，而调用有三条，
    而**花钱的是调用**。而且按调用数不需要 join ``review_runs``，
    于是 CLI 手动跑的那些（同样花钱）也在里面。

    Mock 的调用**不计数** —— 它单价为零，算进去会让「今天还能花几次」
    因为「今天已经降级过一次」而变小（见 ``llm_spend_since``）。
    """

    daily_llm_cost_budget_usd: float = 2.0
    """每天的金额上限，和次数是两道独立的闸。

    一次调用很贵（长 diff + 长输出）时次数还没到、钱已经到了。
    线上按 M9 实测的每 PR 约 $0.006 估，45 次调用约 $0.1，
    所以 2.0 这条线平时够不着 —— 它是给「某天突然有一批大 PR」留的兜底。
    """

    # -- GitHub ------------------------------------------------------------ #

    github_token: str = ""
    """留空 = **dry-run**：publish 节点照样渲染正文、写事件，但不发任何请求，
    事件里 ``posted=false``。这是刻意的 —— 「不需要任何密钥就能跑通全链路」
    是本地开发和 CI 的前提（见 README 的默认值那一段）。"""
    github_webhook_secret: str = ""
    github_api_base: str = "https://api.github.com"
    """**从第一天就可配**。这不只是为了限流桩：指向 ``tests/github_stub.py``
    是 ``publish`` 节点能被单测的前提，也是手工演示「限流后退避重试」的开关。"""

    github_max_retries: int = 3
    github_connect_timeout_s: int = 10
    github_read_timeout_s: int = 30
    """读超时比 LLM 那个（120s）短得多：GitHub 的响应是毫秒级的，一次 25 条
    行内评论的 review 请求也就几秒。设成 120 秒会让「网络断了」表现为
    「GitHub 很慢」，而 publish 节点卡在那里会拖到 run 的 deadline 之后。"""

    github_max_wait_s: float = 60.0
    """单次限流等待的上限。超过它就直接判 ``publish_failed`` ——
    **在 publish 节点里睡一小时比直接失败更糟**：图会停在那儿，
    而报告早就落库了、重新发布随时可以点。"""

    # -- 可观测性 ---------------------------------------------------------- #

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # -- HTTP / CORS ------------------------------------------------------- #

    # NoDecode 是必须的：pydantic-settings 对 list 这类复杂类型会在**校验器之前**
    # 先做一次 JSON 解码，于是 "http://a,http://b" 这种逗号分隔写法会直接抛
    # JSONDecodeError，下面的校验器根本没机会执行。
    # 关掉自动解码后，校验器拿到原始字符串自己切分 ——
    # 在 Vercel / Render 的环境变量输入框里填逗号分隔比填 JSON 数组自然得多。
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["http://localhost:5173"])
    """精简模式下前端在 Vercel，必须显式列出域名。
    完整模式下 nginx 同源代理，CORS 用不上。"""

    port: int = 8000
    """Render 动态分配端口，**必须从环境变量读**，写死会得到 502。"""

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        """接受逗号分隔的字符串，方便在 Render / Vercel 的环境变量框里填。

        同时容忍 JSON 数组写法（``["http://a","http://b"]``）—— 用户的肌肉记忆
        各不相同，而 ``NoDecode`` 关掉自动解码后 JSON 形态不会被自动处理。
        如果只支持逗号分隔，填 JSON 的人会得到
        ``['["http://a"', '"http://b"]']`` 这种静默的垃圾值，
        表现为「CORS 配了但浏览器还是拦截」，极难排查。
        """
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("[") and s.endswith("]"):
                s = s[1:-1]
            return [x.strip().strip("'\"").strip() for x in s.split(",") if x.strip().strip("'\"")]
        return v

    # -- 派生属性 ---------------------------------------------------------- #

    @property
    def is_lite(self) -> bool:
        return self.mode == "lite"

    @property
    def resolved_llm_base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url.rstrip("/")
        return {
            "deepseek": "https://api.deepseek.com",
            "openai": "https://api.openai.com/v1",
            "mock": "",
        }[self.llm_provider]

    @property
    def resolved_llm_model(self) -> str:
        """实际发给 API 的模型 id。

        **这里的默认值是有保质期的。** 模型 id 会被厂商改名和下线 ——
        DeepSeek 的 ``deepseek-chat`` / ``deepseek-reasoner`` 这两个用了一年多的
        别名已于 2026-07-24 停用，用它们发请求会直接 400/404。所以：
        真正部署前用 ``GET {base_url}/models`` 确认一次当前有效的 id，
        然后用 ``LLM_MODEL`` 覆盖它，不要指望这个默认值是准的。

        之所以还留默认值：留空时至少能发出一个**语法正确**的请求，
        拿回一条「模型不存在」的报错；而留空直接报「没有模型」会让人以为是配置漏了。
        """
        if self.llm_model:
            return self.llm_model
        return {
            "deepseek": "deepseek-flash",
            "openai": "gpt-4o-mini",
            "mock": "mock-1",
        }[self.llm_provider]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试里用 ``get_settings.cache_clear()`` 重置。"""
    return Settings()
