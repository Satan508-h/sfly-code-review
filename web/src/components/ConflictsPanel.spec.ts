/**
 * 冲突面板。
 *
 * 这里唯一值得测、也最容易测错的是**空的时候说什么**：一个空表格看起来像
 * 「算过了，没有冲突」，而实际上聚类还没实现（M9），空数组的含义是「还没算」。
 * 这两件事在界面上长得一模一样，含义正好相反。
 */
import { describe, expect, it } from 'vitest'

import { conflictRecord } from '@/testing/factories'
import { mountWithUi } from '@/testing/mount'

import ConflictsPanel from './ConflictsPanel.vue'

describe('ConflictsPanel', () => {
  it('空的时候必须解释「还没算」，而不是让人以为「算过了、没有」', () => {
    const wrapper = mountWithUi(ConflictsPanel, { props: { conflicts: [] } })

    const text = wrapper.text()
    expect(text).toContain('这次没有冲突记录')
    expect(text).toContain('这句话现在还不能当成结论')
    expect(text).toContain('M9')
    // 顺带说清「跨 Worker 印证」也是同一件事导致的
    expect(text).toContain('跨 Worker 印证')
  })

  it('有冲突时把胜出方、让位方和规则都摆出来', () => {
    const wrapper = mountWithUi(ConflictsPanel, {
      props: { conflicts: [conflictRecord()] },
    })

    const text = wrapper.text()
    expect(text).toContain('app/api.py:32')
    expect(text).toContain('安全') // winner_worker 的中文
    expect(text).toContain('风格') // loser_worker 的中文
    expect(text).toContain('严重')
    expect(text).toContain('低')
    expect(text).toContain('职责域优先') // 规则的**中文说明**，不是 slug
  })

  it('未决的冲突要显式标出来 —— 那一条是要人去看的', () => {
    const wrapper = mountWithUi(ConflictsPanel, {
      props: { conflicts: [conflictRecord({ resolution_rule: 'unresolved' })] },
    })
    expect(wrapper.text()).toContain('需人工复核')
  })

  it('遇到没登记过的规则时回显 slug，而不是显示空白', () => {
    const wrapper = mountWithUi(ConflictsPanel, {
      props: { conflicts: [conflictRecord({ resolution_rule: 'some_new_rule' })] },
    })
    expect(wrapper.text()).toContain('some_new_rule')
  })
})
