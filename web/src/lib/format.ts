/**
 * 显示层的纯函数：把契约里的取值翻译成人读的文案与颜色。
 *
 * **为什么单独放一个文件。** 这些映射会被列表页、详情页、时间线、冲突面板
 * 同时用到 —— 散在各个组件里，迟早会出现「同一个 severity 在三个地方三种
 * 颜色」，而**这种不一致不会有任何东西报错**，只会让人怀疑自己看错了。
 *
 * 附带好处：它们是纯函数，接 vitest 时不需要挂载任何组件就能测 —— 前端
 * 真正值得测的也正是这些（而不是把组件的 DOM 快照钉死）。
 */

import type { EventKind, RunStatus, Severity, WorkerType } from '@/api/client'

// --------------------------------------------------------------------------- //
// 严重度
// --------------------------------------------------------------------------- //

export const SEVERITY_LABEL: Record<Severity, string> = {
  critical: '严重',
  high: '高',
  medium: '中',
  low: '低',
  info: '提示',
}

/** 取值是 CSS 变量名而不是十六进制 —— 配色只在 main.css 里定义一次。 */
export const SEVERITY_VAR: Record<Severity, string> = {
  critical: '--sfly-critical',
  high: '--sfly-high',
  medium: '--sfly-medium',
  low: '--sfly-low',
  info: '--sfly-info',
}

/** 从重到轻。排序用，也是界面上展示的固定顺序。 */
export const SEVERITY_ORDER: Severity[] = ['critical', 'high', 'medium', 'low', 'info']

export function severityRank(s: Severity): number {
  return SEVERITY_ORDER.indexOf(s)
}

// --------------------------------------------------------------------------- //
// Worker
// --------------------------------------------------------------------------- //

export const WORKER_LABEL: Record<WorkerType, string> = {
  security: '安全',
  performance: '性能',
  style: '风格',
}

export const WORKER_VAR: Record<WorkerType, string> = {
  security: '--sfly-security',
  performance: '--sfly-performance',
  style: '--sfly-style',
}

// --------------------------------------------------------------------------- //
// run 状态
// --------------------------------------------------------------------------- //

export const RUN_STATUS_LABEL: Record<RunStatus, string> = {
  queued: '排队中',
  dispatched: '已派发',
  waiting: '等待 Worker',
  aggregating: '汇总中',
  published: '已发布',
  publish_failed: '发布失败',
  failed: '失败',
  skipped: '跳过',
}

export const RUN_STATUS_TYPE: Record<RunStatus, 'success' | 'warning' | 'danger' | 'info' | 'primary'> = {
  queued: 'info',
  dispatched: 'primary',
  waiting: 'primary',
  aggregating: 'warning',
  published: 'success',
  // 「发布失败」不是「审查失败」：报告已经落库、钱也花了，处置是「重新发布」。
  // 所以它是 warning 而不是 danger —— 颜色在这里承担的是**引导动作**的作用。
  publish_failed: 'warning',
  failed: 'danger',
  skipped: 'info',
}

/** 终态。和时间线那条一样 —— `publish_failed` 也算跑完了。 */
export const TERMINAL_STATUSES: RunStatus[] = ['published', 'publish_failed', 'failed', 'skipped']

export function isTerminal(status: RunStatus): boolean {
  return TERMINAL_STATUSES.includes(status)
}

// --------------------------------------------------------------------------- //
// 事件
// --------------------------------------------------------------------------- //

export const EVENT_LABEL: Record<EventKind, string> = {
  'run.created': 'run 创建',
  'run.status': '状态变更',
  'node.started': '节点开始',
  'node.finished': '节点完成',
  'worker.dispatched': '任务派发',
  'worker.result': 'Worker 上报',
  'worker.failed': 'Worker 失败',
  'aggregate.done': '聚合完成',
  'publish.done': '发布完成',
  'publish.failed': '发布失败',
  'run.finished': 'run 结束',
}

