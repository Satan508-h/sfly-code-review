/**
 * 冷启动等待循环。
 *
 * 这段逻辑的特点是：**写错了页面看起来「正在加载」**，而它其实永远不会
 * 加载完 —— 没有报错、没有红点，只有一个转圈的图标。所以每一条都对应一个
 * 具体的「看起来正常但永远等下去」：
 *
 * * 热后端不能闪一下唤醒面板（探到之前先假定在唤醒，但只等 2 秒）
 * * 冷启动必须在 2 秒内就说话（HTTP 超时是 90 秒，等它失败太晚）
 * * 失败之后要接着探（不然容器醒了页面也不知道）
 * * 探到 503 算**唤醒结束**（后端在，只是依赖挂了 —— 那是另一件事）
 * * 超过上限要停下来（永远转圈比说「连不上」更糟）
 * * 停止之后不能还有定时器在跑（组件卸载了还在探测）
 */

import { createPinia, setActivePinia } from 'pinia'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HealthResponse } from '@/api/client'

import { useHealthStore } from './health'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, fetchHealth: vi.fn() }
})

const { fetchHealth } = await import('@/api/client')
const fetchHealthMock = vi.mocked(fetchHealth)

function health(over: Partial<HealthResponse> = {}): HealthResponse {
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
    ...over,
  } as HealthResponse
}

beforeEach(() => {
  setActivePinia(createPinia())
  fetchHealthMock.mockReset()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('冷启动等待', () => {
  it('热后端不会闪一下唤醒面板', async () => {
    fetchHealthMock.mockResolvedValue(health())
    const store = useHealthStore()

    await store.startWatch()

    expect(store.waiting).toBe(false)
    expect(store.state).toBe('ok')
    // 那 2 秒的宽限定时器必须被清掉 —— 不清的话它会在探测成功之后
    // 再把 waiting 点亮一次，用户看到的是「加载完了又跳回唤醒中」。
    await vi.advanceTimersByTimeAsync(5_000)
    expect(store.waiting).toBe(false)
    expect(fetchHealthMock).toHaveBeenCalledTimes(1)
  })

  it('探测挂住超过宽限期就立刻说话，不等那 90 秒的超时', async () => {
    let release!: (value: HealthResponse) => void
    fetchHealthMock.mockReturnValue(
      new Promise<HealthResponse>((resolve) => {
        release = resolve
      }),
    )
    const store = useHealthStore()

    const pending = store.startWatch()
    expect(store.waiting).toBe(false) // 还没到 2 秒，不该闪

    await vi.advanceTimersByTimeAsync(2_000)
    expect(store.waiting).toBe(true)

    release(health())
    await pending
    expect(store.waiting).toBe(false)
  })

  it('失败之后接着探，容器醒了页面就跟上', async () => {
    fetchHealthMock.mockRejectedValueOnce(new Error('Network Error'))
    fetchHealthMock.mockResolvedValueOnce(health())
    const store = useHealthStore()

    await store.startWatch()
    expect(store.waiting).toBe(true)
    expect(store.attempts).toBe(1)
    expect(store.error).toContain('Network Error')

    await vi.advanceTimersByTimeAsync(3_000)

    expect(store.attempts).toBe(2)
    expect(store.waiting).toBe(false)
    expect(store.error).toBeNull()
  })

  it('等待期间秒数会往上走', async () => {
    fetchHealthMock.mockRejectedValue(new Error('Network Error'))
    const store = useHealthStore()

    await store.startWatch()
    expect(store.waitedS).toBe(0)

    await vi.advanceTimersByTimeAsync(4_000)
    expect(store.waitedS).toBeGreaterThanOrEqual(4)
  })

  it('探到 503 算唤醒结束 —— 后端在，只是依赖挂了', async () => {
    fetchHealthMock.mockResolvedValue(health({ ok: false }))
    const store = useHealthStore()

    await store.startWatch()

    // 把 503 也当成「还在唤醒」的话，一个数据库挂了的后端会永远显示
    // 「正在唤醒」，而它其实一直在跑 —— 那是彻底误导人的一种显示。
    expect(store.waiting).toBe(false)
    expect(store.state).toBe('bad')
  })

  it('超过等待上限就不再转圈，把真正的原因露出来', async () => {
    fetchHealthMock.mockRejectedValue(new Error('Network Error'))
    const store = useHealthStore()

    await store.startWatch()
    expect(store.waiting).toBe(true)

    await vi.advanceTimersByTimeAsync(120_000)

    expect(store.waiting).toBe(false)
    expect(store.error).toContain('Network Error')
  })

  it('停止之后不再探测', async () => {
    fetchHealthMock.mockRejectedValue(new Error('Network Error'))
    const store = useHealthStore()

    await store.startWatch()
    store.stopWatch()
    const callsSoFar = fetchHealthMock.mock.calls.length

    await vi.advanceTimersByTimeAsync(60_000)

    expect(store.waiting).toBe(false)
    expect(fetchHealthMock).toHaveBeenCalledTimes(callsSoFar)
  })
})
