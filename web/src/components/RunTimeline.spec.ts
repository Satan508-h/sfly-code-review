import { describe, expect, it } from 'vitest'

import type { RunEvent } from '@/api/client'
import { completeRunEvents } from '@/testing/factories'
import { mountWithUi } from '@/testing/mount'

import RunTimeline from './RunTimeline.vue'

/** 一个完整跑完的 run 的事件序列。和 `lib/events.spec.ts` 用的是同一份。 */
const EVENTS: RunEvent[] = completeRunEvents()

function render(
  events: RunEvent[] = EVENTS,
  status: 'published' | 'dispatched' = 'published',
  stream = 'closed',
) {
  return mountWithUi(RunTimeline, {
    props: { events, status, stream: stream as 'closed' },
  })
}

describe('RunTimeline', () => {
  it('七个节点都显示出来，完成的是绿勾', () => {
    const wrapper = render()
    const text = wrapper.text()
    for (const label of [
      '解析载荷',
      '规划',
      '派发',
      '等待屏障',
      '主 Agent 聚合',
      '生成报告',
      '回写评论',
    ]) {
      expect(text).toContain(label)
    }
    expect(wrapper.findAll('.node.done')).toHaveLength(7)
  })

  it('正在跑的时候，当前那个节点是「进行中」而不是已完成', () => {
    // 「三个 Worker 都派出去了、一条结果都还没回来」那个瞬间。
    // **按语义切片而不是按事件种类过滤** —— 上一版是用 `kind !== 'aggregate.done'`
    // 这种排除法写的，结果 `finalize` 的 node.finished 留了下来，把节点数算成了 4。
    const firstResult = EVENTS.findIndex((e) => e.kind === 'worker.result')
    const wrapper = render(EVENTS.slice(0, firstResult), 'dispatched')

    expect(wrapper.findAll('.node.done')).toHaveLength(3) // 解析载荷 / 规划 / 派发
    expect(wrapper.find('.node.active').text()).toContain('等待屏障')
  })

  it('事件按时间正序展示，并带上人读的摘要', () => {
    const wrapper = render()
    const rows = wrapper.findAll('.row')
    const text = wrapper.text()

    // 正序：第一条是 run.created（seq 176），最后一条是 run.finished（seq 187）
    expect(rows[0]!.text()).toContain('Satan508-h/sfly-playground#1')
    expect(rows.at(-1)!.text()).toContain('published')
    // 摘要带着 payload 里真有的东西
    expect(text).toContain('14 条发现')
    expect(text).toContain('14 条行内评论')
    expect(text).toContain('#187')
  })

  it('「只看关键节点」把逐条 Worker 事件折叠掉', async () => {
    const wrapper = render()
    const all = wrapper.findAll('.row').length
    expect(all).toBe(EVENTS.length)

    await wrapper
      .findAll('button')
      .find((b) => b.text().includes('只看关键节点'))!
      .trigger('click')

    // worker.dispatched / worker.result / node.finished 里的逐条细节被滤掉
    expect(wrapper.findAll('.row').length).toBeLessThan(all)
    expect(wrapper.text()).not.toContain('worker.dispatched')
  })

  it('点一行展开它的原始 payload —— 摘要丢掉的字段这里能看全', async () => {
    const wrapper = render()
    expect(wrapper.find('.raw').exists()).toBe(false)

    await wrapper.findAll('.row')[0]!.trigger('click')

    expect(wrapper.find('.raw').exists()).toBe(true)
    expect(wrapper.find('.raw').text()).toContain('"pr_number": 1')
  })

  it('流的状态如实显示，包括「当前环境不支持」', () => {
    expect(render(EVENTS, 'published', 'live').text()).toContain('实时连接中')
    expect(render(EVENTS, 'published', 'unsupported').text()).toContain('不支持实时推送')
  })
})
