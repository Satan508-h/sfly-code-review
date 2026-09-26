/**
 * 时间线的显示逻辑：把事件翻译成一句人读的话，以及从事件流推出
 * 「七个节点跑到哪了」。
 *
 * 全是纯函数，所以能直接测 —— 而这里恰恰值得测：「进度条算错了」和
 * 「摘要读起来不对」在界面上都不会抛异常，只会安静地展示错的结论。
 */

import type { EventKind, RunEvent, RunStatus } from '@/api/client'

import { fmtDuration, fmtInt, NODES } from './format'

// --------------------------------------------------------------------------- //
// 一句话摘要
// --------------------------------------------------------------------------- //

/** payload 里的值可能是任何东西（它来自 jsonb），取值一律走这两个 helper。 */
function str(payload: Record<string, unknown>, key: string): string | null {
  const v = payload[key]
  return typeof v === 'string' && v ? v : null
}
function num(payload: Record<string, unknown>, key: string): number | null {
  const v = payload[key]
  return typeof v === 'number' ? v : null
}
function bool(payload: Record<string, unknown>, key: string): boolean | null {
  const v = payload[key]
  return typeof v === 'boolean' ? v : null
}

/**
 * 事件的摘要。
 *
 * **每一句都只说 payload 里真有的东西。** 不做任何推断 —— 比如
 * `worker.result` 不会去猜「它是不是第一条结果」，那种猜测在事件重放过的
 * run 上（同一条事件会出现多次）一定会得出错误的时间线。
 *
 * 取不到字段时返回空串，由调用方决定不显示，而不是拼出一句缺胳膊少腿的话。
 */
export function summarize(event: RunEvent): string {
  const p = event.payload
  switch (event.kind) {
    case 'run.created': {
      const repo = str(p, 'repo_id') ?? '?'
      const pr = num(p, 'pr_number')
      const files = num(p, 'files')
      const author = str(p, 'pr_author')
      const title = str(p, 'pr_title')
      const parts = [`${repo}#${pr ?? '?'}`]
      if (files !== null) parts.push(`${files} 个文件`)
      if (author) parts.push(`作者 ${author}`)
      if (title) parts.push(`「${title}」`)
      return parts.join(' · ')
    }

    case 'node.finished': {
      const node = str(p, 'node') ?? '?'
      if (node === 'plan') {
        const parts: string[] = []
        const workers = p['planned_workers']
        if (Array.isArray(workers)) parts.push(`${workers.length} 个 Worker`)
        const reviewed = num(p, 'files_reviewed')
        const total = num(p, 'files_total')
        if (reviewed !== null && total !== null) parts.push(`审查 ${reviewed}/${total} 个文件`)
        if (bool(p, 'diff_truncated') === true) parts.push('diff 已裁剪')
        return parts.join(' · ')
      }
      return node
    }

    case 'worker.dispatched': {
      const wt = str(p, 'worker_type') ?? '?'
      const rules = num(p, 'rules')
      const files = num(p, 'files')
      const parts = [wt]
      if (files !== null) parts.push(`${files} 个文件`)
      if (rules !== null) parts.push(`${rules} 条规则`)
      return parts.join(' · ')
    }

    case 'worker.result': {
      const wt = str(p, 'worker_type') ?? '?'
      const status = str(p, 'status') ?? '?'
      const findings = num(p, 'findings')
      const latency = num(p, 'latency_ms')
      const parts = [wt, status]
      if (findings !== null) parts.push(`${findings} 条发现`)
      if (latency !== null) parts.push(fmtDuration(latency))
      return parts.join(' · ')
    }

    case 'worker.failed': {
      const wt = str(p, 'worker_type') ?? '?'
      const cls = str(p, 'error_class')
      const err = str(p, 'error')
      return [wt, cls, err].filter(Boolean).join(' · ')
    }

    case 'aggregate.done': {
      const parts: string[] = []
      const findings = num(p, 'findings')
      const suppressed = num(p, 'suppressed')
      const conflicts = num(p, 'conflicts')
      if (findings !== null) parts.push(`${findings} 条发现`)
      // 「被拦下 0 条」和「没有这个字段」是两件事，分开处理
      if (suppressed !== null && suppressed > 0) parts.push(`拦下 ${suppressed} 条`)
      if (conflicts !== null) parts.push(`${conflicts} 处冲突`)
      if (bool(p, 'degraded') === true) {
        const missing = p['missing_workers']
        const names = Array.isArray(missing) ? missing.join('/') : ''
        parts.push(names ? `降级（缺 ${names}）` : '降级')
      }
      const tokens = num(p, 'tokens')
      if (tokens !== null) parts.push(`${fmtInt(tokens)} tokens`)
      return parts.join(' · ')
    }

    case 'publish.done': {
      const parts: string[] = []
      const form = str(p, 'form')
      if (form) parts.push(form)
      const inline = num(p, 'inline_sent')
      const skipped = num(p, 'inline_skipped')
      if (inline !== null) parts.push(`${inline} 条行内评论`)
      if (skipped !== null && skipped > 0) parts.push(`跳过 ${skipped} 条行内`)
      const id = num(p, 'comment_id')
      if (id !== null) parts.push(`评论 #${id}`)
      // posted=false 有两种来源：没配 token（本来就不打算发）和真发失败了。
      // 这里只说事实，判断交给用户 —— 而 `form=dry_run` 已经说明了是哪一种。
      if (bool(p, 'posted') === false) parts.push('未发布')
      return parts.join(' · ')
    }

    case 'publish.failed': {
      const reason = str(p, 'reason')
      const retryable = bool(p, 'retryable')
      const parts = [reason ?? '发布失败']
      if (retryable === true) parts.push('可重试')
      if (retryable === false) parts.push('不可重试')
      return parts.join(' · ')
    }

    case 'run.finished': {
      const status = str(p, 'status') ?? '?'
      const parts = [status]
      const findings = num(p, 'findings')
      if (findings !== null) parts.push(`${findings} 条发现`)
      const duration = num(p, 'duration_ms')
      if (duration !== null) parts.push(fmtDuration(duration))
      if (bool(p, 'degraded') === true) parts.push('降级')
      return parts.join(' · ')
    }

    case 'run.status':
      return str(p, 'status') ?? ''

    case 'node.started':
      return str(p, 'node') ?? ''

    default:
      return ''
  }
}

