import axios from 'axios'

/**
 * 后端基地址。
 *
 * 默认 `/api` 走相对路径 —— 完整模式下由 nginx（生产）或 Vite 的
 * devServer.proxy（开发）转发到 api:8000。**业务代码里永远不要出现
 * 硬编码的后端地址**，跨环境差异只在这一个地方处理。
 *
 * 精简模式（前端在 Vercel、后端在 Render）在构建时注入绝对地址：
 *   VITE_API_BASE=https://sfly-api.onrender.com/api npm run build
 */
export const API_BASE: string = import.meta.env.VITE_API_BASE ?? '/api'

export const http = axios.create({
  baseURL: API_BASE,
  // Render 免费版冷启动：容器休眠后第一个请求要等约 60 秒。
  // 超时给短了会在冷启动时直接失败，用户看到的是一个坏掉的页面。
  timeout: 90_000,
  headers: { 'Content-Type': 'application/json' },
})

// --------------------------------------------------------------------------- //
// 类型：与 packages/shared/sfly_shared/contracts.py 对应
//
// 手写而非代码生成 —— 契约只有十来个模型且很少变，生成器带来的构建
// 复杂度不划算。改了 contracts.py 记得同步这里。
//
// **字段名必须逐字对齐**，因为对不上不会报错：后端给 `task_id`、前端读
// `taskId` 的结果是一个 undefined，页面显示空白，控制台一句话都没有。
// 新增字段时的核对办法就是这个文件顶上的那句注释 —— 打开
// `GET /api/runs` 看一眼真实响应。
// --------------------------------------------------------------------------- //

export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info'
export type WorkerType = 'security' | 'performance' | 'style'
export type ResultStatus = 'ok' | 'partial' | 'failed'
export type RunStatus =
  | 'queued'
  | 'dispatched'
  | 'waiting'
  | 'aggregating'
  | 'published'
  | 'publish_failed'
  | 'failed'
  | 'skipped'

export type CheckStatus = 'ok' | 'down' | 'skipped'

/** 时间线事件的种类。契约里是一个 Literal，这里照抄 —— 加一个事件类型时两处都要改。 */
export type EventKind =
  | 'run.created'
  | 'run.status'
  | 'node.started'
  | 'node.finished'
  /** 去 GitHub 拉这个 PR 的文件失败了。**不是「没有可审的文件」** ——
   *  那条路是 `skipped`，而这条会重试，用完则整个 run 判 failed。 */
  | 'plan.fetch_failed'
  | 'worker.dispatched'
  | 'worker.result'
  | 'worker.failed'
  | 'aggregate.done'
  | 'publish.done'
  | 'publish.failed'
  | 'run.finished'

/**
 * 一个依赖的探测结果。
 *
 * `skipped` 不是 `down` 的同义词：精简模式下没有 Redis，那是正确状态而不是故障。
 * 后端为此专门用了三态而不是布尔 —— 前端也必须照着区分，否则健康页会在
 * 正确的部署上常亮红灯。
 */
export interface DependencyCheck {
  name: string
  status: CheckStatus
  ok: boolean
  detail: string
  latency_ms: number
}

export interface HealthResponse {
  ok: boolean
  service: string
  version: string
  mode: 'full' | 'lite'
  uptime_s: number
  config: {
    queue_backend: string
    lock_backend: string
    llm_provider: string
    wait_strategy: string
    conflict_resolver: string
    /** 只回显配没配，不回显密钥本身 —— 这两件事必须区分开。 */
    webhook_secret: 'configured' | 'missing'
  }
  checks: Record<string, DependencyCheck>
}

/** 一次 run 的成本与耗时汇总。 */
export interface RunTotals {
  tokens_in: number
  tokens_out: number
  cached_tokens: number
  cost_usd: number
  llm_calls: number
  duration_ms: number
  per_worker_ms: Record<string, number>
}

/** `review_runs` 的一行。 */
export interface RunRow {
  task_id: string
  idempotency_key: string
  repo_id: string
  repo_node_id: string
  pr_number: number
  head_sha: string
  base_sha: string
  status: RunStatus
  attempt: number
  files_total: number
  files_reviewed: number
  diff_truncated: boolean
  planned_workers: WorkerType[]
  missing_workers: WorkerType[]
  deadline_at: string
  dispatched_at: string | null
  published_at: string | null
  /** 评论 id 非空 = 已经发到 PR 上了（也是防重复的第 1 道闸）。 */
  github_comment_id: number | null
  block_merge: boolean | null
  degraded: boolean
  totals: RunTotals | null
  created_at: string
}

/** Worker 报上来的一条原始发现。 */
export interface Finding {
  file: string
  line: number
  end_line: number | null
  severity: Severity
  category: string
  message: string
  evidence: string | null
  confidence: number
  suggestion: string | null
  rule_id: string | null
  source_line_verified: boolean
  fingerprint: string | null
}