export const EVENT_TYPE: Record<EventKind, 'primary' | 'success' | 'warning' | 'danger' | 'info'> = {
  'run.created': 'info',
  'run.status': 'info',
  'node.started': 'info',
  'node.finished': 'primary',
  'worker.dispatched': 'info',
  'worker.result': 'primary',
  'worker.failed': 'danger',
  'aggregate.done': 'primary',
  'publish.done': 'success',
  'publish.failed': 'warning',
  'run.finished': 'success',
}

/**
 * 流水线节点的展示顺序 —— **和 README / 系统状态页上那张图是同一张**。
 *
 * 七个都要留着，包括 `ingest`：它虽然不单独发 `node.started`/`node.finished`
 * （建 run 就是它干的，所以证据是 `run.created`），但把它从进度条上抹掉的话，
 * 页面上的图就和文档里的图对不上了 —— 而那种不一致没人会报错。
 */
export const NODES = ['ingest', 'plan', 'dispatch', 'wait', 'aggregate', 'finalize', 'publish'] as const

export const NODE_LABEL: Record<string, string> = {
  ingest: '解析载荷',
  plan: '规划',
  dispatch: '派发',
  wait: '等待屏障',
  aggregate: '主 Agent 聚合',
  finalize: '生成报告',
  publish: '回写评论',
}

// --------------------------------------------------------------------------- //
// 数字与时间
// --------------------------------------------------------------------------- //

/** `80a87fd8ff9a…` → `80a87fd`。短码在界面上到处都是，认得出是哪次提交就够了。 */
export function shortSha(sha: string | null | undefined, n = 7): string {
  return sha ? sha.slice(0, n) : '—'
}

const pad = (n: number) => String(n).padStart(2, '0')

/**
 * 本地时区的 `MM-DD HH:mm:ss`。
 *
 * 后端全部是 UTC（`...Z`），浏览器会按本地时区渲染 —— 这是**故意的**：
 * 演示时页面上的时间和手机/系统时间对得上，比「和数据库里一致」重要得多。
 */
export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 「3 分钟前」。列表页扫一眼就知道哪些是刚跑完的。 */
export function fmtRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000)
  if (s < 60) return '刚刚'
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`
  return `${Math.floor(s / 86400)} 天前`
}

/**
 * 耗时的两种量级。
 *
 * `duration_ms` 是可以**跨天**的：一个 interrupt 挂起中被重投/恢复过的 run，
 * 它的耗时里含着那段等待（实测见过 758725 ms ≈ 12 分钟）。所以这里必须有
 * 「时:分:秒」那一档，否则界面会显示 `758725ms` 这种读不出来的数字。
 */
export function fmtDuration(ms: number | null | undefined): string {
  if (ms == null || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)} ms`
  const s = ms / 1000
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`
  const total = Math.round(s)
  const m = Math.floor(total / 60)
  if (m < 60) return `${m} 分 ${pad(total % 60)} 秒`
  return `${Math.floor(m / 60)} 时 ${pad(m % 60)} 分`
}

/** 千分位。token 数以万计时不加分隔符很难读。 */
export function fmtInt(n: number | null | undefined): string {
  if (n == null) return '—'
  return n.toLocaleString('zh-CN')
}

/**
 * 成本。
 *
 * **不足 1 分钱时不要显示 `$0.0000`** —— 那看起来像「没算」，而不是「很便宜」。
 * Mock LLM 下成本恒为 0，界面要说清楚这是没花钱而不是没测出来。
 */
export function fmtCost(usd: number | null | undefined): string {
  if (usd == null) return '—'
  if (usd === 0) return '$0'
  if (usd < 0.01) return `$${usd.toFixed(4)}`
  return `$${usd.toFixed(2)}`
}

/** 缓存命中率。**分母为 0 时是「—」而不是 0%**：那是「没有输入」不是「没命中」。 */
export function cacheHitRate(cached: number, total: number): string {
  if (!total) return '—'
  return `${((cached / total) * 100).toFixed(0)}%`
}
