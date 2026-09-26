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
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { RunDetailResponse } from '@/api/client'
import { aggregatedFinding, reviewReport, runRow } from '@/testing/factories'
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
})

describe('RunDetailView', () => {
  it('正常的一份报告：决策、分布、按文件分组都出来', async () => {
    const wrapper = await render({
      run: runRow({ block_merge: true, github_comment_id: 5324355208 }),
      report: reviewReport({
        decision_reason: 'secrets_found',
        findings: [
          aggregatedFinding({ file: 'app/api.py', line: 32, severity: 'critical' }),
          aggregatedFinding({ file: 'app/db.py', line: 18, severity: 'medium', message: 'SQL 拼接' }),
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
      report: reviewReport({ findings: [], block_merge: false, decision_reason: 'below_threshold' }),
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
          aggregatedFinding({ message: '可疑但不确定的一条', stage: 'suppressed', adjusted_confidence: 0.2 }),
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