/**
 * 聚合之后的发现。
 *
 * `confidence` 与 `adjusted_confidence` 的区别是这个项目最值得讲的一件事：
 * 前者是 LLM 自报的（系统性偏高、跨 Worker 不可比），后者是主 Agent 用
 * 确定性公式重算的 —— 界面**必须显示后者**，显示前者等于把那个问题又演示了一遍。
 */
export interface AggregatedFinding extends Finding {
  adjusted_confidence: number
  /** 撞上同一条问题的所有 Worker。跨 Worker 印证是去重最值钱的产物。 */
  sources: WorkerType[]
  corroboration_count: number
  cluster_id: number | null
  needs_human_review: boolean
  stage: 'raw' | 'clustered' | 'suppressed'
  conflict: ConflictRecord | null
}

export interface ConflictRecord {
  file: string
  line: number
  winner_worker: WorkerType
  loser_worker: WorkerType
  winner_severity: Severity
  loser_severity: Severity
  resolution_rule: string
  rationale: string
}

/** 主 Agent 的最终产物。`comment_body` 就是要发到 PR 上的那段 Markdown。 */
export interface ReviewReport {
  task_id: string
  repo_id: string
  repo_node_id: string
  pr_number: number
  head_sha: string
  base_sha: string
  findings: AggregatedFinding[]
  /** 置信度低于阈值、**入库但不发布**的发现。UI 单独一栏，别和 findings 混在一起。 */
  suppressed: AggregatedFinding[]
  conflicts: ConflictRecord[]
  block_merge: boolean
  decision_reason: string
  degraded: boolean
  missing_workers: WorkerType[]
  /** 这次的发现全来自确定性扫描器（今日配额用完 / 线上关了真实模型）。
   *  **和 `degraded` 是两件事**，见后端 `contracts.py` 里那个字段的说明。 */
  scanned_only: boolean
  files_total: number
  files_reviewed: number
  diff_truncated: boolean
  totals: RunTotals
  comment_body: string
  created_at: string
}

/** SSE 事件，同时也是 `run_events` 表的一行。`seq` 就是断线补齐的游标。 */
export interface RunEvent {
  seq: number
  task_id: string
  kind: EventKind
  payload: Record<string, unknown>
  created_at: string
}

export interface RunListResponse {
  runs: RunRow[]
  count: number
}

export interface RunDetailResponse {
  run: RunRow
  /** 还没跑到 `finalize` 时是 `null`（run 在跑、或者失败了）。 */
  report: ReviewReport | null
  events: RunEvent[]
}

// --------------------------------------------------------------------------- //
// 接口
// --------------------------------------------------------------------------- //

export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await http.get<HealthResponse>('/health', {
    // 依赖不可达时后端返回 **503**，而 503 的响应体里带着我们最需要的诊断信息
    // （哪个依赖挂了、报的什么错）。
    //
    // axios 默认把非 2xx 当异常抛，于是「后端连得上、但 Redis 挂了」会表现为
    // 「无法连接后端」—— 恰好是最误导人的那种错误。这里显式接受 5xx，
    // 让调用方拿到真实结果，由它自己看 `ok` 字段决定怎么显示。
    validateStatus: (status) => status < 600,
  })
  return data
}

export async function listRuns(limit = 50, offset = 0): Promise<RunListResponse> {
  const { data } = await http.get<RunListResponse>('/runs', { params: { limit, offset } })
  return data
}

/**
 * 一个 run 的全部：状态 + 报告 + 全量时间线。
 *
 * **时间线一次拿全**是刻意的（不是「先拿一页再翻页」）：一次审查的事件是几十条
 * 量级，而首屏为了时间线再开一条 SSE 会带来一个没人需要的中间态 ——
 * 「报告已经显示了、时间线还在转圈」。之后接上 SSE 时带 `?after=<最大 seq>`
 * 即可，同一张表的同一条查询。
 */
export async function fetchRun(taskId: string): Promise<RunDetailResponse> {
  const { data } = await http.get<RunDetailResponse>(`/runs/${encodeURIComponent(taskId)}`)
  return data
}

/**
 * 时间线的事件流地址。
 *
 * 返回的是**地址而不是 EventSource 实例** —— 建连、断线补齐、`run.finished`
 * 之后主动关闭这三件事都在 `stores/runs.ts` 里，因为「什么时候该关」
 * 取决于组件之外的状态。这里只负责拼 URL，顺便保证 `after` 一定是个整数
 * （拼成 `after=undefined` 会被后端当成 422，而不是「从头开始」）。
 */
export function eventsUrl(taskId: string, after: number): string {
  return `${API_BASE}/runs/${encodeURIComponent(taskId)}/events?after=${Math.max(0, Math.floor(after))}`
}

/** `https://github.com/o/n/pull/3` —— 报告里的 `pr_url` 是权威来源，这里只做兜底拼接。 */
export function prUrl(run: Pick<RunRow, 'repo_id' | 'pr_number'>): string {
  return `https://github.com/${run.repo_id}/pull/${run.pr_number}`
}