// --------------------------------------------------------------------------- //
// 节点进度
// --------------------------------------------------------------------------- //

export type NodeState = 'pending' | 'active' | 'done' | 'failed'

export interface NodeProgress {
  node: string
  state: NodeState
}

/**
 * 每个节点的完成由**哪一种事件**标记。
 *
 * 这不是猜的，是照着发事件的地方定的（`apps/orchestrator/sfly_orchestrator/nodes/`）：
 *
 *   * `ingest` 不单独发事件，它和 `run.created` 是同一拍（建 run 就是它干的）
 *   * `plan` / `finalize` 发 `node.finished`（只有这两个节点发）
 *   * `dispatch` 发 `worker.dispatched`（一条 lane 一条）
 *   * `wait` **什么都不发** —— 它的产物是「屏障闭合了」，而那件事的证据就是
 *     `aggregate.done` 出现了（aggregate 只在屏障闭合之后才会跑）
 *   * `aggregate` 发 `aggregate.done`
 *   * `publish` 发 `publish.done` / `publish.failed`
 *
 * 所以这张表和后端的节点实现是**一对必须一起改的东西**：新增一个节点却忘了
 * 在这里登记，症状是进度条永远停在前一个节点上，而没有任何东西报错。
 */
const COMPLETION: Record<string, (kind: EventKind, payload: Record<string, unknown>) => boolean> = {
  ingest: (kind) => kind === 'run.created',
  plan: (kind, p) => kind === 'node.finished' && p['node'] === 'plan',
  dispatch: (kind) => kind === 'worker.dispatched',
  wait: (kind) => kind === 'aggregate.done',
  aggregate: (kind) => kind === 'aggregate.done',
  finalize: (kind, p) => kind === 'node.finished' && p['node'] === 'finalize',
  publish: (kind) => kind === 'publish.done',
}

/**
 * 从事件流推出七个节点的状态。
 *
 * 三处刻意的处理：
 *
 * * **看一遍就够，不关心顺序。** 重放过的 run 里同一种事件会出现多次
 *   （实测一个 run 有 5 次 `aggregate.done`），用「出现过吗」比用「最后一次
 *   是什么」稳 —— 后者在事件乱序到达时会给出错的进度。
 * * **`publish.failed` 优先于 `publish.done`。** 一个 run 可能先失败再重发成功，
 *   那时两个事件都在；只要成功过就算成功（`run.finished` 里的 status 是权威，
 *   这里只看节点）。
 * * **不推断未来的节点。** run 停在 `plan` 就结束了（没有可审的文件）时，
 *   后面的节点是 `pending` 而不是 `failed` —— 它们**没跑**，和失败了是两回事。
 */
export function nodeStates(events: readonly RunEvent[], status: RunStatus): NodeProgress[] {
  const done = new Set<string>()
  let publishFailed = false

  for (const e of events) {
    for (const [node, matches] of Object.entries(COMPLETION)) {
      if (matches(e.kind, e.payload)) done.add(node)
    }
    if (e.kind === 'publish.failed') publishFailed = true
  }

  const terminal = ['published', 'publish_failed', 'failed', 'skipped'].includes(status)

  return NODES.map((node) => {
    if (done.has(node)) return { node, state: 'done' as const }
    // publish 失败过且没成功过 —— 这是唯一一个「没完成」能确定是失败的位置
    if (node === 'publish' && publishFailed && !done.has('publish')) {
      return { node, state: 'failed' as const }
    }
    // 还没到终态：第一个没完成的节点就是当前正在跑的那个
    if (!terminal) {
      const allNodes = NODES as readonly string[]
      const firstPending = allNodes.find((n) => !done.has(n))
      if (node === firstPending) return { node, state: 'active' as const }
    }
    return { node, state: 'pending' as const }
  })
}

// --------------------------------------------------------------------------- //
// 筛选
// --------------------------------------------------------------------------- //

/** 流水线节点级的事件。「仅关键节点」用它把 15 条 worker.dispatched 折叠掉。 */
const KEY_KINDS: EventKind[] = [
  'run.created',
  'node.finished',
  'aggregate.done',
  'publish.done',
  'publish.failed',
  'run.finished',
]

export function isKeyEvent(event: RunEvent): boolean {
  return KEY_KINDS.includes(event.kind)
}
