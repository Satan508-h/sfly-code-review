/**
 * 后端健康状态。
 *
 * 顶栏那个小徽章和「系统状态」页读的是同一份数据 —— 所以它必须是个 store，
 * 否则两边各拉一次，会出现「顶栏说正常、系统页说 Redis 挂了」这种自己和自己
 * 吵架的界面。
 *
 * **失败不是异常状态。** 后端连不上时这个 store 的 `error` 有值、`health`
 * 是 null，而页面照常渲染（顶栏显示一个红点）。把整个应用做成「后端没起来
 * 就白屏」，在实际演示里是最亏的一种失败方式 —— 那正是你最需要页面能说话
 * 的时候。免费档会休眠，这个场景一定会出现。
 *
 * ### 冷启动：为什么这里有一条等待循环
 *
 * 免费的演示实例在长时间没人访问后休眠，下一个访客要等它重新拉起容器 ——
 * **Hugging Face Spaces 免费档是 2–5 分钟**。而「等失败了再提示」的做法等于
 * 让访客对着白屏站几分钟，而他完全不知道发生了什么。
 *
 * 所以这里有两条时间线：
 *
 * * `SLOW_AFTER_MS`（2 秒）—— 探测还没回来就先假定它在唤醒，页面立刻说话。
 *   给 2 秒而不是 0，是因为热后端 50 毫秒就答了，一上来就闪一个「正在唤醒」
 *   比不提示更糟。
 * * `POLL_MS`（3 秒）—— 失败之后多久再探一次，直到它答应。
 *
 * `GIVE_UP_MS` 之后**不再叫「唤醒中」**：一个永远转圈的页面比一个说「连不上」
 * 的页面更糟 —— 前者不告诉你任何事，还让你一直等下去。
 *
 * ### 上界必须比平台的冷启动长
 *
 * 这几个数**跟着部署平台走**。`GIVE_UP_MS` 定小了，症状恰好发生在它唯一有用的
 * 那个场景里：真撞上冷启动时，面板在容器醒过来之前变成「后端未连通」。
 * 换平台时要一起改 —— 这类数不会报错，只会让提示「有时候没用」。
 *
 * ### 「后端在」和「后端好」是两件事
 *
 * 探到 503（依赖挂了）也算**唤醒结束**：容器醒了，能应答了，剩下的问题归
 * 顶栏那个红点和系统状态页。把 503 也当成「还在唤醒」会让一个数据库挂了的
 * 后端永远显示「正在唤醒」，而它其实一直在跑。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { API_BASE, fetchHealth, type HealthResponse } from '@/api/client'

/** 探测还没回来就先假定在唤醒。见模块文档：热后端 50ms 就答了，不能一上来就闪。 */
const SLOW_AFTER_MS = 2_000

/** 失败之后多久再探一次。唤醒本来就要几分钟，探得更密没有意义。 */
const POLL_MS = 3_000

/** 等待上限。超过它就不再叫「唤醒中」，把真正的原因交给页面显示。
 *
 * **这个数跟着部署平台走。** 免费档的冷启动有多长，上限就该比它长多少：
 * Hugging Face Spaces 免费档唤醒要 **2–5 分钟**（它要重新拉起容器），
 * 所以取 7 分钟。定小了的后果正好发生在这个上限唯一有用的那个场景里 ——
 * 真撞上冷启动时，面板会在容器醒过来之前变成「后端未连通」，
 * 而访客已经等了两分钟。
 *
 * 从前这里是 120 秒，因为当时的平台（Render）冷启动只有 30–60 秒。
 * 换平台时这个数**必须一起改**，否则它不会报错，只会在线上表现成
 * 「唤醒提示有时候没用」。
 */
const GIVE_UP_MS = 420_000

type Timer = ReturnType<typeof setTimeout>

