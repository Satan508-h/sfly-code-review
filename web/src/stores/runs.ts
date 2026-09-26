/**
 * run 列表的状态。
 *
 * ### 为什么列表页是轮询、详情页才是 SSE
 *
 * 后端的 SSE 接口是 `GET /api/runs/{task_id}/events` —— **一条流对应一个 run**。
 * 没有「所有 run 的事件流」这种东西，而列表页要的恰恰是后者（新 run 冒出来、
 * 老 run 的状态变了）。硬用 SSE 得开 N 条连接，还得自己处理「新 run 出现了，
 * 给它补一条流」。
 *
 * 轮询的成本在这里可以忽略：一次 `SELECT ... ORDER BY task_id DESC LIMIT 50`，
 * 走主键索引（ULID 前 48 位是时间戳，所以主键就是时间索引），毫秒级。
 * 而它换来的是「页面自己会动」这个演示效果 —— 跑一次 `python tasks.py demo`，
 * 几秒后列表顶上多一行。
 *
 * ### 轮询必须能停
 *
 * `setInterval` 不清掉的话，切走页面后它还在跑。开发时表现为「改一行代码，
 * 热更新一次，网络面板里的请求数翻一倍」—— 一个会自己长大的资源泄漏。
 * 所以 `stop()` 是必须调用的，由视图的 `onUnmounted` 负责。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { listRuns, type RunRow } from '@/api/client'
import { isTerminal } from '@/lib/format'

/** 列表自动刷新间隔。比 SSE 的 1 秒宽得多 —— 这里没有「实时」的承诺。 */
const POLL_MS = 5000

export const useRunsStore = defineStore('runs', () => {
  const runs = ref<RunRow[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const loadedAt = ref<number | null>(null)

  let timer: ReturnType<typeof setInterval> | null = null
  /**
   * 正在飞行中的那次请求。
   *
   * 挡住的是「上一次还没回来、下一次又发出去」：后端冷启动时一次请求要几秒，
   * 而轮询间隔是 5 秒 —— 没有这道闸的话会稳定地叠起来，越叠越多。
   * **不是防抖**（那会推迟刷新），是「上一次没回来就跳过这一拍」。
   */
  let inflight = false

  /** 正在跑的 run。演示时最想看的就是这几行。 */
  const activeCount = computed(() => runs.value.filter((r) => !isTerminal(r.status)).length)

  async function load(): Promise<void> {
    if (inflight) return
    inflight = true
    // 首屏才显示骨架屏；后续自动刷新时**不能**把已有内容换成骨架屏 ——
    // 那会让页面每 5 秒闪一次。
    if (loadedAt.value === null) loading.value = true
    try {
      const data = await listRuns(50, 0)
      runs.value = data.runs
      error.value = null
      loadedAt.value = Date.now()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
      inflight = false
    }
  }

  function start(): void {
    void load()
    if (timer !== null) return
    timer = setInterval(() => void load(), POLL_MS)
  }

  function stop(): void {
    if (timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }

  return { runs, loading, error, loadedAt, activeCount, load, start, stop }
})
