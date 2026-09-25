"""评论正文的渲染 —— Markdown、纯函数、不碰网络。

``finalize`` 节点调它，``publish`` 节点把结果投出去。**生成内容和投递是两件事**：
GitHub 会限流、会 401、会因为 diff 变了而 422，那时报告不该跟着一起丢 ——
正文已经在 ``review_reports.comment_body`` 里了，重新发布不需要重新聚合。

### 隐藏标记

正文第一行是 ``<!-- sfly:run:{task_id} -->``，Markdown 渲染后不可见。
它是防重复评论的第二道闸（第一道是 ``review_runs.github_comment_id``）：
两种失效方式各挡一种 ——

* ``github_comment_id`` 没写成功但评论发出去了（写库失败）→ 标记能认出来；
* 标记被别的机器人吃掉、或者正文被人工编辑过 → 数据库那列还认得出来。

只用一道闸的话，上面两种情况各会产出一次重复评论，而重复评论是**用户可见**的
噪音 —— 它比一次失败的发布难解释得多。

### 条数上限

``MAX_LISTED`` 是给超大 PR 准备的。一个改了 200 个文件的 PR 报出 60 条发现时，
一条 60 行的评论没有人会读完，而「读不完」的下一步是「不看」——
那等于全部意见都白提了。超出部分在正文里说清楚还剩多少条，
而不是静默截断（静默截断会让人以为报告就这些）。
"""

from __future__ import annotations

from sfly_agent.aggregate.decision import decision_text
from sfly_agent.labels import SEVERITY_EMOJI, SEVERITY_LABEL, SEVERITY_ORDER, worker_label
from sfly_shared.contracts import AggregatedFinding, ReviewReport

#: 正文里最多列多少条。超出的部分会有一个「另有 N 条」的说明。
MAX_LISTED = 25

#: 证据和建议在引用块里各自最多显示多少字符。LLM 偶尔会贴一大段代码进来 ——
#: 一段 200 行的引用会让整条评论变成一次滚动，而关键的那句被埋在里面。
MAX_QUOTE_CHARS = 300


def render_comment(report: ReviewReport) -> str:
    """把报告渲染成 PR 评论的 Markdown 正文。"""
    lines: list[str] = [
        f"<!-- sfly:run:{report.task_id} -->",
        "## 🤖 sfly 审查报告",
        "",
        _verdict(report),
        "",
    ]
    if report.degraded:
        lines += [_degraded_notice(report), ""]

    lines += [_summary_line(report), ""]

    if not report.findings:
        # 「没发现问题」和「没跑成」必须读起来完全不同。降级提示已经单独写了，
        # 所以这里可以说「没有发现」—— 读者能看见上面那一行。
        lines += ["未发现问题。", ""]
    else:
        lines += _findings_sections(report)

    lines += [_footer(report)]
    # **结尾不留换行。** ``_Contract`` 开了 ``str_strip_whitespace=True``，
    # 于是这份正文存进 jsonb 再读回来时首尾空白会被吃掉。留一个的话，
    # ``finalize`` 事件里的 ``comment_chars`` 和 ``publish`` 里的会差 1 ——
    # 一个看起来像 bug 却什么也不说明的差异（M5 实测踩到）。
    # 让它一开始就等于最终形态。
    return "\n".join(lines).rstrip()


def _verdict(report: ReviewReport) -> str:
    """**只有两种结论，而且都不是 APPROVE。**

    机器人审批人类 PR 是策略漏洞（见 ``decision.py`` 的模块文档）：
    一个被绕过的模型会拿到一个和人工审批长得一模一样的绿色标记。
    所以措辞刻意避开了「通过」「同意」这类词 —— 报告只表达
    「建议改」或「供参考」，合不合由人决定。
    """
    if report.block_merge:
        return f"**结论：🔴 建议修改后再合并** —— {decision_text(report.decision_reason)}"
    return f"**结论：💬 供参考** —— {decision_text(report.decision_reason)}"


