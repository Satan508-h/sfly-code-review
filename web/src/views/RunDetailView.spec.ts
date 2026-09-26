/**
 * 详情页的渲染测试。
 *
 * 这一页的分支比其余页面加起来都多，而**每一种分支都对应一个真实状态**：
 * 报告还没生成、审查失败、一条问题都没有、有问题、有被拦下的。写错任何一种的
 * 代价都是「页面在展示一个不存在的结论」—— 比如把「还没跑完」显示成
 * 「没有问题」，那是最糟的一种错。
 *
 * 用 `vi.mock` 换掉取数层而不是架一个假的 HTTP 服务：被测的是**拿到数据之后
 * 怎么显示**，网络那一段已经有后端自己的测试和 `scripts/replay_webhook.py`
 * 在管。测试的边界要划在能被一次读懂的范围内。
 */
import { flushPromises } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { RunDetailResponse, RunEvent } from '@/api/client'
import {
  aggregatedFinding,
  completeRunEvents,
  reviewReport,
  runEvent,
  runRow,
} from '@/testing/factories'
import { mountWithUi } from '@/testing/mount'

import RunDetailView from './RunDetailView.vue'

vi.mock('@/api/client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/client')>()
  return { ...actual, fetchRun: vi.fn() }
})

const { fetchRun } = await import('@/api/client')
const fetchRunMock = vi.mocked(fetchRun)

