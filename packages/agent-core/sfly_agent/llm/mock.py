"""Mock LLM —— 离线和 CI 的默认后端。

**它的定位容易被人想错，所以写在最前面：Mock 不是「随机吐几条假 finding」。**
下游所有东西都建在它上面 —— M2/M3 的队列往返测试、M5 的图端到端、M9 的评测
基线。如果它输出的是随机内容，那些测试和数字就全都没有意义。

所以它是一个**确定性的、真的读 diff 的规则扫描器**：
  * 逐行扫新增行，用正则匹配真实存在的漏洞模式
  * 从 ``@@`` 头部跟踪新文件行号，报出来的行号落在变更行上
  * 命中规则时回填 ``rule_id``，让 ``grounded`` 置信度加成在本地也能复现
  * 同一个输入永远产出同一个输出（连故障注入也是）

它**不是**用来假装模型很聪明的。它的用途是让除模型之外的所有环节都能被
单独验证 —— 换掉它，链路其余部分一行都不用改。

故障注入（``MOCK_LLM_FAILURE_RATE``）值得单独说一句：它是**验证修复阶梯
真的在工作**的唯一手段。没有它，「模型返回坏 JSON」这条路径要等到
线上真实模型第一次抽风时才会被走到，而那时你在看生产环境。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from sfly_agent.llm.base import LLMResponse, estimate_tokens
from sfly_shared.contracts import Finding, Severity, WorkerType, stable_hash
from sfly_shared.diff import DiffLine, iter_added_lines, iter_diff_lines
from sfly_shared.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Pattern:
    """一条检测规则。``lanes`` 决定它属于哪个 Worker。"""

    category: str
    severity: Severity
    rule_id: str | None
    message: str
    suggestion: str
    regex: re.Pattern[str]
    lanes: tuple[WorkerType, ...]
    confidence: float = 0.8
    #: 命中后需要再看一眼整行是否包含这些词之一（用来排除测试代码、注释掉的例子）
    unless: tuple[str, ...] = ()


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


SEC = (WorkerType.SECURITY,)
PERF = (WorkerType.PERFORMANCE,)
STY = (WorkerType.STYLE,)

#: 检测表。**顺序有意义** —— 同一个行号上第一个命中的规则胜出，
#: 所以高危的排前面，避免一条 `f"SELECT {x}"` 同时被报成 sqli 和 formatting。
PATTERNS: tuple[_Pattern, ...] = (
    # -- 安全 -------------------------------------------------------------- #
    _Pattern(
        category="sqli",
        severity=Severity.CRITICAL,
        rule_id="sec-sqli-001",
        message="SQL 语句用字符串拼接/格式化构造，用户输入可直接改写查询语义",
        suggestion="改用参数化查询：cursor.execute('SELECT ... WHERE id = %s', (user_id,))",
        regex=_rx(r"\b(?:execute|executemany|raw|query)\s*\(\s*f[\"']"),
        lanes=SEC,
        confidence=0.9,
        # 唯一被放行的插值是**占位符生成器**：`IN ({placeholders})` 里的
        # 插值结果是 "?,?,?"，不含任何外部数据，是参数化查询的合法用法。
        # 不放行它就会在唯一正确的写法上报错 —— 而这条规则的措辞
        # （「改用参数化查询」）会让作者困惑：我已经参数化了。
        unless=("#", "sqlite3.connect", "tests/", "placeholder"),
    ),
    _Pattern(
        category="sqli",
        severity=Severity.HIGH,
        rule_id="sec-sqli-001",
        message="SQL 语句通过 .format() 插入变量，等价于字符串拼接",
        suggestion="改用参数化查询，让驱动负责转义",
        regex=_rx(r"\b(?:execute|executemany|query)\s*\([^)]*\.format\s*\("),
        lanes=SEC,
        confidence=0.85,
    ),
    _Pattern(
        category="sqli",
        severity=Severity.HIGH,
        rule_id="sec-sqli-001",
        message="SQL 语句用 % 或 + 拼接变量",
        suggestion="改用参数化查询；拼接的字符串永远不会被转义",
        regex=_rx(r"\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b[^\"']*[\"']\s*(?:%|\+)\s*\w"),
        lanes=SEC,
        confidence=0.8,
    ),
    _Pattern(
        category="secrets",
        severity=Severity.CRITICAL,
        rule_id="sec-secrets-001",
        message="疑似把凭据硬编码在源码里，会随仓库永久留存",
        suggestion="改从环境变量读取，并轮换这个已经泄露的值",
        regex=_rx(
            r"\b(?:password|passwd|pwd|secret|api_?key|access_?key|auth_?token|client_?secret|private_?key)"
            r"\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']"
        ),
        lanes=SEC,
        confidence=0.75,
        unless=("os.environ", "getenv", "settings.", "config.", "placeholder", "changeme", "<", "${"),
    ),
    _Pattern(
        category="command_injection",
        severity=Severity.CRITICAL,
        rule_id="sec-cmdi-001",
        message="以 shell 方式执行命令，参数里的元字符会被 shell 解释",
        suggestion="去掉 shell=True 改为列表传参，或用 shlex.quote 转义",
        regex=_rx(r"\b(?:os\.system|os\.popen|subprocess\.\w+\s*\([^)]*shell\s*=\s*True)"),
        lanes=SEC,
        confidence=0.9,
    ),
    _Pattern(
        category="deserialization",
        severity=Severity.CRITICAL,
        rule_id="sec-deser-001",
        message="对不可信数据做反序列化，pickle 的 __reduce__ 可以直接执行任意代码",
        suggestion="改用 JSON 等纯数据格式；确需 pickle 就必须先做签名校验",
        regex=_rx(r"\b(?:pickle|cPickle|marshal|dill)\.loads?\s*\("),
        lanes=SEC,
        confidence=0.85,
    ),
    _Pattern(
        category="deserialization",
        severity=Severity.HIGH,
        rule_id="sec-deser-001",
        message="yaml.load 默认构造任意对象，未指定 SafeLoader",
        suggestion="改用 yaml.safe_load，或显式传 Loader=yaml.SafeLoader",
        regex=_rx(r"\byaml\.load\s*\((?![^)]*Loader)"),
        lanes=SEC,
        confidence=0.8,
    ),
    _Pattern(
        category="crypto",
        severity=Severity.MEDIUM,
        rule_id="sec-crypto-001",
        message="使用了已被攻破的哈希算法（MD5/SHA1），碰撞可被构造",
        suggestion="口令用 bcrypt/argon2；完整性校验用 SHA-256 及以上",
        regex=_rx(r"\b(?:hashlib\.)?(?:md5|sha1)\s*\("),
        lanes=SEC,
        confidence=0.7,
        unless=("usedforsecurity", "tests/"),
    ),
    _Pattern(
        category="crypto",
        severity=Severity.HIGH,
        rule_id="sec-crypto-001",
        message="关闭了 TLS 证书校验，中间人可无感截获全部流量",
        suggestion="去掉 verify=False；自签证书应把 CA 装进信任链而不是跳过校验",
        regex=_rx(r"\bverify\s*=\s*False"),
        lanes=SEC,
        confidence=0.9,
    ),
    _Pattern(
        category="insecure_random",
        severity=Severity.HIGH,
        rule_id="sec-random-001",
        message="用非密码学随机数生成安全敏感值，输出可被预测",
        suggestion="改用 secrets 模块（secrets.token_urlsafe / secrets.choice）",
        regex=_rx(r"\brandom\.(?:random|randint|choice|choices|randrange|sample)\s*\("),
        lanes=SEC,
        confidence=0.75,
        unless=("secrets.", "SystemRandom", "#"),
    ),
    _Pattern(
        category="path_traversal",
        severity=Severity.HIGH,
        rule_id="sec-pathtraversal-001",
        message="文件路径由变量拼接而成，可用 ../ 逃出预期目录",
        suggestion="os.path.realpath 后校验前缀，或改用白名单映射到固定路径",
        # 两条分支：f-string 里的插值，以及 os.path.join 的最后一个参数是变量
        # （不是字面量）。后者会误报「拼接两个内部变量」的写法，所以置信度压到 0.7 ——
        # 这是有意的取舍：路径拼接是少数几类「宁可多问一句」的代码。
        regex=_rx(r"(?:\bopen\s*\(\s*f[\"'][^\"']*\{)|(?:\bos\.path\.join\s*\([^)]*[,\s][a-z_]\w*\s*\))"),
        lanes=SEC,
        confidence=0.7,
    ),
    _Pattern(
        category="ssrf",
        severity=Severity.HIGH,
        rule_id="sec-ssrf-001",
        message="请求的 URL 来自变量，若可控则可访问内网元数据等内部服务",
        suggestion="对目标做白名单校验，禁止解析到私有网段和 169.254.0.0/16",
        regex=_rx(r"\b(?:requests|httpx|aiohttp)\.(?:get|post|put|head|request)\s*\(\s*(?![\"'])"),
        lanes=SEC,
        confidence=0.6,
        unless=('f"', "f'", "BASE_URL", "base_url", "self._base", "settings."),
    ),
    _Pattern(
        category="xss",
        severity=Severity.HIGH,
        rule_id="sec-xss-001",
        message="把内容直接当 HTML 插入，未转义的输入可以执行脚本",
        suggestion="改用 textContent；确需渲染 HTML 就先过 DOMPurify 一类的白名单清洗",
        regex=_rx(r"\b(?:innerHTML|outerHTML|document\.write|dangerouslySetInnerHTML|v-html)\b"),
        lanes=SEC,
        confidence=0.8,
    ),
    # -- 性能 -------------------------------------------------------------- #
    _Pattern(
        category="unbounded_query",
        severity=Severity.MEDIUM,
        rule_id="perf-unbounded-001",
        message="无上限的查询：结果集大小由数据量决定，表一大就 OOM",
        suggestion="加 LIMIT / 分页；确实需要全量就改成流式游标",
        # 刻意**不含** fetchall：它说的是「把结果取回来」，而不是
        # 「查询没有边界」—— 一句 `LIMIT 10` 之后的 fetchall 是完全正确的写法，
        # 而单看这一行无法区分。`SELECT *` 和 ORM 的 .all() 才是真正的信号。
        regex=_rx(r"\b(?:SELECT\s+\*|\.all\s*\(\s*\)|\.scalars\s*\()"),
        lanes=PERF,
        confidence=0.6,
        unless=("#", "count(", "limit", "LIMIT"),
    ),
    _Pattern(
        category="blocking_io",
        severity=Severity.HIGH,
        rule_id="perf-blocking-001",
        message="在异步上下文里做同步阻塞调用，会卡住整个事件循环",
        suggestion="改用 await asyncio.to_thread(...) 或对应的异步客户端",
        regex=_rx(r"\btime\.sleep\s*\("),
        lanes=PERF,
        confidence=0.85,
    ),
    _Pattern(
        category="quadratic",
        severity=Severity.MEDIUM,
        rule_id="perf-quadratic-001",
        message="循环里做字符串累加，每次都会复制整个已有字符串",
        suggestion="先 append 到列表，循环结束后 ''.join(parts)",
        # 注意要允许 f-string 前缀：`text += f"{x}"` 是最常见的形态，
        # 只写 ["'] 会漏掉它 —— 而漏掉的表现是「这条规则从来没触发过」，
        # 很容易被当成「代码里没有这种写法」。
        regex=_rx(r"^\s*\w+\s*\+=\s*f?[\"']"),
        lanes=PERF,
        confidence=0.7,
    ),
    _Pattern(
        category="repeated_work",
        severity=Severity.LOW,
        rule_id="perf-repeated-001",
        message="循环条件里每次都重新计算长度，多数情况下长度是不变的",
        suggestion="循环外先算一次；确需动态求值就写清楚原因",
        regex=_rx(r"\bfor\s+\w+\s+in\s+range\s*\(\s*len\s*\("),
        lanes=PERF,
        confidence=0.55,
    ),
    # -- 风格 -------------------------------------------------------------- #
    _Pattern(
        category="dead_code",
        severity=Severity.LOW,
        rule_id="style-deadcode-001",
        message="print 调试残留，进入主干会污染标准输出",
        suggestion="改用 logging，或直接删掉",
        regex=_rx(r"^\s*print\s*\("),
        lanes=STY,
        confidence=0.7,
    ),
    _Pattern(
        category="error_handling",
        severity=Severity.MEDIUM,
        rule_id="style-errhandling-001",
        message="裸 except 会吞掉包括 KeyboardInterrupt 在内的所有异常",
        suggestion="至少写 except Exception，并记录日志",
        regex=_rx(r"^\s*except\s*:"),
        lanes=STY,
        confidence=0.85,
    ),
    _Pattern(
        category="error_handling",
        severity=Severity.MEDIUM,
        rule_id="style-errhandling-001",
        message="可变对象作为默认参数，会在多次调用之间被共享和累积",
        suggestion="默认值写 None，函数体里再 if x is None: x = []",
        regex=_rx(r"\bdef\s+\w+\s*\([^)]*=\s*(?:\[\s*\]|\{\s*\})"),
        lanes=STY,
        confidence=0.9,
    ),
    _Pattern(
        category="docs",
        severity=Severity.INFO,
        rule_id="style-docs-001",
        message="提交里留下了未完成的待办标记",
        suggestion="转成 issue 跟踪，或补上引用说明它为什么还不能做",
        regex=_rx(r"\b(?:TODO|FIXME|XXX|HACK)\b"),
        lanes=STY,
        confidence=0.6,
    ),
    _Pattern(
        category="naming",
        severity=Severity.LOW,
        rule_id="style-naming-001",
        message="变量名是单个字母，读者无法从名字推断它的含义",
        suggestion="改成能读出用途的名字；循环下标用 i/j/k 可以保留",
        regex=_rx(r"^\s*([a-hln-rt-z])\s*=\s*\S"),
        lanes=STY,
        confidence=0.5,
    ),
    _Pattern(
        category="complexity_readability",
        severity=Severity.LOW,
        rule_id="style-readability-001",
        message="用 == None 与 None 比较，语义上应使用 is None",
        suggestion="改成 is None / is not None",
        regex=_rx(r"[=!]=\s*None\b"),
        lanes=STY,
        confidence=0.85,
    ),
)

#: 循环体里出现这些调用就是 N+1 —— 每条都会打一次数据库或网络。
#
#: 第二行是按命名惯例猜的（``get_*`` / ``list_*`` / ``fetch_*`` ...）。
#: 它会有误报：循环里调一个纯内存的 ``get_name(x)`` 也会命中。
#: 保留它的理由是**循环上下文已经滤掉了绝大多数噪音**，而漏掉
#: ``list_orders()`` 这种业务命名的查询，代价是整个性能 Worker 最有价值的
#: 一条检测形同虚设。宁可多问一句。
_N_PLUS_ONE_CALL = _rx(
    r"\b(?:\.query\s*\(|\.filter\s*\(|\.find\s*\(|\.find_one\s*\(|\.all\s*\(|"
    r"\.execute\s*\(|session\.get\s*\(|requests\.(?:get|post)\s*\(|httpx\.(?:get|post)\s*\(|"
    r"\b(?:get|list|fetch|load|find|query|count|select)_[a-z_]\w*\s*\()"
)
# 刻意**不含**裸 ``.get(``：它绝大多数时候是 dict.get / 缓存查找，
# 而在循环里「先批量取回、再在内存里查表」正是**修好 N+1 之后**的标准写法。
# 把它算成 N+1，等于精确地在正确代码上报错。
# ORM 的 session.get 单独列出，因为那个确实是查询。
_LOOP_HEAD = re.compile(r"^(\s*)(?:for|while)\s+(.+?)\s*:\s*$")

#: 长行阈值。超过就报 formatting —— 这是唯一一条不看语义的规则，误报率最低。
MAX_LINE_LENGTH = 120

#: N+1 和长行检查都不是表驱动的一条 ``_Pattern``（前者要上下文，后者不看语义），
#: 但它们同样得遵守 lane 过滤，所以各给一个哨兵用来查 ``lanes``。
_N_PLUS_ONE_SPEC = _Pattern(
    category="n_plus_one",
    severity=Severity.HIGH,
    rule_id="perf-nplus1-001",
    message="",
    suggestion="",
    regex=_N_PLUS_ONE_CALL,
    lanes=PERF,
)
_LONG_LINE_SPEC = _Pattern(
    category="formatting",
    severity=Severity.INFO,
    rule_id=None,
    message="",
    suggestion="",
    regex=re.compile(r""),
    lanes=STY,
)

#: 故障注入的形态。**每一种对应修复阶梯的一级**，这不是巧合：
#: 不能复现的坏输出等于没测过。三种坏法各自只能被一级救回来，
#: 所以「跑一遍全部形态」就等于把整条阶梯走了一遍。
FAILURE_MODES: tuple[str, ...] = (
    "fenced",  # ```json 围栏      → L1 配平扫描
    "prose",  # 前后有解释文字      → L1
    "truncated",  # 写到一半 token 用完 → L1 抢救完整条目
    "trailing_comma",  # 尾逗号        → L2 清理
    "smart_quotes",  # 全角引号        → L2 清理
    "single_quotes",  # Python 单引号   → 只能走 L3 修复调用
    "unquoted_keys",  # Python 字典风格 → 只能走 L3
)


def _placeholder_like(value: str) -> bool:
    """凭据检测的误报过滤。

    文档、示例配置、CI 模板里全是形如 ``password = "your-password-here"`` 的
    东西。它们**看起来**完全像硬编码凭据，但报出来只会稀释真实告警 ——
    而演示时最刺眼的就是一条假警报。
    """
    lowered = value.lower()
    return any(hint in lowered for hint in ("xxx", "your", "example", "sample", "dummy", "test", "1234"))


class MockLLM:
    """见模块文档。实现 ``LLMProvider`` 协议。"""

    name = "mock"

    def __init__(
        self,
        *,
        model: str = "mock-1",
        worker_types: Collection[WorkerType] | None = None,
        failure_rate: float = 0.0,
        delay_ms: int = 0,
    ) -> None:
        self.model = model
        #: ``None`` = 三种都报。完整模式下每个 Worker 只传自己那一种，
        #: 这样 Mock 也会遵守「各报各的lane」，与真实 prompt 的约束一致。
        self.worker_types = frozenset(worker_types) if worker_types is not None else None
        self.failure_rate = max(0.0, min(1.0, failure_rate))
        self.delay_ms = delay_ms

    # -- LLMProvider -------------------------------------------------------- #

    async def complete(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> LLMResponse:
        started = time.perf_counter()
        if self.delay_ms:
            await asyncio.sleep(self.delay_ms / 1000)

        findings = list(self._detect(user))
        payload = {"findings": [self._as_llm_dict(f) for f in findings]}
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        finish_reason = "stop"

        # 故障注入的随机源**由内容决定**，不是全局随机。
        # 同一份 diff 每次都坏成同一个样子 —— 否则「这个 fixture 能不能被救回来」
        # 这种断言根本没法写。
        #
        # 下面抑制 S311 的理由：那条规则说别用 Mersenne Twister 做密码学用途，
        # 完全正确 —— 但这里的用途恰恰相反，我们要的是**可复现**而不是不可预测。
        # 换成 secrets 会让故障注入变成不可复现，把这条测试毁掉。
        rng = random.Random(stable_hash(user, str(self.failure_rate)))  # noqa: S311
        if self.failure_rate and rng.random() < self.failure_rate:
            text, finish_reason = _inject_failure(text, rng.choice(FAILURE_MODES))
            log.debug("mock.failure_injected", mode=finish_reason)

        return LLMResponse(
            text=text,
            model=self.model,
            tokens_in=estimate_tokens(system + user),
            # 注意这里没有按估算的「真实」输出长度算，而是按完整响应算：
            # 截断的响应实际产出的 token 更少，但它消耗的预算上限是 max_tokens。
            tokens_out=estimate_tokens(text),
            cached_tokens=0,  # Mock 没有前缀缓存，报一个假的命中率会污染评测
            latency_ms=int((time.perf_counter() - started) * 1000),
            finish_reason=finish_reason,
        )

    # -- 检测 --------------------------------------------------------------- #

    def _lane_ok(self, pattern: _Pattern) -> bool:
        return self.worker_types is None or any(w in self.worker_types for w in pattern.lanes)

    def _detect(self, prompt: str) -> Iterator[Finding]:
        """扫提示词里的新增行，产出 finding。

        **直接扫整个 user 提示词而不是先切出 diff 段**：``iter_added_lines``
        只认 ``diff --git`` / ``--- `` 头部，而提示词里的规则段落是
        ``- [id] 标题`` 这种形式，不会被误认成文件头。少一次「切出 diff 段」
        的字符串处理，就少一处能出错的地方。
        """
        added = list(iter_added_lines(prompt))
        seen: set[tuple[str, int, str]] = set()

        for item in added:
            # 长行单独处理并**短路**：一行又长又含 SQL 拼接时只报长行，
            # 否则同一行刷出三条 finding，把真正的告警淹没在格式噪音里。
            if len(item.content) > MAX_LINE_LENGTH:
                key = (item.path, item.line, "formatting")
                if self._lane_ok(_LONG_LINE_SPEC) and key not in seen:
                    seen.add(key)
                    yield self._long_line_finding(item)
                continue

            for pattern in self._detect_line(item):
                key = (item.path, item.line, pattern.category)
                if key in seen:
                    continue
                seen.add(key)
                yield self._make(pattern, item)

        for finding in self._detect_n_plus_one(list(iter_diff_lines(prompt))):
            key = (finding.file, finding.line, finding.category)
            if key not in seen:
                seen.add(key)
                yield finding

    @staticmethod
    def _long_line_finding(item: DiffLine) -> Finding:
        return Finding(
            file=item.path,
            line=item.line,
            severity=Severity.INFO,
            category="formatting",
            message=f"单行长度 {len(item.content)} 超过 {MAX_LINE_LENGTH}，需要横向滚动才能读完",
            evidence=item.content.strip()[:120],
            confidence=0.4,
            suggestion="按语义断行；过长的表达式通常也意味着它做了不止一件事",
        )

    def _detect_line(self, item: DiffLine) -> Iterator[_Pattern]:
        text = item.content
        for pattern in PATTERNS:
            if not self._lane_ok(pattern) or pattern.regex.search(text) is None:
                continue
            if any(hint.lower() in text.lower() for hint in pattern.unless):
                continue
            if pattern.category == "secrets" and _placeholder_like(text):
                continue
            yield pattern

    def _detect_n_plus_one(self, lines: Sequence[DiffLine]) -> Iterator[Finding]:
        """循环体里的数据库/网络调用 = N+1。

        这条规则**无法用单行正则表达**：它需要「上文的循环」这个上下文。
        花了二十行代码单独实现，因为 N+1 是性能 Worker 最值钱的一条检测 ——
        它也是唯一一条「改了之后真能快一个数量级」的建议。

        **吃的是全部 diff 行，不只是新增行。** 真实 PR 里 ``for`` 那行通常
        原本就在，这次新增的只有循环体 —— 只看新增行的话，最典型的那类
        N+1 恰好永远检测不到。所以循环上下文可以来自上下文行，
        但**报出来的那行必须是新增行**（意见要挂在这次改动上）。
        """
        if not self._lane_ok(_N_PLUS_ONE_SPEC):
            return

        loop_indent: int | None = None
        path = ""
        last_line = 0

        for item in lines:
            # 换文件、或行号出现跳变（hunk 之间被略过的部分）时，
            # 之前记下的循环上下文已经不可信了
            if item.path != path or item.line - last_line > 1:
                loop_indent = None
                path = item.path
            if not item.is_removed:
                last_line = item.line

            if item.is_removed or not item.content.strip():
                continue

            text = item.content
            indent = len(text) - len(text.lstrip())

            if (head := _LOOP_HEAD.match(text)) is not None:
                loop_indent = len(head.group(1))
                continue

            if loop_indent is not None and indent <= loop_indent:
                loop_indent = None  # 缩进回到循环同级或更浅 —— 循环结束了

            in_loop_body = loop_indent is not None and indent > loop_indent
            if in_loop_body and item.is_added and _N_PLUS_ONE_CALL.search(text) is not None:
                loop_indent = None  # 一个循环只报一次
                yield Finding(
                    file=item.path,
                    line=item.line,
                    severity=Severity.HIGH,
                    category="n_plus_one",
                    message="循环体内每次都发起一次查询/请求，往返次数随数据量线性增长",
                    evidence=text.strip()[:200],
                    confidence=0.8,
                    suggestion=f"把第 {item.line} 行的调用提到循环外做批量查询，循环里只做内存查表",
                    rule_id="perf-nplus1-001",
                )

    # -- 组装 --------------------------------------------------------------- #

    def _make(self, pattern: _Pattern, item: DiffLine) -> Finding:
        return Finding(
            file=item.path,
            line=item.line,
            severity=pattern.severity,
            category=pattern.category,
            message=pattern.message,
            evidence=item.content.strip()[:200] or None,
            confidence=pattern.confidence,
            suggestion=pattern.suggestion,
            rule_id=pattern.rule_id,
        )

    @staticmethod
    def _as_llm_dict(finding: Finding) -> dict[str, Any]:
        """转成模型会吐出来的那种字典。

        ``source_line_verified`` 必须排除：它是 Worker 回填的，不在 LLM 的输出
        契约里。Mock 泄漏这个字段会让契约测试失效 —— 测试会以为模型真的会报它。
        """
        data = finding.model_dump(mode="json", exclude={"source_line_verified", "fingerprint"})
        return {k: v for k, v in data.items() if v is not None}


def _inject_failure(text: str, mode: str) -> tuple[str, str]:
    """把干净的 JSON 弄坏，模拟真实模型的各种抽风。

    返回 ``(坏文本, 说明)``。说明会写进 ``finish_reason`` 之外的日志，
    让「这次是哪种坏法」在排查时可见。
    """
    if mode == "fenced":
        return f"```json\n{text}\n```", "stop"
    if mode == "prose":
        return f"好的，我已经完成了审查。结果如下：\n\n{text}\n\n希望这些建议对你有帮助。", "stop"
    if mode == "trailing_comma":
        return text.rstrip()[:-1].rstrip() + ",\n}", "stop"
    if mode == "smart_quotes":
        return text.replace('": ', "”: ").replace(', "', ", “"), "stop"
    if mode == "truncated":
        # 砍在数组中间 —— 最真实的一种：模型写着写着 token 用完了
        return text[: max(1, int(len(text) * 0.7))], "length"
    if mode == "single_quotes":
        return text.replace('"', "'"), "stop"
    if mode == "unquoted_keys":
        return _UNQUOTED_KEY_RE.sub(r"\1:", text), "stop"
    return text, "stop"


#: Python 字典风格的键：``"file": `` → ``file: ``。模型偶尔会按 Python 的字面量
#: 语法输出。它**不能**用「去引号」之类的清理救回来 —— 那会连字符串值的引号
#: 一起动到 —— 所以只能走 L3 修复调用。
_UNQUOTED_KEY_RE = re.compile(r'"([a-z_]+)":')
