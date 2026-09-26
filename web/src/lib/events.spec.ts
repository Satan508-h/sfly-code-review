import { describe, expect, it } from 'vitest'

import type { RunEvent } from '@/api/client'
import { completeRunEvents, runEvent } from '@/testing/factories'

import { isKeyEvent, nodeStates, summarize } from './events'

const REAL_RUN = completeRunEvents()

/**
 * 按事件类型取一条，**不按数组下标**。
 *
 * 一开始这里写的是 `REAL_RUN[0]` / `REAL_RUN[5]`，而往 fixture 中间补了
 * 一条事件之后，那些下标全部错位 —— 测试红了，但红的原因是「fixture 长了」，
 * 和被测的东西毫无关系。按名字取就不会有这种问题。
 */
function of(kind: RunEvent['kind']): RunEvent {
  const found = REAL_RUN.find((e) => e.kind === kind)
  if (!found) throw new Error(`fixture 里没有 ${kind}`)
  return found
}

describe('summarize', () => {
  it('run.created 一句话说清在审什么', () => {
    expect(summarize(of('run.created'))).toBe(
      'Satan508-h/sfly-playground#1 · 4 个文件 · 作者 Satan508-h · 「重构用户接口并加上备份入口」',
    )
  })

  it('plan 的完成事件顺带说出规划结果', () => {
    expect(summarize(REAL_RUN.find((e) => e.payload['node'] === 'plan')!)).toBe(
      '3 个 Worker · 审查 4/4 个文件',
    )
  })

  it('worker.result 说清谁、什么状态、几条、多久', () => {
    expect(summarize(of('worker.result'))).toBe('performance · ok · 2 条发现 · 1 ms')
  })

  it('aggregate.done 把「0 处冲突」也说出来', () => {
    // 「0」和「这个字段不存在」是两件事：前者说明算过且没冲突，
    // 后者说明这一版根本没算。界面上必须能分开。
    const text = summarize(of('aggregate.done'))
    expect(text).toContain('14 条发现')
    expect(text).toContain('拦下 2 条')
    expect(text).toContain('0 处冲突')
  })

  it('publish.done 说清发了什么、发到哪', () => {
    const text = summarize(of('publish.done'))
    expect(text).toContain('review:comment+inline')
    expect(text).toContain('14 条行内评论')
    expect(text).toContain('评论 #5324355208')
  })

  it('payload 缺字段时不拼出半句话，也不抛异常', () => {
    // 这些 payload 来自 jsonb，形状不保证。缺字段时的正确行为是「少说一句」，
    // 而不是显示 undefined / NaN 这种看起来像坏了的东西。
    expect(summarize(runEvent({ kind: 'worker.result', payload: {} }))).toBe('? · ?')
    expect(summarize(runEvent({ kind: 'run.created', payload: {} }))).toBe('?#?')
    expect(summarize(runEvent({ kind: 'publish.done', payload: {} }))).toBe('')
  })

  it('posted=false 是「未发布」，但那不等于失败（dry-run 也长这样）', () => {
    const text = summarize(runEvent({ kind: 'publish.done', payload: { form: 'dry_run', posted: false } }))
    expect(text).toBe('dry_run · 未发布')
  })
})

describe('nodeStates', () => {
  it('跑完的 run：七个节点全绿', () => {
    const states = nodeStates(REAL_RUN, 'published')
    expect(states.map((s) => `${s.node}=${s.state}`)).toEqual([
      'ingest=done',
      'plan=done',
      'dispatch=done',
      'wait=done',
      'aggregate=done',
      'finalize=done',
      'publish=done',
    ])
  })

  it('正在跑的 run：第一个没完成的节点是「进行中」，后面的还是「未开始」', () => {
    // 只到 dispatch —— 三个 Worker 都派出去了，但一条结果都还没回来
    const partial = REAL_RUN.slice(0, 3)
    const states = nodeStates(partial, 'dispatched')
    expect(states.map((s) => `${s.node}=${s.state}`)).toEqual([
      'ingest=done',
      'plan=done',
      'dispatch=done',
      'wait=active',
      'aggregate=pending',
      'finalize=pending',
      'publish=pending',
    ])
  })

  it('没有可审的文件时 run 停在 plan —— 后面的节点是「未开始」，不是「失败」', () => {
    // 「没跑」和「失败了」在界面上必须长得不一样。这类 run（`skipped`）是正常的。
    const onlyPlan = REAL_RUN.filter(
      (e) => e.kind === 'run.created' || (e.kind === 'node.finished' && e.payload['node'] === 'plan'),
    )
    const states = nodeStates(onlyPlan, 'skipped')
    expect(states.slice(0, 2).every((s) => s.state === 'done')).toBe(true)
    expect(states.slice(2).every((s) => s.state === 'pending')).toBe(true)
  })

  it('发布失败时那一个节点是红的', () => {
    const events = [
      ...REAL_RUN.filter((e) => e.kind !== 'publish.done' && e.kind !== 'run.finished'),
      runEvent({ seq: 190, kind: 'publish.failed', payload: { reason: '401 Unauthorized', retryable: false } }),
    ]
    const states = nodeStates(events, 'publish_failed')
    expect(states.at(-1)).toEqual({ node: 'publish', state: 'failed' })
  })

  it('重放过的 run（同一种事件出现多次）照样算对', () => {
    // 实测一个 run 有 5 次 aggregate.done / publish.done（被重投过）。
    // 用「出现过吗」而不是「最后一次是什么」，正是为了这种输入。
    const replayed = [...REAL_RUN, ...REAL_RUN.map((e) => ({ ...e, seq: e.seq + 100 }))]
    expect(nodeStates(replayed, 'published').every((s) => s.state === 'done')).toBe(true)
  })

  it('一条事件都没有时全是未开始，不抛异常', () => {
    const states = nodeStates([], 'queued')
    expect(states).toHaveLength(7)
    expect(states[0]).toEqual({ node: 'ingest', state: 'active' })
  })
})

describe('isKeyEvent', () => {
  it('把逐条 Worker 事件滤掉，只留节点级的', () => {
    // 一个 48 条事件的 run 里有 15 条 worker.dispatched，全列出来会淹掉主线
    expect(isKeyEvent(runEvent({ kind: 'worker.dispatched' }))).toBe(false)
    expect(isKeyEvent(runEvent({ kind: 'worker.result' }))).toBe(false)
    expect(isKeyEvent(runEvent({ kind: 'aggregate.done' }))).toBe(true)
    expect(isKeyEvent(runEvent({ kind: 'publish.done' }))).toBe(true)
  })
})
