/**
 * 渲染测试。
 *
 * **断言的是「页面上出现了什么字」，不是 DOM 结构。** 后者改一次样式就要改一次
 * 断言，最后所有人都学会了「测试红了就改期望值」—— 那等于没有测试。
 *
 * 这几条盯的是**只有渲染才会暴露的错**：条件写反了、字段取错了、
 * v-if 忘了加。类型检查对这类错误一无所知（`f.evidence` 是 `string | null`，
 * 类型上没问题，但页面上可能就是空的）。
 */
import { describe, expect, it } from 'vitest'

import { aggregatedFinding } from '@/testing/factories'
import { mountWithUi } from '@/testing/mount'

import FindingItem from './FindingItem.vue'

describe('FindingItem', () => {
  it('把一条发现该有的东西都显示出来', () => {
    const wrapper = mountWithUi(FindingItem, {
      props: {
        finding: aggregatedFinding({
          severity: 'critical',
          message: '硬编码的密钥',
          evidence: 'SECRET = "sk-live-123"',
          suggestion: '改用环境变量',
          category: 'secrets',
          rule_id: 'sec-secret-001',
        }),
      },
    })

    const text = wrapper.text()
    expect(text).toContain('严重') // 严重度标签的中文，不是 slug
    expect(text).toContain('硬编码的密钥')
    expect(text).toContain('SECRET = "sk-live-123"') // 证据逐字展示
    expect(text).toContain('改用环境变量')
    expect(text).toContain('secrets')
    expect(text).toContain('sec-secret-001')
    expect(text).toContain('行 32')
  })

  it('置信度显示**重算值**，并把模型自报值一起标出来', () => {
    // 这个组件存在的理由：LLM 自报的置信度系统性偏高且跨 Worker 不可比，
    // 所以界面必须以 adjusted_confidence 为准。若哪天有人把显示的字段换回
    // `confidence`，这条会红。
    const wrapper = mountWithUi(FindingItem, {
      props: { finding: aggregatedFinding({ confidence: 0.9, adjusted_confidence: 0.6 }) },
    })

    expect(wrapper.text()).toContain('0.60')
    expect(wrapper.text()).toContain('自报 0.90')
  })

  it('行号没落在变更行上时给出标记', () => {
    const wrapper = mountWithUi(FindingItem, {
      props: { finding: aggregatedFinding({ source_line_verified: false }) },
    })
    expect(wrapper.text()).toContain('行号未校验')
  })

  it('被置信度闸拦下的那条会显示「已拦下」', () => {
    const wrapper = mountWithUi(FindingItem, {
      props: { finding: aggregatedFinding({ stage: 'suppressed', adjusted_confidence: 0.2 }) },
    })
    expect(wrapper.text()).toContain('已拦下')
  })

  it('没有证据和建议时不显示空块', () => {
    const wrapper = mountWithUi(FindingItem, {
      props: { finding: aggregatedFinding({ evidence: null, suggestion: null, rule_id: null }) },
    })
    expect(wrapper.find('.evidence').exists()).toBe(false)
    expect(wrapper.find('.suggestion').exists()).toBe(false)
  })

  it('需要人工复核时打标', () => {
    const wrapper = mountWithUi(FindingItem, {
      props: { finding: aggregatedFinding({ needs_human_review: true }) },
    })
    expect(wrapper.text()).toContain('需人工复核')
  })
})
