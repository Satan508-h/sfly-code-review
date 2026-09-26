/**
 * 事件流客户端的测试。
 *
 * 用一个假的 `EventSource` 而不是真连一条流：要测的是**我们这一侧的三条纪律**
 * （收到 run.finished 主动收流、按 seq 去重、坏数据不能让整条流停掉），
 * 而这三条恰恰是「看起来能用、实际会漏事件或空转」的那类代码 ——
 * 真连一条 SSE 反而更难把它们逼出来（比如「无限重连」要等 5 秒宽限期才知道）。
 */
import { describe, expect, it, vi } from 'vitest'

import type { RunEvent } from '@/api/client'
import { runEvent } from '@/testing/factories'

import { openRunStream, type EventSourceLike } from './sse'

/** 手写的假 EventSource：记下 URL，并把几个回调暴露出来让测试自己触发。 */
class FakeEventSource implements EventSourceLike {
  static instances: FakeEventSource[] = []

  onopen: ((ev: Event) => void) | null = null
  onerror: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent<string>) => void) | null = null

  closed = false
  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }

  close(): void {
    this.closed = true
  }

  // ── 测试用的触发器 ──
  emit(event: RunEvent): void {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent<string>)
  }
  emitRaw(data: string): void {
    this.onmessage?.({ data } as MessageEvent<string>)
  }

  static last(): FakeEventSource {
    const es = FakeEventSource.instances.at(-1)
    if (!es) throw new Error('没有创建过 EventSource')
    return es
  }
  static reset(): void {
    FakeEventSource.instances = []
  }
}

function spyHandlers() {
  return {
    onEvent: vi.fn(),
    onOpen: vi.fn(),
    onError: vi.fn(),
    onDone: vi.fn(),
  }
}

function open(cursor = 0) {
  FakeEventSource.reset()
  const handlers = spyHandlers()
  const stream = openRunStream('01M3DVHP409TBPY721JN9VTA0P', cursor, handlers, {
    EventSourceImpl: FakeEventSource,
  })
  return { stream, handlers, es: FakeEventSource.last() }
}

describe('openRunStream', () => {
  it('把游标带进 URL —— 首屏已经拿过的那批不该再来一遍', () => {
    const { es } = open(176)
    expect(es.url).toContain('after=176')
  })

  it('onopen / onmessage 转成回调', () => {
    const { handlers, es } = open()
    es.onopen?.({} as Event)
    expect(handlers.onOpen).toHaveBeenCalledTimes(1)

    es.emit(runEvent({ seq: 5 }))
    expect(handlers.onEvent).toHaveBeenCalledTimes(1)
  })

  it('重复和过期的 seq 被丢掉（服务端只保证一个不少，不保证一个不多）', () => {
    const { handlers, es } = open(10)

    es.emit(runEvent({ seq: 9 })) // 比水位旧
    es.emit(runEvent({ seq: 10 })) // 等于水位
    expect(handlers.onEvent).not.toHaveBeenCalled()

    es.emit(runEvent({ seq: 11 }))
    es.emit(runEvent({ seq: 11 })) // 同一条又来一次（重连时的重叠）
    es.emit(runEvent({ seq: 12 }))
    expect(handlers.onEvent).toHaveBeenCalledTimes(2)
  })

  it('收到 run.finished 主动收流，并说明原因', () => {
    // 不收的话 EventSource 会在服务端关流后**自动重连**，变成永远不停的
    // 「连上 → 没新事件 → 收流 → 再连上」。SSE 协议里没有「别连了」这个信号。
    const { handlers, es, stream } = open()

    es.emit(runEvent({ seq: 7, kind: 'run.finished', payload: { status: 'published' } }))

    expect(handlers.onDone).toHaveBeenCalledWith('finished')
    expect(es.closed).toBe(true)
    expect(stream.live).toBe(false)
  })

  it('收流之后的事件与 error 都不再上报', () => {
    const { handlers, es } = open()
    es.emit(runEvent({ seq: 7, kind: 'run.finished', payload: {} }))

    es.emit(runEvent({ seq: 8 }))
    es.onerror?.({} as Event)

    expect(handlers.onEvent).toHaveBeenCalledTimes(1) // 只有那条 finished
    expect(handlers.onError).not.toHaveBeenCalled()
  })

  it('坏掉的一行只跳过它自己，后面的照常', () => {
    const { handlers, es } = open()
    es.emitRaw('{这不是 json')
    es.emitRaw('null')
    es.emit(runEvent({ seq: 3, kind: 'aggregate.done', payload: {} }))

    expect(handlers.onError).not.toHaveBeenCalled()
    expect(handlers.onEvent).toHaveBeenCalledTimes(1)
  })

  it('缺 seq 的事件被丢掉 —— 没有水位就没法去重，宁可不要', () => {
    const { handlers, es } = open()
    es.emitRaw(JSON.stringify({ kind: 'run.created', payload: {} }))
    expect(handlers.onEvent).not.toHaveBeenCalled()
  })

  it('调用方 close() 之后报 closed，且再 close 一次不会重复上报', () => {
    const { stream, handlers, es } = open()
    stream.close()
    stream.close()

    expect(handlers.onDone).toHaveBeenCalledTimes(1)
    expect(handlers.onDone).toHaveBeenCalledWith('closed')
    expect(es.closed).toBe(true)
  })

  it('环境里没有 EventSource 时给出 unsupported，而不是抛异常', () => {
    // 老浏览器 / 非浏览器环境。页面上的历史事件来自详情接口，所以这只会
    // 丢掉「边跑边看」，不会显示错的内容。
    const handlers = spyHandlers()
    const stream = openRunStream('x', 0, handlers, { EventSourceImpl: undefined })
    expect(stream.live).toBe(false)
    expect(handlers.onError).toHaveBeenCalled()
  })
})