/** 挂载并等首屏那次取数落地。 */
async function render(response: RunDetailResponse, taskId = '01M3DVHP409TBPY721JN9VTA0P') {
  fetchRunMock.mockResolvedValue(response)
  const wrapper = mountWithUi(RunDetailView, {
    props: { taskId },
    global: { mocks: { $router: { push: vi.fn() } } },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  fetchRunMock.mockReset()
  FakeEventSource.reset()
  ;(globalThis as { EventSource?: unknown }).EventSource = FakeEventSource
})

afterEach(() => {
  delete (globalThis as { EventSource?: unknown }).EventSource
})

/**
 * 把浏览器那个全局的 `EventSource` 换成假的。
 *
 * `openRunStream` 是**在调用那一刻**去读 `globalThis.EventSource` 的，
 * 所以这里装上就能接住——不用给组件开洞传参。
 */
class FakeEventSource {
  static instances: FakeEventSource[] = []
  static reset(): void {
    FakeEventSource.instances = []
  }
  static last(): FakeEventSource {
    const es = FakeEventSource.instances.at(-1)
    if (!es) throw new Error('没有创建过 EventSource —— 详情页没有接流')
    return es
  }

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
  emit(event: RunEvent): void {
    this.onmessage?.({ data: JSON.stringify(event) } as MessageEvent<string>)
  }
}

describe('RunDetailView', () => {
  it('正常的一份报告：决策、分布、按文件分组都出来', async () => {
    const wrapper = await render({
      run: runRow({ block_merge: true, github_comment_id: 5324355208 }),
      report: reviewReport({
        decision_reason: 'secrets_found',
        findings: [
          aggregatedFinding({ file: 'app/api.py', line: 32, severity: 'critical' }),
          aggregatedFinding({
            file: 'app/db.py',
            line: 18,
            severity: 'medium',
            message: 'SQL 拼接',
          }),
        ],
      }),
      events: [],
    })

    const text = wrapper.text()
    expect(text).toContain('建议阻止合并')
    expect(text).toContain('凭据泄露') // decision_reason 的中文说明
    expect(text).toContain('app/api.py')
    expect(text).toContain('app/db.py')
    expect(text).toContain('SQL 拼接')
    expect(wrapper.findAll('.group')).toHaveLength(2)
  })

  it('报告还没生成时说的是「还没跑完」，而不是「没有问题」', async () => {
    // 这两句话在界面上长得像，含义正好相反。把「还没跑完」显示成「没有问题」
    // 会让一次审查看起来通过了，而它其实根本没跑完。
    const wrapper = await render({ run: runRow({ status: 'waiting' }), report: null, events: [] })

    expect(wrapper.text()).toContain('报告还没生成')
    expect(wrapper.text()).not.toContain('这次审查没有发现问题')
  })

  it('审查失败时说的是失败，并把人指向时间线', async () => {
    const wrapper = await render({ run: runRow({ status: 'failed' }), report: null, events: [] })
    expect(wrapper.text()).toContain('这次审查失败了')
  })

  it('一条问题都没有时给出空状态（那是正常结果，不是错误）', async () => {
    const wrapper = await render({
      run: runRow(),
      report: reviewReport({
        findings: [],
        block_merge: false,
        decision_reason: 'below_threshold',
      }),
      events: [],
    })

    expect(wrapper.text()).toContain('这次审查没有发现问题')
    expect(wrapper.text()).toContain('不阻断合并')
    expect(wrapper.findAll('.group')).toHaveLength(0)
  })

  it('被置信度闸拦下的那些单独一栏，并说明为什么留着', async () => {
    const wrapper = await render({
      run: runRow(),
      report: reviewReport({
        suppressed: [
          aggregatedFinding({
            message: '可疑但不确定的一条',
            stage: 'suppressed',
            adjusted_confidence: 0.2,
          }),
        ],
      }),
      events: [],
    })

    const text = wrapper.text()
    expect(text).toContain('被置信度闸拦下的 1 条')
    expect(text).toContain('可疑但不确定的一条')
    expect(text).toContain('入库但不发布')
  })

  it('降级运行会在状态栏上说出来，并点了名', async () => {
    const wrapper = await render({
      run: runRow({ degraded: true, missing_workers: ['style'] }),
      report: reviewReport({ degraded: true, missing_workers: ['style'] }),
      events: [],
    })
    expect(wrapper.text()).toContain('降级运行')
    expect(wrapper.text()).toContain('1 个 Worker 未上报')
  })

  // ── 实时时间线（SSE）的接法 ─────────────────────────────────────────── //

  it('正在跑的 run：从首屏最大的 seq 接着开流，新事件进时间线', async () => {
    const history = completeRunEvents().slice(0, 3) // 建 run / 规划 / 派发
    const cursor = history.at(-1)!.seq

    const wrapper = await render({
      run: runRow({ status: 'waiting' }),
      report: null,
      events: history,
    })

    // 首屏那批已经在页面上（不是开流之后才有的）
    expect(wrapper.text()).toContain('Satan508-h/sfly-playground#1')
    // **游标 = 首屏最大的 seq** —— 少了它会把已经显示过的事件再收一遍
    expect(FakeEventSource.last().url).toContain(`after=${cursor}`)

    FakeEventSource.last().emit(
      runEvent({
        seq: cursor + 1,
        kind: 'aggregate.done',
        payload: { findings: 3, conflicts: 0, suppressed: 0 },
      }),
    )
    await flushPromises()

    expect(wrapper.text()).toContain('3 条发现')
  })

  it('收到 run.finished：收流，并重新拉一次把刚生成的报告补上', async () => {
    const history = completeRunEvents().slice(0, 3)
    const wrapper = await render({
      run: runRow({ status: 'waiting' }),
      report: null,
      events: history,
    })

    fetchRunMock.mockResolvedValue({
      run: runRow({ status: 'published' }),
      report: reviewReport(),
      events: completeRunEvents(),
    })
    FakeEventSource.last().emit(
      runEvent({ seq: 999, kind: 'run.finished', payload: { status: 'published' } }),
    )
    await flushPromises()

    expect(FakeEventSource.last().closed).toBe(true)
    // 报告是 finalize 之后才落库的 —— 不重拉的话，「边跑边看」的人会一直
    // 盯着「报告还没生成」
    expect(fetchRunMock.mock.calls.length).toBeGreaterThan(1)
    expect(wrapper.text()).toContain('建议阻止合并')
  })

  it('已经跑完、事件也齐了的 run：根本不开流（否则会每 5 秒重连一次）', async () => {
    await render({
      run: runRow({ status: 'published' }),
      report: reviewReport(),
      events: completeRunEvents(),
    })

    // EventSource 在服务端关流后会自动重连，而服务端对终态 run 只会
    // 再宽限 5 秒就关 —— 每连一次都是白连，而且永远不停
    expect(FakeEventSource.instances).toHaveLength(0)
  })

  it('run 不存在时给出「还没到」而不是错误，并自动重试', async () => {
    // 投递之后 run 由编排器创建，那一小段窗口里 404 是**正常的**。
    vi.useFakeTimers()
    try {
      fetchRunMock.mockRejectedValue({ response: { status: 404 } })
      const wrapper = mountWithUi(RunDetailView, {
        props: { taskId: '01M3DVHP409TBPY721JN9VTA0P' },
        global: { mocks: { $router: { push: vi.fn() } } },
      })
      await flushPromises()

      expect(wrapper.text()).toContain('这个 run 还不存在')
      expect(fetchRunMock).toHaveBeenCalledTimes(1)

      // 800ms 之后应当再试一次 —— 这就是「run 由编排器创建」那段延迟的处置
      await vi.advanceTimersByTimeAsync(900)
      expect(fetchRunMock.mock.calls.length).toBeGreaterThan(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
