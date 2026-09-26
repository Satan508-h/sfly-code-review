import { describe, expect, it } from 'vitest'

import type { AggregatedFinding } from '@/api/client'
import { aggregatedFinding } from '@/testing/factories'

import { confidencePair, decisionText, groupByFile, severityCounts, sortFindings } from './findings'

describe('sortFindings', () => {
  it('严重度从重到轻，同严重度时按文件路径再按行号', () => {
    const list: AggregatedFinding[] = [
      aggregatedFinding({ file: 'b.py', line: 5, severity: 'low' }),
      aggregatedFinding({ file: 'z.py', line: 9, severity: 'critical' }),
      aggregatedFinding({ file: 'a.py', line: 40, severity: 'low' }),
      aggregatedFinding({ file: 'a.py', line: 7, severity: 'low' }),
      aggregatedFinding({ file: 'm.py', line: 1, severity: 'medium' }),
    ]

    expect(sortFindings(list).map((f) => `${f.file}:${f.line}`)).toEqual([
      'z.py:9',
      'm.py:1',
      'a.py:7',
      'a.py:40',
      'b.py:5',
    ])
  })

  it('不改动传入的数组', () => {
    // 直接 `list.sort()` 是原地排序，会悄悄改掉调用方的数组 ——
    // 而调用方往往是 computed 的源数据，于是「排序」变成了「改数据」。
    const list = [
      aggregatedFinding({ severity: 'low' }),
      aggregatedFinding({ severity: 'critical' }),
    ]
    sortFindings(list)
    expect(list[0]?.severity).toBe('low')
  })
})

describe('groupByFile', () => {
  it('组的顺序由组内最严重的那条决定，而不是按文件名字典序', () => {
    // 这条测试盯的是一个具体的错法：用 Map + Object.values 之后的组顺序
    // 取决于插入顺序或键的字典序，于是 `zebra.py`（critical）会排在
    // `alpha.py`（low）**后面** —— 最危险的文件被排到最后，而且没有任何报错。
    const list = [
      aggregatedFinding({ file: 'alpha.py', line: 1, severity: 'low' }),
      aggregatedFinding({ file: 'zebra.py', line: 2, severity: 'critical' }),
      aggregatedFinding({ file: 'alpha.py', line: 3, severity: 'medium' }),
    ]

    const groups = groupByFile(list)
    expect(groups.map((g) => g.file)).toEqual(['zebra.py', 'alpha.py'])
    expect(groups[1]?.findings.map((f) => f.line)).toEqual([3, 1])
  })

  it('worst 取的是组内最重的那条', () => {
    const list = [
      aggregatedFinding({ file: 'a.py', line: 1, severity: 'medium' }),
      aggregatedFinding({ file: 'a.py', line: 2, severity: 'critical' }),
    ]
    expect(groupByFile(list)[0]?.worst).toBe('critical')
  })
})

describe('severityCounts', () => {
  it('固定从重到轻，且包含计数为 0 的档位', () => {
    // 0 的档位必须保留：分布条上「严重 0」和「这一档不存在」在界面上是
    // 两件事，而少一档会让标签的位置随数据左右跳动。
    const counts = severityCounts([
      aggregatedFinding({ severity: 'critical' }),
      aggregatedFinding({ severity: 'critical' }),
      aggregatedFinding({ severity: 'info' }),
    ])
    expect(counts.map((c) => `${c.severity}=${c.count}`)).toEqual([
      'critical=2',
      'high=0',
      'medium=0',
      'low=0',
      'info=1',
    ])
  })
})

describe('confidencePair', () => {
  it('差值够大时把模型自报值一并给出来', () => {
    const p = confidencePair(aggregatedFinding({ confidence: 0.9, adjusted_confidence: 0.6 }))
    expect(p).toEqual({ adjusted: 0.6, raw: 0.9 })
  })

  it('差值小于 0.02 时不给原始值 —— 那是显示噪音', () => {
    const p = confidencePair(aggregatedFinding({ confidence: 0.61, adjusted_confidence: 0.6 }))
    expect(p.raw).toBeNull()
  })
})

describe('decisionText', () => {
  it('已知 slug 给出中文说明', () => {
    expect(decisionText('secrets_found')).toContain('凭据泄露')
  })

  it('未知 slug 回显自身，而不是「未知原因」', () => {
    // 和后端 `decision_text()` 的策略一致：回显能让人去 grep，
    // 「未知原因」只能让人来问作者。
    expect(decisionText('some_new_rule')).toBe('some_new_rule')
  })
})
