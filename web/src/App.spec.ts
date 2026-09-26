/**
 * 应用外壳：**访客点开公网链接时看到的第一屏**。
 *
 * 这一屏有两件事值得钉住，而它们都属于「错了不会报错」那一类：
 *
 * 1. **唤醒面板要出现。** 不出现的表现是白屏 —— 而 Render 免费档冷启动
 *    60 秒是必然会发生的场景，也就是简历上那个链接被点开的第一秒。
 * 2. **唤醒期间不能挂载路由。** 挂载了的话，每个视图都会在同一个冷启动
 *    窗口里发自己的请求、一起挂 90 秒、然后各报一次错 —— 访客看到三四个
 *    红色错误，而真相只有一个：容器还没醒。这条用「路由内容不存在」来断言。
 */

import { flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { HealthResponse } from '@/api/client'
import { mountWithUi } from '@/testing/mount'

import App from './App.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, fetchHealth: vi.fn() }
})

const { fetchHealth } = await import('@/api/client')
const fetchHealthMock = vi.mocked(fetchHealth)

function health(): HealthResponse {
  return {
    ok: true,
    service: 'sfly-api',
    version: '0.1.0',
    mode: 'lite',
    uptime_s: 1,
    config: {
      queue_backend: 'memory',
      lock_backend: 'memory',
      llm_provider: 'deepseek',
      wait_strategy: 'interrupt',
      conflict_resolver: 'rules',
      webhook_secret: 'configured',
    },
    checks: {},
  } as HealthResponse
}

/** 路由出口里的占位内容。断言它在不在，等于断言视图有没有被挂载。 */
const Routed = { template: '<div data-test="routed">路由内容</div>' }

async function render() {
  const router = createRouter({
    history: createMemoryHistory(),
    // 三个真实存在的路径都要登记：顶栏那两个 RouterLink 会去解析它们，
    // 缺一个就是一行 `No match found for location with path "/runs"` 的警告。
    // 那行警告本身无害，但**无害的警告会盖住有事的警告** —— 而这一屏
    // 恰恰是最需要警告干净的地方。
    routes: [
      { path: '/', component: Routed },
      { path: '/runs', component: Routed },
      { path: '/system', component: Routed },
    ],
  })
  await router.push('/')
  await router.isReady()

  const wrapper = mountWithUi(App, { global: { plugins: [router] } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  fetchHealthMock.mockReset()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('冷启动第一屏', () => {
  it('后端在的时候直接出内容，不出现唤醒面板', async () => {
    fetchHealthMock.mockResolvedValue(health())

    const wrapper = await render()

    expect(wrapper.find('.waking').exists()).toBe(false)
    expect(wrapper.find('[data-test="routed"]').exists()).toBe(true)
  })

  it('探测挂住超过宽限期就显示唤醒面板，并暂时不挂载路由', async () => {
    fetchHealthMock.mockReturnValue(new Promise<HealthResponse>(() => {})) // 永远不返回
    const wrapper = await render()

    expect(wrapper.find('.waking').exists()).toBe(false) // 还没到 2 秒

    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()

    expect(wrapper.find('.waking').exists()).toBe(true)
    expect(wrapper.text()).toContain('正在唤醒服务')
    // **这条是本节的重点**：唤醒期间视图不能挂载。
    expect(wrapper.find('[data-test="routed"]').exists()).toBe(false)
  })

  it('等待期间把「等了多久、探了几次」显示出来', async () => {
    fetchHealthMock.mockRejectedValue(new Error('Network Error'))
    const wrapper = await render()

    await vi.advanceTimersByTimeAsync(4_000)
    await flushPromises()

    const stats = wrapper.find('.waking-stats')
    expect(stats.exists()).toBe(true)
    expect(stats.text()).toContain('已等待 4 秒')
    expect(stats.text()).toContain('第 2 次探测')
  })

  it('后端醒过来之后面板消失，视图挂上', async () => {
    fetchHealthMock.mockRejectedValueOnce(new Error('Network Error'))
    fetchHealthMock.mockResolvedValueOnce(health())
    const wrapper = await render()

    expect(wrapper.find('.waking').exists()).toBe(true)

    await vi.advanceTimersByTimeAsync(3_000)
    await flushPromises()

    expect(wrapper.find('.waking').exists()).toBe(false)
    expect(wrapper.find('[data-test="routed"]').exists()).toBe(true)
  })

  it('超过等待上限之后改说「未连通」，而不是一直转圈', async () => {
    fetchHealthMock.mockRejectedValue(new Error('Network Error'))
    const wrapper = await render()

    await vi.advanceTimersByTimeAsync(125_000)
    await flushPromises()

    expect(wrapper.find('.waking').exists()).toBe(false)
    expect(wrapper.find('.backend-down').exists()).toBe(true)
    expect(wrapper.find('.backend-down').text()).toContain('Network Error')
    // 放弃等待之后视图要挂上 —— 系统状态页正是排查时要看的那一页
    expect(wrapper.find('[data-test="routed"]').exists()).toBe(true)
  })
})
