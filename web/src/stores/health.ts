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
 * 的时候。Render 免费版冷启动 60 秒，这个场景一定会出现。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { API_BASE, fetchHealth, type HealthResponse } from '@/api/client'

export const useHealthStore = defineStore('health', () => {
  const health = ref<HealthResponse | null>(null)
  const error = ref<string | null>(null)
  const loading = ref(false)
  const checkedAt = ref<number | null>(null)

  /** 三态：未知 / 可用 / 依赖异常。**「未知」和「异常」必须分开** —— 首次加载
   *  还没回来时显示红点会吓人，而它其实什么也没说明。 */
  const state = computed<'unknown' | 'ok' | 'bad'>(() => {
    if (health.value === null) return error.value ? 'bad' : 'unknown'
    return health.value.ok ? 'ok' : 'bad'
  })

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

  return { health, error, loading, checkedAt, state, load }
})
