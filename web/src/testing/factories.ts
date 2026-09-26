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

import type { AggregatedFinding, ReviewReport, RunRow } from '@/api/client'

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
