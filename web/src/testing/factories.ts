/**
 * 测试用的假数据工厂。
 *
 * **住在 `src/` 下面而不是 `tests/`，有两个理由：**
 *
 * 1. `vue-tsc --noEmit` 只覆盖 `src/**` —— 放到外面的话，这个文件里的类型错误
 *    不会有人发现，而它恰恰是**给测试提供「形状正确」的数据**的那个文件。
 *    它一旦和契约漂移，测试会全绿而页面会烂掉。
 * 2. 它不会被这个文件所在的目录名决定是否进包：Vite 只看**入口图**，
 *    应用代码不 import 它，所以它一个字节都不会进 dist。
 *
 * 这和 Python 那边 `tests/factories.py` 的分工是一致的 —— 那边的 `webhook_payload()`
 * 造的是合成载荷（只进内存和桩），真实的那个在 `fixtures/webhook_pr.json`。
 */

import type { AggregatedFinding, ReviewReport, RunEvent, RunRow } from '@/api/client'

/** 一条时间线事件。默认是「一条普通的 Worker 上报」，测试只覆盖自己关心的字段。 */
export function runEvent(over: Partial<RunEvent> = {}): RunEvent {
  return {
    seq: 1,
    task_id: '01M3DVHP409TBPY721JN9VTA0P',
    kind: 'worker.result',
    payload: { worker_type: 'security', status: 'ok', findings: 2, latency_ms: 12 },
    created_at: '2026-09-26T03:19:51.134829Z',
    ...over,
  }
}

/** 一条发现的默认值。**默认值刻意选成「最普通的一条」**，测试只覆盖自己关心的字段。 */
export function aggregatedFinding(over: Partial<AggregatedFinding> = {}): AggregatedFinding {
  return {
    file: 'app/api.py',
    line: 32,
    end_line: null,
    severity: 'high',
    category: 'command_injection',
    message: '以 shell 方式执行命令，参数里的元字符会被 shell 解释',
    evidence: 'subprocess.run(f"tar -czf {target}.tgz {UPLOAD_DIR}", shell=True)',
    confidence: 0.9,
    suggestion: '去掉 shell=True 改为列表传参',
    rule_id: 'sec-cmdi-001',
    source_line_verified: true,
    fingerprint: null,
    adjusted_confidence: 0.6,
    sources: ['security'],
    corroboration_count: 1,
    cluster_id: null,
    needs_human_review: false,
    stage: 'clustered',
    conflict: null,
    ...over,
  }
}

export function runRow(over: Partial<RunRow> = {}): RunRow {
  return {
    task_id: '01M3DVHP409TBPY721JN9VTA0P',
    idempotency_key: 'demo/sfly-playground:1:80a87fd',
    repo_id: 'demo/sfly-playground',
    repo_node_id: 'R_demo',
    pr_number: 1,
    head_sha: '80a87fd8ff9ad2cb9e8d82e0665167a5bf85ad43',
    base_sha: 'dc5fd0ee24e641a8591b2107c12a45435c46519a',
    status: 'published',
    attempt: 1,
    files_total: 4,
    files_reviewed: 4,
    diff_truncated: false,
    planned_workers: ['security', 'performance', 'style'],
    missing_workers: [],
    deadline_at: '2026-09-26T03:42:29.823920Z',
    dispatched_at: '2026-09-26T03:19:51.114777Z',
    published_at: '2026-09-26T03:32:30.882661Z',
    github_comment_id: 5324355208,
    block_merge: true,
    degraded: false,
    totals: {
      tokens_in: 4733,
      tokens_out: 1399,
      cached_tokens: 0,
      cost_usd: 0,
      llm_calls: 0,
      duration_ms: 49,
      per_worker_ms: { security: 4, performance: 1, style: 2 },
    },
    created_at: '2026-09-26T03:19:51.061373Z',
    ...over,
  }
}

/**
 * 一个**完整跑完**的 run 的事件序列，照着 `GET /api/runs/{id}` 的真实响应抄的。
 *
 * 放在这里而不是各个 spec 自己写一份，是被同一个错误教会的：直接写在 spec 里的
 * 版本**两次都漏了 `finalize` 那条 `node.finished`**（只有 plan 和 finalize 会发），
 * 于是「七个节点全绿」那条断言连着红了两回，而红的原因和被测的东西毫无关系。
 *
 * 改动它的时候记住：这张表必须覆盖**每一个**节点的完成事件，
 * 否则 `nodeStates` 的断言会以「看起来像代码坏了」的方式失败。
 */
export function completeRunEvents(): RunEvent[] {
  return [
    runEvent({
      seq: 176,
      kind: 'run.created',
      payload: {
        files: 4,
        repo_id: 'Satan508-h/sfly-playground',
        head_sha: '80a87fd8ff9a3085aa93590930dd47ed1df32711',
        pr_title: '重构用户接口并加上备份入口',
        pr_author: 'Satan508-h',
        pr_number: 1,
      },
    }),
    runEvent({
      seq: 177,
      kind: 'node.finished',
      payload: {
        node: 'plan',
        deadline_at: '2026-09-26T03:29:51.114445+00:00',
        files_total: 4,
        diff_truncated: false,
        files_reviewed: 4,
        planned_workers: ['security', 'performance', 'style'],
      },
    }),
    runEvent({
      seq: 178,
      kind: 'worker.dispatched',
      payload: { files: 4, rules: 8, message_id: '1790392791120-0', worker_type: 'security' },
    }),
    runEvent({
      seq: 181,
      kind: 'worker.result',
      payload: { status: 'ok', findings: 2, latency_ms: 1, error_class: null, worker_type: 'performance' },
    }),
    runEvent({
      seq: 184,
      kind: 'aggregate.done',
      payload: {
        tokens: 6132,
        cost_usd: 0,
        degraded: false,
        findings: 14,
        conflicts: 0,
        suppressed: 2,
        missing_workers: [],
      },
    }),
    runEvent({
      seq: 185,
      kind: 'node.finished',
      payload: { node: 'finalize', block_merge: true, findings: 14, comment_chars: 3113 },
    }),
    runEvent({
      seq: 186,
      kind: 'publish.done',
      payload: {
        form: 'review:comment+inline',
        posted: true,
        findings: 14,
        comment_id: 5324355208,
        block_merge: true,
        inline_sent: 14,
        comment_chars: 3113,
        inline_skipped: 0,
      },
    }),
    runEvent({
      seq: 187,
      kind: 'run.finished',
      payload: { status: 'published', cost_usd: 0, degraded: false, findings: 14, duration_ms: 49 },
    }),
  ]
}

export function reviewReport(over: Partial<ReviewReport> = {}): ReviewReport {
  return {
    task_id: '01M3DVHP409TBPY721JN9VTA0P',
    repo_id: 'demo/sfly-playground',
    repo_node_id: 'R_demo',
    pr_number: 1,
    head_sha: '80a87fd8ff9ad2cb9e8d82e0665167a5bf85ad43',
    base_sha: 'dc5fd0ee24e641a8591b2107c12a45435c46519a',
    findings: [aggregatedFinding()],
    suppressed: [],
    conflicts: [],
    block_merge: true,
    decision_reason: 'secrets_found',
    degraded: false,
    missing_workers: [],
    files_total: 4,
    files_reviewed: 4,
    diff_truncated: false,
    totals: runRow().totals!,
    comment_body: '## sfly 审查报告\n\n...',
    created_at: '2026-09-26T03:32:30.882661Z',
    ...over,
  }
}
