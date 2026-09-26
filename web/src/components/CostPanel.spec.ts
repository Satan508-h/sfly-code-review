import { describe, expect, it } from 'vitest'

import type { RunTotals } from '@/api/client'
import { mountWithUi } from '@/testing/mount'

import CostPanel from './CostPanel.vue'

function totals(over: Partial<RunTotals> = {}): RunTotals {
  return {
    tokens_in: 4733,
    tokens_out: 1399,
    cached_tokens: 0,
    cost_usd: 0,
    llm_calls: 0,
    duration_ms: 49,
    per_worker_ms: { security: 4, performance: 1, style: 2 },
    ...over,
  }
}

function render(over: Partial<RunTotals> = {}, extra: Record<string, unknown> = {}) {
  return mountWithUi(CostPanel, {
    props: {
      totals: totals(over),
      filesTotal: 4,
      filesReviewed: 4,
      diffTruncated: false,
      ...extra,
    },
  })
}

describe('CostPanel', () => {
  it('成本为 0 时说的是「没花钱」，而不是留一个光秃秃的 $0', () => {
    // Mock LLM 下 cost_usd 恒为 0。一个 $0 会被读成「这块没接上」。
    const wrapper = render()
    expect(wrapper.text()).toContain('$0')
    expect(wrapper.text()).toContain('没花钱，不是没算')
  })

  it('有真实调用时显示调用次数和金额', () => {
    const wrapper = render({
      cost_usd: 0.0342,
      llm_calls: 6,
      tokens_in: 12000,
      cached_tokens: 9000,
    })
    expect(wrapper.text()).toContain('$0.0342')
    expect(wrapper.text()).toContain('6 次 LLM 调用')
    expect(wrapper.text()).toContain('75%') // 缓存命中率 9000/12000
  })

  it('输入为 0 时缓存命中率是「—」，不是 0%', () => {
    // 0% 读作「一次都没命中」，而真相是「没有输入」—— 两件事不一样。
    const wrapper = render({ tokens_in: 0, cached_tokens: 0 })
    expect(wrapper.text()).toContain('—')
    expect(wrapper.text()).not.toContain('0%')
  })

  it('按 Worker 拆的耗时从慢到快排，让「谁慢」一眼看出来', () => {
    const wrapper = render()
    const labels = wrapper.findAll('.bar-label').map((el) => el.text())
    expect(labels).toEqual(['安全', '风格', '性能']) // 4ms / 2ms / 1ms
  })

  it('没有 Worker 耗时数据时不画空条', () => {
    const wrapper = render({ per_worker_ms: {} })
    expect(wrapper.findAll('.bar-row')).toHaveLength(0)
    expect(wrapper.text()).toContain('没有 Worker 上报耗时')
  })

  it('diff 被裁剪时说出来 —— 「只审了 40 个」会被读成「只改了 40 个」', () => {
    const wrapper = render({}, { filesTotal: 200, filesReviewed: 40, diffTruncated: true })
    expect(wrapper.text()).toContain('40 / 200')
    expect(wrapper.text()).toContain('只审了风险最高的前几个')
  })
})
