/**
 * run 事件流（SSE）的客户端。
 *
 * 协议那一侧的约定写在 `apps/api/sfly_api/routes/events.py` 的文档字符串里，
 * 这里是它的另一半 —— **客户端必须自己做的三件事**，每一件漏了都会出一个
 * 看起来像后端 bug 的症状：
 *
 * 1. **收到 `run.finished` 要主动 `close()`。**
 *    EventSource 在服务端关流之后会**自动重连**（这是规范行为，不是 bug），
 *    于是变成「连上 → 没有新事件 → 服务端收流 → 再连上」的循环，永远不会停。
 *    SSE 协议里没有「别连了」这个信号，所以这件事只能由客户端做。
 *
 * 2. **按 `seq` 去重。**
 *    首屏从 `GET /api/runs/{id}` 已经拿过一批事件，之后开流时 `?after=` 与
 *    浏览器重连带的 `Last-Event-ID` 之间可能有重叠。服务端只保证**一个都不会少**，
 *    不保证一个都不多 —— 去重是客户端的责任。
 *
 * 3. **重连不自己写。**
 *    浏览器重连时会带 `Last-Event-ID: <它收到的最后一个 id>`，服务端从
 *    `run_events` 表里补齐缺口。自己写一套重连逻辑只会和服务端的补齐机制打架
 *    （两套游标，谁对谁错说不清）。我们只在 `onerror` 里把状态显示出来。
 *
 * `EventSource` 由参数注入而不是直接用全局的那个：jsdom **没有实现**
 * `EventSource`，不注入的话这一整个文件的逻辑都没法测 —— 而上面那三件事
 * 恰恰是最需要测的（它们是「看起来能用但会漏事件」的那类代码）。
 */

import { eventsUrl, type RunEvent } from './client'

export interface RunStreamHandlers {
  /** 一条**新的**事件（已按 seq 去重、已排除首屏已有的那批）。 */
  onEvent: (event: RunEvent) => void
  /** 连接已建立（含重连成功）。 */
  onOpen?: () => void
  /** 连接出错。浏览器会自动重连，所以这不是终态。 */
  onError?: () => void
  /** 收流。`finished` = 看到 run.finished 主动关的；`closed` = 调用方关的。 */
  onDone?: (reason: 'finished' | 'closed') => void
}

export interface RunStream {
  close(): void
  /** 还开着吗。用来避免在收流之后再更新「连接中」这类状态。 */
  readonly live: boolean
}

/** 可注入的最小 EventSource 形状 —— 只要求我们真正用到的那几个成员。 */
export interface EventSourceLike {
  onopen: ((ev: Event) => void) | null
  onerror: ((ev: Event) => void) | null
  onmessage: ((ev: MessageEvent<string>) => void) | null
  close(): void
}

export type EventSourceCtor = new (url: string) => EventSourceLike

export interface RunStreamDeps {
  /** 默认用浏览器那个。测试传一个假的进来。 */
  EventSourceImpl?: EventSourceCtor | undefined
}

export function openRunStream(
  taskId: string,
  cursor: number,
  handlers: RunStreamHandlers,
  deps: RunStreamDeps = {},
): RunStream {
  const Ctor = deps.EventSourceImpl ?? (globalThis as { EventSource?: EventSourceCtor }).EventSource

  let live = true
  /**
   * 已见过的最大 seq。
   *
   * 用「只前进的水位」而不是一个 Set：这条流按 seq 递增，水位是 O(1) 的，
   * 而 Set 会随着长 run 无限增长。**代价**是它假定事件不会乱序到达 ——
   * 这个假定成立，因为服务端查的是 `WHERE seq > $1 ORDER BY seq`。
   */
  let watermark = cursor
  let source: EventSourceLike | null = null

  if (!Ctor) {
    // 环境里根本没有 EventSource（jsdom、老浏览器）。明确说一声然后退化成
    // 「没有实时更新」—— 页面上的历史事件仍然是从详情接口拿到的全量，
    // 所以这只会丢掉「边跑边看」这一件事，不会显示错误的内容。
    handlers.onError?.()
    return {
      close: () => {},
      get live() {
        return false
      },
    }
  }

  const finish = (reason: 'finished' | 'closed'): void => {
    if (!live) return
    live = false
    source?.close()
    source = null
    handlers.onDone?.(reason)
  }

  source = new Ctor(eventsUrl(taskId, cursor))
  source.onopen = () => {
    if (live) handlers.onOpen?.()
  }
  source.onerror = () => {
    // 收流之后再来的 error 是我们自己 close 引起的（浏览器会为此报一次），
    // 把它当成故障显示出来会让人以为出了问题。
    if (live) handlers.onError?.()
  }
  source.onmessage = (ev: MessageEvent<string>) => {
    if (!live) return

    // **解析和形状检查要分开做。** 一开始只把 `JSON.parse` 包在 try 里，
    // 于是 `null` / `5` / `"x"` 这些**合法 JSON 但不是对象**的载荷会在
    // 下一行 `event.seq` 上抛 TypeError —— 一个穿出 onmessage 的异常。
    // 症状是流看起来还活着（浏览器只是把错误记到控制台），但那条消息之后
    // 的处理全断了。测试里那条「坏掉的一行只跳过它自己」就是被这个打红的。
    let parsed: unknown
    try {
      parsed = JSON.parse(ev.data)
    } catch {
      return
    }
    if (typeof parsed !== 'object' || parsed === null) return

    const event = parsed as Partial<RunEvent>
    if (typeof event.seq !== 'number' || typeof event.kind !== 'string') return
    if (event.seq <= watermark) return

    watermark = event.seq
    handlers.onEvent(event as RunEvent)
    if (event.kind === 'run.finished') finish('finished')
  }

  return {
    close: () => finish('closed'),
    get live() {
      return live
    },
  }
}