export const useHealthStore = defineStore('health', () => {
  const health = ref<HealthResponse | null>(null)
  const error = ref<string | null>(null)
  const loading = ref(false)
  const checkedAt = ref<number | null>(null)

  /** 后端还没答应，而我们已经等了一会儿（或失败过了）。页面据此显示唤醒提示。 */
  const waiting = ref(false)
  /** 已经等了多久（秒）。显示出来是为了让「它在动」这件事看得见。 */
  const waitedS = ref(0)
  /** 探了几次。同上 —— 一个数字在涨，比一个转圈的图标更能说明没卡死。 */
  const attempts = ref(0)

  /** 三态：未知 / 可用 / 依赖异常。**「未知」和「异常」必须分开** —— 首次加载
   *  还没回来时显示红点会吓人，而它其实什么也没说明。 */
  const state = computed<'unknown' | 'ok' | 'bad'>(() => {
    if (health.value === null) return error.value ? 'bad' : 'unknown'
    return health.value.ok ? 'ok' : 'bad'
  })

  // 计时器进不了响应式：它们只是实现细节，而把 timer 放进 ref 会让「等了多久」
  // 这件事和 Vue 的渲染缠在一起。`running` 是循环的开关 —— 没有它，一个已经
  // 被 stopWatch 清掉的定时器回调仍会把 waiting 重新点亮。
  let startedAt = 0
  let running = false
  let slowTimer: Timer | null = null
  let pollTimer: Timer | null = null
  let tickTimer: Timer | null = null

  function clearTimers(): void {
    for (const timer of [slowTimer, pollTimer, tickTimer]) {
      if (timer !== null) clearTimeout(timer)
    }
    slowTimer = null
    pollTimer = null
    tickTimer = null
  }

  /** 单次探测。它只负责「问一次并记下结果」，重试策略在外面。 */
  async function load(): Promise<void> {
    loading.value = true
    try {
      health.value = await fetchHealth()
      error.value = null
    } catch (e) {
      // 冷启动场景下这个失败是正常的。把 API_BASE 打进错误信息里，
      // 否则看到 "Network Error" 根本不知道它在连哪儿。
      error.value = `无法连接后端（${API_BASE}）：${e instanceof Error ? e.message : String(e)}`
      health.value = null
    } finally {
      loading.value = false
      checkedAt.value = Date.now()
    }
  }

  /** 停止等待。组件卸载时必须调 —— 否则循环会在后台一直探测。 */
  function stopWatch(): void {
    running = false
    clearTimers()
    waiting.value = false
  }

  /** 开始守着后端，直到它答应或者超过 `GIVE_UP_MS`。 */
  async function startWatch(): Promise<void> {
    clearTimers()
    running = true
    startedAt = Date.now()
    waiting.value = false
    waitedS.value = 0
    attempts.value = 0
    slowTimer = setTimeout(() => {
      if (running) enterWaiting()
    }, SLOW_AFTER_MS)
    await probe()
  }

  /** 用户点「重试」：重新开始守护。 */
  async function retryNow(): Promise<void> {
    await startWatch()
  }

  async function probe(): Promise<void> {
    attempts.value += 1
    await load()
    if (!running) return
    if (error.value === null) {
      // 后端答应了 —— 哪怕是 503（依赖挂了）也说明**它在**。见模块文档。
      clearTimers()
      waiting.value = false
      return
    }
    enterWaiting()
  }

  function enterWaiting(): void {
    if (!running) return
    if (slowTimer !== null) {
      clearTimeout(slowTimer)
      slowTimer = null
    }
    waiting.value = true
    waitedS.value = elapsedSeconds()

    if (tickTimer === null) {
      const tick = (): void => {
        tickTimer = setTimeout(tick, 1000)
        waitedS.value = elapsedSeconds()
      }
      tickTimer = setTimeout(tick, 1000)
    }

    if (Date.now() - startedAt >= GIVE_UP_MS) {
      // 超时。**不再叫「唤醒中」**：error 留着，页面显示真正的原因。
      running = false
      clearTimers()
      waiting.value = false
      return
    }

    if (pollTimer === null) {
      pollTimer = setTimeout(() => {
        pollTimer = null
        void probe()
      }, POLL_MS)
    }
  }

  function elapsedSeconds(): number {
    return Math.floor((Date.now() - startedAt) / 1000)
  }

  return {
    health,
    error,
    loading,
    checkedAt,
    waiting,
    waitedS,
    attempts,
    state,
    load,
    startWatch,
    stopWatch,
    retryNow,
  }
})
