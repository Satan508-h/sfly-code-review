"""``publish`` —— 把评论投出去。

### 三道闸，各挡一种**不同**的重复评论

1. **``review_runs.github_comment_id``** —— 数据库说发过了。挡的是正常的
   「节点被重放」（``interrupt()`` 的语义、checkpoint 恢复）。
2. **正文里的隐藏标记** ``<!-- sfly:run:{task_id} -->`` —— PR 上已经有这条正文了。
   挡的是第 1 道挡不住的那种：**评论发出去了、但写库那一步失败了**。
   那时数据库里没有 comment id，而 PR 上已经有一条 —— 只看数据库就会再发一遍。
3. **投递的幂等**（``mark_published`` 是 UPDATE，图的重放是常态）——
   这一层不需要额外的东西，它由「写的是同一行同一列」保证。

只做第 1 道，上面第二种情况会产出重复评论；只做第 2 道，每次重放都要多打
两次 API（查 review、查评论）。而重复评论是**用户可见**的噪音 ——
它比一次失败的发布难解释得多。

### 失败是「投递失败」，不是「审查失败」

发不出去时状态写 ``published`` 之外的那个终态 ``publish_failed``，**不是**
``failed``：报告已经落库了（``review_reports``），钱也花了，重算一遍只会再花一次。
两者在 UI 上的处置完全不同 —— 后者该有个「重新发布」按钮，前者该去查 Worker。

这个节点**绝不向上抛异常**：抛出去的表现是 run 停在 ``aggregating``，
而扫描器的 ``due_runs`` 只看 ``dispatched``/``waiting``（见那个方法的文档），
于是**没有任何东西能唤醒它**。所以失败也要走完「写状态 + 写事件」这两步。

### 阶梯式降级：被拒了就退一格重发，不去解析错误消息

GitHub 拒绝一次 review（422）有两个来源，而且**都可能出现**：

* ``Can not request changes on your own pull request`` —— 机器人账号和开 PR 的
  是同一个人。处置方式是改用 ``COMMENT``。
* 行号不在 diff 里（``path``/``line`` 指向未变更的行）—— 处置方式是去掉行内评论。

解析错误消息字符串来决定该怎么办是脆的（GitHub 改个措辞就失效），所以这里是
一个**从完整到保守的阶梯**：review+行内 → review → 普通评论。最后一格
（``POST /issues/{n}/comments``，不带任何行内）没有可以被拒的地方，
它还能失败就只剩权限和网络 —— 那两种都不该重试。

### 没有 token 就是 dry-run

``ctx.github is None`` 时一行 HTTP 都不发，正文照样渲染（``finalize`` 早就渲染好了）、
事件里写 ``posted=false`` 和原因。这**不是**失败路径：本地开发和 CI 因此不需要
任何密钥就能跑通全链路（见 README 的默认值那一段）。状态仍然是 ``published`` ——
一次不该发评论的运行，发了该发的东西（报告）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from sfly_agent.aggregate.render import clip_quote, marker_for
from sfly_agent.github import GitHubClient, GitHubError, GitHubValidationError, ReviewEvent
from sfly_agent.labels import SEVERITY_EMOJI, SEVERITY_LABEL, SEVERITY_ORDER, worker_label
from sfly_agent.state import ReviewState
from sfly_orchestrator.context import NodeContext
from sfly_shared.contracts import AggregatedFinding, FilePatch, ReviewReport, RunStatus
from sfly_shared.logging import get_logger

log = get_logger(__name__)

#: 行内评论上限。**和 ``render.MAX_LISTED`` 是同一个数字** —— 正文里最多列
#: 25 条，行内也不该超过它：一条没被列进正文的发现如果出现在行内，
#: 读者对不上账（「这条评论在说哪个问题？」）。
MAX_INLINE = 25


# --------------------------------------------------------------------------- #
# 投递的结果
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Outcome:
    """一次投递的结局。**只描述事实**，状态怎么写在 :func:`publish` 里。

    ``form`` 是这个项目里少见的「给人和 UI 看的字符串」，取值：
    ``review+inline`` / ``review`` / ``comment`` / ``already`` / ``adopted:review`` /
    ``dry_run``。它回答的是「这条评论是怎么出去的」—— 排查时第一个要问的问题，
    因为三种形式的权限要求、失败模式、以及读者看到的样子都不一样。
    """

    posted: bool
    form: str
    reason: str = ""
    comment_id: int | None = None
    inline_sent: int = 0
    #: 行号不在 diff 变更行上、因而不做行内评论的条数。**要说出来** ——
    #: 静默丢掉它们会让「行内只有 3 条」看起来像「只发现了 3 条」。
    inline_skipped: int = 0
    #: **这次投递失败了**（和 ``posted=False`` 不是一回事）。
    #:
    #: ``posted=False`` 有两种来源：dry-run（没配 token，本来就没打算发）和
    #: 真的发失败了。把这两件事混在一个字段里的后果是 —— dry-run 的 run 会被
    #: 记成 ``publish_failed``，于是「本地不需要密钥就能跑通全链路」这句话
    #: 在状态层面变成假的（每个 run 都带着一个红灯）。状态和事件类型
    #: 因此都由这个字段决定，而不是由 ``posted``。
    delivery_failed: bool = False
    #: 值得再点一次「重新发布」吗。只有失败时才有意义：
    #: 限流/5xx 是 True（过一会儿就好了），权限/422 是 False（点了只会再红一次）。
    retryable: bool | None = None


@dataclass(slots=True)
class _Attempt:
    """阶梯上的一格。``event`` 对 ``kind="comment"`` 没有意义（issue 评论没有事件）。"""

    kind: Literal["review", "comment"]
    event: ReviewEvent
    inline: list[dict[str, Any]] = field(default_factory=list)

    @property
    def label(self) -> str:
        if self.kind == "comment":
            return "comment"
        return f"review:{self.event.lower()}" + ("+inline" if self.inline else "")


# --------------------------------------------------------------------------- #
# 节点
# --------------------------------------------------------------------------- #


async def publish(state: ReviewState, ctx: NodeContext) -> dict[str, Any]:
    report = ReviewReport.model_validate(state["report"])
    task_id = report.task_id

    try:
        outcome = await _settle(state, ctx, report)
    except GitHubError as exc:
        # **绝不向上抛。** 抛出去的表现是 run 停在 ``aggregating``，而扫描器的
        # ``due_runs`` 只看 ``dispatched``/``waiting`` —— 没有任何东西能唤醒它。
        # 报告已经在库里了（``finalize`` 写的），这次失败影响的只是
        # 「评论有没有发出去」，那是可以事后重发的一件事。
        log.warning("node.publish.failed", task_id=task_id, error=str(exc), retryable=exc.retryable)
        outcome = _Outcome(
            posted=False,
            form="failed",
            reason=f"{type(exc).__name__}: {exc}",
            delivery_failed=True,
            retryable=exc.retryable,
        )

    if outcome.comment_id is not None:
        # 先记 id 再写状态：崩在中间的表现是「评论已发、状态还没到终态」——
        # 重放时第 1 道闸会认出来（**它认的是 id**），于是不会重发。
        # 反过来写的话，崩在中间会让下一次重放只能靠第 2 道闸（标记）认出来，
        # 那要多花两次 API，而且得指望标记没被人工编辑过。
        await ctx.store.mark_published(task_id, outcome.comment_id)

    # **状态先写，事件后写。顺序是刻意的**，因为两者之间必然有一个窗口
    # （两次写库不可能原子），而两种顺序的失败方向不一样：
    #
    # * 状态先 → 崩在中间：run 读作「已完成」而时间线少了最后一条。客户端跟着
    #   ``run.finished`` 事件走的话，它等到的是连接超时，然后读一次状态发现
    #   已经完成了 —— 安全的失败方向。
    # * 事件先 → 崩在中间：run 永远停在 ``aggregating``，而**没有任何东西能
    #   唤醒它** —— 扫描器的 ``due_runs`` 只看 ``dispatched``/``waiting``
    #   （见那个方法的文档）。一个永远不动的 run 比一条缺失的事件难解释得多。
    #
    # 所以断言「run 到终态了」之后不能立刻去读事件 —— 那不是测试写得不对，
    # 是这两件事本来就没有先后保证。
    await ctx.store.set_status(
        task_id, RunStatus.PUBLISH_FAILED if outcome.delivery_failed else RunStatus.PUBLISHED
    )
    await ctx.emit(
        task_id,
        "publish.failed" if outcome.delivery_failed else "publish.done",
        {
            "posted": outcome.posted,
            "form": outcome.form,
            "reason": outcome.reason,
            "comment_id": outcome.comment_id,
            "retryable": outcome.retryable,
            "pr_url": f"https://github.com/{report.repo_id}/pull/{report.pr_number}",
            "block_merge": report.block_merge,
            "findings": len(report.findings),
            "inline_sent": outcome.inline_sent,
            "inline_skipped": outcome.inline_skipped,
            "comment_chars": len(report.comment_body),
        },
    )
    await ctx.emit(
        task_id,
        "run.finished",
        {
            "status": (RunStatus.PUBLISH_FAILED if outcome.delivery_failed else RunStatus.PUBLISHED).value,
            "degraded": report.degraded,
            "findings": len(report.findings),
            "cost_usd": round(report.totals.cost_usd, 6),
            "duration_ms": report.totals.duration_ms,
        },
    )
    log.info(
        "node.publish",
        task_id=task_id,
        posted=outcome.posted,
        failed=outcome.delivery_failed,
        form=outcome.form,
        comment_id=outcome.comment_id,
        inline_sent=outcome.inline_sent,
        inline_skipped=outcome.inline_skipped,
        reason=outcome.reason,
        block_merge=report.block_merge,
    )
    return {}


async def _settle(state: ReviewState, ctx: NodeContext, report: ReviewReport) -> _Outcome:
    """判断这条正文要不要发、发到哪、怎么发。**只投递，不改任何状态。**"""
    run = await ctx.store.get_run(report.task_id)

    # 闸 1：数据库说已经发过了（正常重放路径）
    if run is not None and run.github_comment_id is not None:
        return _Outcome(
            posted=True,
            form="already",
            reason="数据库里已有 comment id（节点重放，不重发）",
            comment_id=run.github_comment_id,
        )

    github = ctx.github
    if github is None:
        # 没有 token = dry-run。**状态仍然是 published**：该做的事做完了，
        # 只是这次运行没打算发评论。见模块文档最后一段。
        return _Outcome(posted=False, form="dry_run", reason="没配 GITHUB_TOKEN（dry-run，正文只落库）")

    adopted = await _find_existing(github, report)
    if adopted is not None:
        form, comment_id = adopted
        return _Outcome(
            posted=True,
            form=f"adopted:{form}",
            reason="评论已经在 PR 上（上次发出去之后写库失败了）",
            comment_id=comment_id,
        )

    inline, skipped = _inline_comments(state, report)
    event = await _review_event(github, state, report)
    return await _send(github, report, inline, skipped, event)


async def _find_existing(github: GitHubClient, report: ReviewReport) -> tuple[str, int] | None:
    """闸 2：PR 上已经有带隐藏标记的正文吗。

    **查不动就当作没有**（记一条 warning 继续发）。理由是两种失败方向的代价不对等：
    查询失败说明 GitHub 的读接口对我们不可用，而紧接着的写请求走的是同一个
    API、同一个 token —— 它几乎必然也会失败。真出现「读失败、写成功」这种
    罕见组合，下一次运行的第 1 道闸会认出来。
    反过来（查不动就不发）的代价是那个 PR 永远拿不到评论。
    """
    marker = marker_for(report.task_id)
    try:
        return await github.find_marker(report.repo_id, report.pr_number, marker)
    except GitHubError as exc:
        log.warning(
            "node.publish.marker_lookup_failed", error=str(exc), note="按「没有」继续，写请求会再报一次"
        )
        return None


async def _review_event(github: GitHubClient, state: ReviewState, report: ReviewReport) -> ReviewEvent:
    """该发 ``REQUEST_CHANGES`` 还是 ``COMMENT``。

    **永不发 ``APPROVE``**（机器人审批人类 PR 是策略漏洞，见 ``decision.py``）；
    这里判断的是技术上**能不能**发：GitHub 禁止对自己的 PR 请求修改。

    先问一次「我是谁」比「发出去被 422 再降级」少一次注定的失败请求，
    而客户端的 ``whoami()`` 会把结果记住 —— 一次运行里只花一次。
    拿不到身份时照发 ``REQUEST_CHANGES``：那说明我们在的是**不确定**的状态，
    而阶梯的最后一格兜得住。降级是**可见**的（事件里的 ``form``），
    所以这不是「悄悄换个行为」，是「记录下来的退让」。
    """
    if not report.block_merge:
        return "COMMENT"

    author = str(state.get("bootstrap", {}).get("pr_author") or "")
    login = await github.whoami()
    if login and author and login.lower() == author.lower():
        log.info(
            "node.publish.own_pull_request",
            repo=report.repo_id,
            pr=report.pr_number,
            note="机器人就是 PR 作者本人，GitHub 不允许给自己的 PR 请求修改 → 发 COMMENT",
        )
        return "COMMENT"
    return "REQUEST_CHANGES"


async def _send(
    github: GitHubClient,
    report: ReviewReport,
    inline: list[dict[str, Any]],
    skipped: int,
    event: ReviewEvent,
) -> _Outcome:
    """按阶梯逐格试，直到有一格成功。

    **只对 422 往下走。** 403/404/429/5xx 直接抛给调用方（那就是
    ``publish_failed``）—— 它们是「这次不行」，不是「这样发不行」，
    退到最后一格也一样不行。
    """
    attempts: list[_Attempt] = [_Attempt("review", event, inline)]
    if inline:
        attempts.append(_Attempt("review", event))
    if event != "COMMENT":
        attempts.append(_Attempt("review", "COMMENT"))
    # 最后一格换个端点：普通评论没有行号可以不对，失败面最小。
    attempts.append(_Attempt("comment", "COMMENT"))

    last: GitHubValidationError | None = None
    for index, attempt in enumerate(attempts):
        try:
            data = await _post(github, report, attempt)
        except GitHubValidationError as exc:
            last = exc
            log.warning(
                "node.publish.rejected",
                form=attempt.label,
                status=exc.status_code,
                reason=exc.errors or str(exc),
                note="退到下一格重发",
            )
            continue
        return _Outcome(
            posted=True,
            form=attempt.label,
            # 降级是**可见**的：事件里的 form 和这句 reason 都要说清楚
            # 完整的那个形式被拒了，以及 GitHub 说了什么。
            reason=(
                ""
                if index == 0
                else f"完整形式被 GitHub 拒绝（HTTP {last.status_code if last else '?'}）"
                f"，降到 {attempt.label} 重发：{last.errors if last else ''}"
            ),
            comment_id=_comment_id(data),
            inline_sent=len(attempt.inline),
            inline_skipped=skipped,
        )

    assert last is not None  # 循环里没有 return 就说明每一格都抛了 422
    raise last


async def _post(github: GitHubClient, report: ReviewReport, attempt: _Attempt) -> dict[str, Any]:
    if attempt.kind == "comment":
        return await github.create_issue_comment(report.repo_id, report.pr_number, body=report.comment_body)
    return await github.create_review(
        report.repo_id,
        report.pr_number,
        body=report.comment_body,
        event=attempt.event,
        comments=attempt.inline,
    )


def _comment_id(data: dict[str, Any]) -> int | None:
    """响应里的 id。

    取不到就返回 ``None`` —— 那时评论**确实发出去了**，只是我们记不住它的 id，
    于是第 1 道闸失效、下一次重放要靠正文里的标记认出来（第 2 道）。
    这是可接受的降级，但要在日志里留痕（调用方的 ``comment_id=None`` 就是痕迹）。
    """
    raw = data.get("id")
    return int(raw) if isinstance(raw, int) else None


# --------------------------------------------------------------------------- #
# 行内评论
# --------------------------------------------------------------------------- #


def _inline_comments(state: ReviewState, report: ReviewReport) -> tuple[list[dict[str, Any]], int]:
    """把发现映射成行内评论，返回 ``(评论, 被跳过的条数)``。

    **行号只认 ``changed_lines``。** GitHub 会拒绝锚定在未变更行上的评论，
    而一次拒绝会让**整个** review 都不成立（连汇总正文一起）—— 所以这里
    宁可少发一条行内评论，也不赌。``line`` 对不上时那条发现仍然在汇总正文里
    （正文按严重度列出了每条 ``文件:行号``），读者不会漏掉它。

    构造这个集合的数据来自 ``plan`` 节点排好序的补丁（``state["file_patches"]``），
    也就是 Worker 收到的那一份 —— 两边用的是同一份 diff。
    """
    changed: dict[str, set[int]] = {}
    for raw in state.get("file_patches", []):
        patch = FilePatch.model_validate(raw)
        changed[patch.path] = set(patch.changed_lines)

    comments: list[dict[str, Any]] = []
    skipped = 0
    for finding in _by_severity(report.findings):
        if len(comments) >= MAX_INLINE:
            skipped += 1
            continue
        if finding.line not in changed.get(finding.file, set()):
            skipped += 1
            continue
        comments.append(
            {
                "path": finding.file,
                "line": finding.line,
                # 新文件那一侧。changed_lines 记的就是新文件的行号
                # （见 FilePatch 的文档），所以只能是 RIGHT。
                "side": "RIGHT",
                "body": _inline_body(finding),
            }
        )
    return comments, skipped


def _by_severity(findings: list[AggregatedFinding]) -> list[AggregatedFinding]:
    """严重的先发。

    行内评论有条数上限，而被丢掉的是**列表尾部** —— 所以顺序决定了
    「哪几条没被贴到代码旁边」。按严重度排（同级按置信度），
    丢掉的一定是最不重要的那些。
    """
    order = {severity: index for index, severity in enumerate(SEVERITY_ORDER)}
    return sorted(findings, key=lambda f: (order[f.severity], -f.adjusted_confidence))


def _inline_body(finding: AggregatedFinding) -> str:
    """单条行内评论的正文。

    比汇总里那一段**短**：它贴在代码旁边，读者已经有上下文了，
    不需要再重复「哪个文件、哪一行」。但「谁报出来的」必须留着 ——
    跨 Worker 印证是这个项目最值钱的产物。
    """
    lines = [
        f"{SEVERITY_EMOJI[finding.severity]} **{SEVERITY_LABEL[finding.severity]}**"
        f" · `{finding.category}` · 置信度 {finding.adjusted_confidence:.0%}"
        f" · {_sources(finding)}",
        "",
        finding.message,
    ]
    if finding.evidence:
        lines += ["", f"> 证据：`{clip_quote(finding.evidence)}`"]
    if finding.suggestion:
        lines += ["", f"> 建议：{clip_quote(finding.suggestion)}"]
    if finding.rule_id:
        lines += ["", f"> 依据规则：`{finding.rule_id}`"]
    if finding.needs_human_review:
        lines += ["", "> ⚖️ 与另一个 Worker 的判断存在冲突，**需要人工裁决**。"]
    return "\n".join(lines).rstrip()


def _sources(finding: AggregatedFinding) -> str:
    names = [worker_label(w) for w in finding.sources]
    if len(names) <= 1:
        return f"{names[0] if names else '未知'} Worker 报出"
    return f"{'、'.join(names)} 共 {len(names)} 个 Worker 独立报出"
