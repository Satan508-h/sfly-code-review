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

export interface WorkerCheck {
  ok: boolean
  detail?: string
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
  checks: Record<string, WorkerCheck>
}

// --------------------------------------------------------------------------- //
// 接口
// --------------------------------------------------------------------------- //

export async function fetchHealth(): Promise<HealthResponse> {
  const { data } = await http.get<HealthResponse>('/health')
  return data
}
