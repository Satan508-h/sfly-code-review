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
  }
  checks: Record<string, DependencyCheck>
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