def _degraded_notice(report: ReviewReport) -> str:
    """降级徽章的文字版。**必须在最上面** —— 它改变的是整份报告的可信度，
    而不只是某一条发现。"""
    if report.missing_workers:
        names = "、".join(worker_label(w) for w in report.missing_workers)
        return f"> ⚠️ **本次审查不完整**：{names} Worker 没有上报结果（超时或失败）。"
    return "> ⚠️ **本次审查不完整**：有 Worker 上报了失败结果。"


def _summary_line(report: ReviewReport) -> str:
    counts = dict.fromkeys(SEVERITY_ORDER, 0)
    for finding in report.findings:
        counts[finding.severity] += 1
    parts = [f"{SEVERITY_LABEL[s]} {counts[s]}" for s in SEVERITY_ORDER if counts[s]]
    summary = " · ".join(parts) if parts else "无"

    extra = f"，另有 {len(report.suppressed)} 条因置信度不足未列出" if report.suppressed else ""
    return f"共 **{len(report.findings)}** 条发现：{summary}{extra}"


def _findings_sections(report: ReviewReport) -> list[str]:
    lines: list[str] = []
    by_severity: dict[str, list[AggregatedFinding]] = {}
    for finding in report.findings:
        by_severity.setdefault(finding.severity.value, []).append(finding)

    listed = 0
    truncated_at = None
    for severity in SEVERITY_ORDER:
        group = by_severity.get(severity.value, [])
        if not group:
            continue
        remaining = MAX_LISTED - listed
        if remaining <= 0:
            truncated_at = severity
            break
        shown, hidden = group[:remaining], group[remaining:]
        lines += [f"### {SEVERITY_EMOJI[severity]} {SEVERITY_LABEL[severity]}（{len(group)}）", ""]
        lines += [_finding_block(f) for f in shown]
        listed += len(shown)
        if hidden:
            truncated_at = severity
            break

    if truncated_at is not None:
        lines += [f"*还有 {len(report.findings) - listed} 条未在此列出（正文上限 {MAX_LISTED} 条）。*", ""]
    return lines


def _finding_block(finding: AggregatedFinding) -> str:
    out = [
        f"**`{finding.file}:{finding.line}`** · `{finding.category}`"
        f" · 置信度 {finding.adjusted_confidence:.0%} · {_sources(finding)}",
        finding.message,
    ]
    if finding.evidence:
        out.append(f"> 证据：`{_clip(finding.evidence)}`")
    if finding.suggestion:
        out.append(f"> 建议：{_clip(finding.suggestion)}")
    if finding.rule_id:
        out.append(f"> 依据规则：`{finding.rule_id}`")
    if finding.needs_human_review:
        out.append("> ⚖️ 与另一个 Worker 的判断存在冲突，**需要人工裁决**。")
    out.append("")
    return "\n".join(out)


def _sources(finding: AggregatedFinding) -> str:
    """谁报出了这条。

    跨 Worker 印证是这个项目最值钱的产物，所以在正文里**显式说出来** ——
    「两个不同的 Worker 各自独立地发现了同一处问题」比「有两个来源」
    对读者的说服力完全不同。
    """
    names = [worker_label(w) for w in finding.sources]
    if len(names) <= 1:
        return f"{names[0] if names else '未知'} Worker 报出"
    return f"{'、'.join(names)} 共 {len(names)} 个 Worker 独立报出"


def _clip(text: str) -> str:
    """截断并**说明截断了**。一个戛然而止的引用看起来像渲染坏了。"""
    flat = " ".join(text.split())
    if len(flat) <= MAX_QUOTE_CHARS:
        return flat
    return flat[:MAX_QUOTE_CHARS] + "…（已截断）"


def _footer(report: ReviewReport) -> str:
    totals = report.totals
    parts = [
        f"{report.files_reviewed}/{report.files_total} 个文件",
        f"{totals.duration_ms / 1000:.1f}s",
        f"{totals.tokens_in + totals.tokens_out} tokens",
    ]
    if totals.cost_usd:
        parts.append(f"${totals.cost_usd:.4f}")
    if totals.cached_tokens:
        parts.append(f"缓存命中 {totals.cache_hit_rate:.0%}")
    return f"<sub>由 sfly 多 Agent 审查系统生成 · run `{report.task_id}` · {' · '.join(parts)}</sub>"
