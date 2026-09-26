<script setup lang="ts">
/**
 * 单个 run 的详情。
 *
 * 这一页要在一屏里回答四个问题，顺序就是页面从上往下的顺序：
 *
 *   1. **这是在审什么？** —— 仓库、PR、提交、时间
 *   2. **审完了吗、出了问题吗？** —— 状态、降级徽章、阻断标记、成本
 *   3. **审出了什么？** —— 按文件分组的发现
 *   4. **它到底怎么跑的？** —— 事件时间线（SSE 实时）
 *
 * ### 「run 不存在」要当成正常状态处理，不是错误
 *
 * 投递之后立刻打开详情页，可能拿到 **404** —— 因为 run 由编排器创建，
 * 而编排器要先消费到那条 bootstrap。正常在百毫秒级，但**这就是两个进程
 * 之间的真实延迟**，不是异常。所以这一页对 404 的处置是自动重试几次，
 * 而不是弹一个红色错误框。
 *
 * 这段重试**曾经是死的**：`load(auto)` 里判 `if (auto && ...)`，而
 * `onMounted(() => void load())` 从不传参数 —— 于是 `auto` 恒为 `false`，
 * 那句 setTimeout 一次都没执行过，页面文档却写着「会自动重试」。
 * 是渲染测试抓出来的（`RunDetailView.spec.ts` 里那条「run 不存在时……并自动重试」）。
 * 现在改成：**404 一定安排重试**，`auto` 只决定要不要显示骨架屏。
 *
 * ### 时间线为什么「终态也可能开流」
 *
 * 首屏是全量的（`GET /api/runs/{id}` 一次给全），之后才接 SSE。一个已经跑完的
 * run 通常不需要开流 —— 但有一个真实的窗口：`publish` **先写状态、后写事件**，
 * 所以在那两步之间抓到详情页的话，run 读作已完成而最后那条 `run.finished`
 * 还没落库。所以判据不是「run 到终态了吗」，而是「终态**且**事件齐了吗」。
 */
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'

import {
  fetchRun,
  prUrl,
  type AggregatedFinding,
  type RunDetailResponse,
  type RunEvent,
  type Severity,
} from '@/api/client'
import { openRunStream, type RunStream } from '@/api/sse'
import ConfidenceBar from '@/components/ConfidenceBar.vue'
import ConflictsPanel from '@/components/ConflictsPanel.vue'
import CostPanel from '@/components/CostPanel.vue'
import FindingItem from '@/components/FindingItem.vue'
import RunTimeline from '@/components/RunTimeline.vue'
import SeverityChip from '@/components/SeverityChip.vue'
import {
  fmtCost,
  fmtDuration,
  fmtInt,
  fmtTime,
  isTerminal,
  RUN_STATUS_LABEL,
  RUN_STATUS_TYPE,
  shortSha,
} from '@/lib/format'
import { decisionText, groupByFile, severityCounts, sortFindings } from '@/lib/findings'

const props = defineProps<{ taskId: string }>()

const data = ref<RunDetailResponse | null>(null)
const error = ref<string | null>(null)
const notFound = ref(false)
const loading = ref(true)
/** 自动重试了几次。用来在页面上说明「还在等编排器接手」，而不是干转圈。 */
const retries = ref(0)

const MAX_RETRY = 8
const RETRY_MS = 800

const run = computed(() => data.value?.run ?? null)
const report = computed(() => data.value?.report ?? null)

const tab = ref<'findings' | 'timeline' | 'analysis'>('findings')

// ── 时间线 ──────────────────────────────────────────────────────────────── //

/** 首屏那批 + 流里新来的，按 seq 合并。走 SSE 时它就是页面上那条时间线。 */
const events = ref<RunEvent[]>([])
const streamState = ref<'connecting' | 'live' | 'closed' | 'unsupported'>('connecting')

let stream: RunStream | null = null
/** 收流用的计时器（见下面 `openStream` 的说明）。 */
let streamTimer: ReturnType<typeof setTimeout> | null = null

function closeStream(): void {
  stream?.close()
  stream = null
  if (streamTimer !== null) {
    clearTimeout(streamTimer)
    streamTimer = null
  }
}

/** 合并一条新事件。**按 seq 去重** —— 首屏那批和流之间允许重叠。 */
function pushEvent(event: RunEvent): void {
  if (events.value.some((e) => e.seq === event.seq)) return
  events.value = [...events.value, event].sort((a, b) => a.seq - b.seq)
}

/**
 * 接上事件流。
 *
 * `alreadyTerminal` 时开的是**有限时长**的流：只为了把可能还差的那条
 * `run.finished` 等回来，等不到也得收 —— 否则服务端宽限期一过就关流，
 * 而 EventSource 会**自动重连**，于是变成每 5 秒连一次的无限循环
 * （服务端不会再有新事件，也就永远不会主动说不连了）。
 */
function openStream(cursor: number, alreadyTerminal: boolean): void {
  closeStream()
  if (typeof (globalThis as { EventSource?: unknown }).EventSource === 'undefined') {
    streamState.value = 'unsupported'
    return
  }

  streamState.value = 'connecting'
  stream = openRunStream(props.taskId, cursor, {
    onEvent: (event) => pushEvent(event),
    onOpen: () => {
      streamState.value = 'live'
    },
    onError: () => {
      // 浏览器会自动重连并带上 Last-Event-ID，服务端负责补齐缺口 ——
      // 所以这里只改状态，不自己写重连。
      if (stream?.live) streamState.value = 'connecting'
    },
    onDone: (reason) => {
      streamState.value = 'closed'
      closeStream()
      // 收到 run.finished 意味着报告刚刚才落库。**重新拉一次**，
      // 否则「一边跑一边看」的人会一直看着「报告还没生成」。
      if (reason === 'finished') void load({ auto: true })
    },
  })

  if (alreadyTerminal) {
    streamTimer = setTimeout(() => {
      streamState.value = 'closed'
      closeStream()
    }, 15_000)
  }
}

// ── 发现 ────────────────────────────────────────────────────────────────── //

// ── 发现 ────────────────────────────────────────────────────────────────── //

/** 点击严重度标签筛选。`null` = 不筛。 */
const levelFilter = ref<Severity | null>(null)

const shownFindings = computed<AggregatedFinding[]>(() => {
  const all = report.value?.findings ?? []
  const filtered = levelFilter.value ? all.filter((f) => f.severity === levelFilter.value) : all
  return sortFindings(filtered)
})

const groups = computed(() => groupByFile(shownFindings.value))
const counts = computed(() => severityCounts(report.value?.findings ?? []))
const suppressed = computed(() => report.value?.suppressed ?? [])

const totalFindings = computed(() => report.value?.findings.length ?? 0)

function toggleLevel(s: Severity): void {
  levelFilter.value = levelFilter.value === s ? null : s
}

/** 「评论原文」对话框 —— 报告里那段要发到 PR 上的 Markdown。 */
const commentOpen = ref(false)

/** 挂起的重试定时器。**必须在卸载时清掉** —— 否则组件销毁之后它还会发一次请求，
 *  而在测试里表现为「用例已经结束，却还有未决的定时器」。 */
let retryTimer: ReturnType<typeof setTimeout> | null = null

function clearRetry(): void {
  if (retryTimer !== null) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
}

/**
 * 404 之后安排下一次重试。
 *
 * 次数上限是必要的：地址本来就拼错的话，无限重试会让页面看起来在「加载中」
 * 永远不结束，而正确的结果是明确告诉人「这个 id 不存在」。
 */
function scheduleRetry(): void {
  if (retries.value >= MAX_RETRY) return
  retries.value += 1
  clearRetry()
  retryTimer = setTimeout(() => void load({ auto: true }), RETRY_MS)
}

async function load(opts: { auto?: boolean } = {}): Promise<void> {
  // `auto` 只决定**要不要显示骨架屏**：自动重试时把已有内容换成骨架屏，
  // 页面会每 800 毫秒闪一次。它不参与「要不要重试」的判断 ——
  // 那正是这里出过的 bug。
  if (!opts.auto) loading.value = true
  try {
    data.value = await fetchRun(props.taskId)
    error.value = null
    notFound.value = false
    retries.value = 0
    clearRetry()

    // 首屏事件是全量的，所以时间线**先有内容再有连接** —— 不会出现
    // 「报告已经显示了、时间线还在转圈」那个中间态。
    events.value = [...data.value.events].sort((a, b) => a.seq - b.seq)
    const cursor = events.value.reduce((max, e) => Math.max(max, e.seq), 0)
    const terminal = isTerminal(data.value.run.status)
    const hasFinished = events.value.some((e) => e.kind === 'run.finished')
    if (terminal && hasFinished) {
      // 跑完了、事件也齐了 —— 没有可等的
      closeStream()
      streamState.value = 'closed'
    } else {
      openStream(cursor, terminal)
    }

    // 正在跑的 run 默认落在时间线上：那一刻「发现」页只会说「报告还没生成」，
    // 而人点进来想看的是「它跑到哪了」。
    if (!terminal && data.value.report === null) tab.value = 'timeline'
  } catch (e) {
    // axios 的错误对象里才有状态码；拿不到就按「其他错误」处理。
    const status = (e as { response?: { status?: number } }).response?.status
    if (status === 404) {
      notFound.value = true
      error.value = null
      // 刚投递完的窗口期：等编排器把 run 建出来。
      scheduleRetry()
    } else {
      error.value = e instanceof Error ? e.message : String(e)
      clearRetry()
    }
  } finally {
    loading.value = false
  }
}

/** 「再试一次」按钮：人点了就重置计数，否则次数用完之后那一按什么也不会发生。 */
function retryNow(): void {
  retries.value = 0
  void load()
}

onMounted(() => void load())
onUnmounted(() => {
  clearRetry()
  closeStream()
})
// 同一个组件复用于不同 run 时（从详情页跳到另一个 run），必须重新拉 ——
// 否则页面会显示上一个 run 的数据，而且**看不出哪里不对**。
// 流也必须重开：不关的话旧 run 的事件会混进新 run 的时间线。
watch(
  () => props.taskId,
  () => {
    levelFilter.value = null
    tab.value = 'findings'
    retries.value = 0
    clearRetry()
    closeStream()
    events.value = []
    data.value = null
    void load()
  },
)
</script>

<template>
  <div class="page">
    <el-card v-if="loading && !run" shadow="never">
      <el-skeleton :rows="5" animated />
    </el-card>

    <!-- 「不存在」不是错误，是「还没到」。文案要说清楚该等还是该查 -->
    <el-card v-else-if="notFound" shadow="never">
      <el-result icon="info" title="这个 run 还不存在">
        <template #sub-title>
          <p class="mono taskid">{{ taskId }}</p>
          <p v-if="retries > 0" class="dim">
            已重试 {{ retries }} 次 —— run 由编排器创建，刚投递完的那一小段窗口里还没有它。
          </p>
          <p v-else class="dim">
            地址对吗？也可能是 run 已过期被清理，或者你打开的是一个拼错的 id。
          </p>
        </template>
        <template #extra>
          <el-button @click="retryNow()">再试一次</el-button>
          <el-button type="primary" @click="$router.push('/runs')">回到运行记录</el-button>
        </template>
      </el-result>
    </el-card>

    <el-alert
      v-else-if="error"
      type="error"
      :closable="false"
      show-icon
      title="读取失败"
      :description="error"
    />

    <template v-else-if="run">
      <!-- ── 顶部状态栏 ────────────────────────────────────────────── -->
      <el-card shadow="never" class="bar">
        <div class="bar-main">
          <el-tag :type="RUN_STATUS_TYPE[run.status]" effect="dark" size="large">
            {{ RUN_STATUS_LABEL[run.status] }}
          </el-tag>

          <a class="pr-link" :href="prUrl(run)" target="_blank" rel="noopener">
            {{ run.repo_id }} <span class="pr">#{{ run.pr_number }}</span>
          </a>

          <!-- 降级 / 阻断两个标记挨着状态放：它们决定「要不要立刻看」。 -->
          <el-tag v-if="run.degraded" type="warning" effect="plain">
            降级运行 —— {{ run.missing_workers.length }} 个 Worker 未上报
          </el-tag>
          <el-tag v-if="run.block_merge" type="danger" effect="plain">建议阻止合并</el-tag>

          <el-button
            v-if="report?.comment_body"
            class="raw-btn"
            size="small"
            @click="commentOpen = true"
          >
            评论原文
          </el-button>
        </div>

        <div class="bar-meta">
          <span
            >提交 <code>{{ shortSha(run.head_sha) }}</code></span
          >
          <span class="sep">·</span>
          <span
            >基线 <code>{{ shortSha(run.base_sha) }}</code></span
          >
          <span class="sep">·</span>
          <span>{{ fmtTime(run.created_at) }}</span>
          <template v-if="run.published_at">
            <span class="sep">·</span>
            <span>发布于 {{ fmtTime(run.published_at) }}</span>
          </template>
          <template v-if="run.github_comment_id">
            <span class="sep">·</span>
            <a
              class="pr-link small"
              :href="`${prUrl(run)}#pullrequestreview-${run.github_comment_id}`"
              target="_blank"
              rel="noopener"
            >
              评论 #{{ run.github_comment_id }}
            </a>
          </template>
        </div>
      </el-card>

      <!-- ── 成本与规模 ────────────────────────────────────────────── -->
      <el-row :gutter="12" class="stats">
        <el-col :xs="12" :sm="8" :md="4">
          <el-card shadow="never" class="stat">
            <div class="stat-label">文件</div>
            <div class="stat-value mono">{{ run.files_reviewed }}/{{ run.files_total }}</div>
            <div class="stat-sub">
              {{ run.diff_truncated ? '已裁剪，只审风险最高的前几个' : '全部审完' }}
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card shadow="never" class="stat">
            <div class="stat-label">耗时</div>
            <div class="stat-value mono">{{ fmtDuration(run.totals?.duration_ms) }}</div>
            <div class="stat-sub">含屏障等待</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card shadow="never" class="stat">
            <div class="stat-label">输入 token</div>
            <div class="stat-value mono">{{ fmtInt(run.totals?.tokens_in) }}</div>
            <div class="stat-sub">3 个 Worker 合计</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card shadow="never" class="stat">
            <div class="stat-label">输出 token</div>
            <div class="stat-value mono">{{ fmtInt(run.totals?.tokens_out) }}</div>
            <div class="stat-sub">含结构化 JSON</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card shadow="never" class="stat">
            <div class="stat-label">成本</div>
            <div class="stat-value mono">{{ fmtCost(run.totals?.cost_usd) }}</div>
            <div class="stat-sub">mock 模式下恒为 0</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="4">
          <el-card shadow="never" class="stat">
            <div class="stat-label">LLM 调用</div>
            <div class="stat-value mono">{{ fmtInt(run.totals?.llm_calls) }}</div>
            <div class="stat-sub">
              {{ run.totals?.llm_calls === 0 ? '结果来自缓存 / mock' : '真实调用次数' }}
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- ── 标签页 ────────────────────────────────────────────────── -->
      <el-tabs v-model="tab" class="tabs">
        <!-- 发现 -->
        <el-tab-pane name="findings">
          <template #label>
            <span>发现</span>
            <span v-if="report" class="n">{{ totalFindings }}</span>
          </template>

          <!-- run 还没跑到 finalize：报告不存在，这不是「没问题」 -->
          <el-card v-if="!report" shadow="never">
            <el-result
              icon="info"
              :title="run.status === 'failed' ? '这次审查失败了，没有报告' : '报告还没生成'"
            >
              <template #sub-title>
                <p class="dim">
                  {{
                    run.status === 'failed'
                      ? 'run 在汇总之前就失败了 —— 时间线里能看到是哪一步。'
                      : '报告由 finalize 节点生成。这个 run 还在跑，切到「时间线」看进度。'
                  }}
                </p>
              </template>
            </el-result>
          </el-card>

          <template v-else>
            <!-- 决策行：阻断与否 + 为什么。这句话来自确定性规则引擎，不是 LLM 写的 -->
            <el-card shadow="never" class="decision" :class="{ block: report.block_merge }">
              <div class="dec-main">
                <strong class="dec-title">
                  {{ report.block_merge ? '建议阻止合并' : '不阻断合并' }}
                </strong>
                <span class="dec-reason">{{ decisionText(report.decision_reason) }}</span>
                <code class="dec-slug">{{ report.decision_reason }}</code>
              </div>
              <p class="dec-note">
                阻断判断由确定性规则引擎做（不是 LLM）——
                这样同一份输入永远得到同一个结论，评测才可复现。
                <strong>永不发 APPROVE</strong>：机器人审批人类 PR 是策略漏洞。
              </p>
            </el-card>

            <!-- 严重度分布。点一下就是筛选 -->
            <div class="dist">
              <button
                v-for="c in counts"
                :key="c.severity"
                class="dist-item"
                :class="{ off: c.count === 0, active: levelFilter === c.severity }"
                :disabled="c.count === 0"
                @click="toggleLevel(c.severity)"
              >
                <SeverityChip :severity="c.severity" :count="c.count" />
              </button>
              <span v-if="levelFilter" class="clear" @click="levelFilter = null">清除筛选</span>
              <span v-if="suppressed.length" class="suppressed-hint">
                另有 {{ suppressed.length }} 条因置信度不足被拦下（未发布）
              </span>
            </div>

            <!-- 没发现任何问题。这是**正常结果**，不是错误 —— clean.diff 就是为了
                 验证这一点存在的：多数学生只测「能不能发现问题」，不测误报。 -->
            <el-card v-if="totalFindings === 0" shadow="never">
              <el-result icon="success" title="这次审查没有发现问题">
                <template #sub-title>
                  <p class="dim">
                    干净的结果同样是有价值的证据 —— 评测集里专门有一组「无问题 diff」
                    用来测误报率，因为只统计命中的精确率是没有意义的。
                  </p>
                </template>
              </el-result>
            </el-card>

            <el-empty v-else-if="shownFindings.length === 0" description="当前筛选下没有发现" />

            <!-- 按文件分组 -->
            <el-card
              v-for="g in groups"
              v-else
              :key="g.file"
              shadow="never"
              class="group"
              :class="`sev-${g.worst}`"
            >
              <template #header>
                <div class="group-head">
                  <code class="file">{{ g.file }}</code>
                  <span class="group-count">{{ g.findings.length }} 条</span>
                  <SeverityChip :severity="g.worst" class="group-sev" />
                </div>
              </template>
              <FindingItem
                v-for="f in g.findings"
                :key="`${f.file}:${f.line}:${f.category}`"
                :finding="f"
              />
            </el-card>

            <!-- 被置信度闸拦下的。**入库但不发布** —— 留着是为了测量这道闸
                 砍掉了多少真实问题，否则阈值只能盲调。 -->
            <el-collapse v-if="suppressed.length" class="suppressed">
              <el-collapse-item :name="1">
                <template #title>
                  <span class="sup-title">被置信度闸拦下的 {{ suppressed.length }} 条</span>
                  <span class="sup-sub">入库但不发布 —— 为什么留着它们是有意的</span>
                </template>
                <p class="sup-note">
                  置信度低于 0.35 的发现会写进 <code>findings</code> 表但**不进 PR 评论**：
                  它们是评测集测量「这道闸砍掉了多少真实问题」的唯一数据来源。
                  只记录发出去的那些，阈值就永远只能靠感觉调。
                </p>
                <div class="sup-list">
                  <div v-for="f in suppressed" :key="`${f.file}:${f.line}`" class="sup-row">
                    <SeverityChip :severity="f.severity" />
                    <code class="sup-where">{{ f.file }}:{{ f.line }}</code>
                    <span class="sup-msg">{{ f.message }}</span>
                    <ConfidenceBar :finding="f" />
                  </div>
                </div>
              </el-collapse-item>
            </el-collapse>
          </template>
        </el-tab-pane>

        <!-- 时间线 -->
        <el-tab-pane name="timeline">
          <template #label>
            <span>时间线</span>
            <span v-if="events.length" class="n">{{ events.length }}</span>
          </template>
          <RunTimeline :events="events" :status="run.status" :stream="streamState" />
        </el-tab-pane>

        <!-- 冲突与成本 -->
        <el-tab-pane name="analysis">
          <template #label>
            <span>冲突与成本</span>
            <span v-if="report?.conflicts.length" class="n">{{ report.conflicts.length }}</span>
          </template>

          <el-card shadow="never" class="block">
            <template #header>
              <span class="block-title">冲突消解</span>
              <span class="block-hint">确定性规则引擎 · 不用 LLM 裁判</span>
            </template>
            <ConflictsPanel :conflicts="report?.conflicts ?? []" />
          </el-card>

          <el-card shadow="never" class="block">
            <template #header>
              <span class="block-title">成本与规模</span>
              <span class="block-hint">单次审查的实际开销 —— 面试里几乎一定会被问的那个数</span>
            </template>
            <CostPanel
              v-if="run.totals"
              :totals="run.totals"
              :files-total="run.files_total"
              :files-reviewed="run.files_reviewed"
              :diff-truncated="run.diff_truncated"
              :findings="report?.findings.length"
              :suppressed="report?.suppressed.length"
              :report="report"
            />
            <el-empty v-else description="这个 run 还没有成本数据" />
          </el-card>
        </el-tab-pane>
      </el-tabs>

      <!-- 评论原文 -->
      <el-dialog v-model="commentOpen" title="要发到 PR 上的原文" width="760px">
        <pre class="comment-body">{{ report?.comment_body }}</pre>
      </el-dialog>
    </template>
  </div>
</template>

<style scoped>
.bar :deep(.el-card__body) {
  padding: 14px 18px;
}
.bar-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.raw-btn {
  margin-left: auto;
}
.pr-link {
  font-size: 15px;
  font-weight: 600;
  color: #1d4ed8;
  text-decoration: none;
}
.pr-link:hover {
  text-decoration: underline;
}
.pr-link.small {
  font-size: 12px;
  font-weight: 400;
}
.pr {
  color: var(--sfly-text-dim);
  font-weight: 400;
}
.bar-meta {
  margin-top: 8px;
  color: var(--sfly-text-dim);
  font-size: 12px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.sep {
  color: var(--sfly-border);
}

.stats {
  margin-top: 12px;
}
.stat {
  text-align: left;
}
.stat-label {
  color: var(--sfly-text-dim);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.stat-value {
  font-size: 17px;
  font-weight: 600;
  margin: 4px 0 2px;
}
.stat-sub {
  color: var(--sfly-text-dim);
  font-size: 11px;
  line-height: 1.4;
}

.tabs {
  margin-top: 16px;
}
.n {
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--sfly-border);
  color: var(--sfly-text-dim);
  font-size: 11px;
}

.block {
  margin-bottom: 12px;
}
.block :deep(.el-card__header) {
  padding: 10px 16px;
  background: var(--sfly-bg);
}
.block-title {
  font-weight: 600;
}
.block-hint {
  margin-left: 10px;
  color: var(--sfly-text-dim);
  font-size: 12px;
}

/* 决策 */
.decision :deep(.el-card__body) {
  padding: 12px 16px;
}
.decision {
  border-left: 3px solid #16a34a;
}
.decision.block {
  border-left-color: var(--sfly-critical);
}
.dec-main {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.dec-title {
  font-size: 14px;
}
.decision.block .dec-title {
  color: var(--sfly-critical);
}
.dec-reason {
  font-size: 13px;
  color: var(--sfly-text);
}
.dec-slug {
  margin-left: auto;
  color: var(--sfly-text-dim);
  font-size: 11px;
}
.dec-note {
  margin: 8px 0 0;
  color: var(--sfly-text-dim);
  font-size: 12px;
  line-height: 1.6;
}

/* 分布 */
.dist {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 12px 0;
  flex-wrap: wrap;
}
.dist-item {
  border: none;
  background: none;
  padding: 0;
  cursor: pointer;
  border-radius: 999px;
}
.dist-item.active :deep(.chip) {
  box-shadow: 0 0 0 2px color-mix(in srgb, currentColor 30%, transparent);
}
.dist-item.off {
  cursor: default;
  opacity: 0.45;
}
.clear {
  margin-left: 4px;
  font-size: 12px;
  color: #2563eb;
  cursor: pointer;
}
.suppressed-hint {
  margin-left: auto;
  font-size: 12px;
  color: var(--sfly-text-dim);
}

/* 分组 */
.group {
  margin-bottom: 12px;
}
.group :deep(.el-card__header) {
  padding: 10px 14px;
  background: var(--sfly-bg);
}
.group :deep(.el-card__body) {
  padding: 0;
}
.group-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.file {
  font-weight: 600;
  font-size: 13px;
  color: var(--sfly-text);
}
.group-count {
  color: var(--sfly-text-dim);
  font-size: 12px;
}
.group-sev {
  margin-left: auto;
}

.suppressed {
  margin-top: 4px;
}
.sup-title {
  font-weight: 600;
  font-size: 13px;
}
.sup-sub {
  margin-left: 10px;
  color: var(--sfly-text-dim);
  font-size: 12px;
}
.sup-note {
  margin: 0 0 10px;
  color: var(--sfly-text-dim);
  font-size: 12px;
  line-height: 1.6;
}
.sup-list {
  display: flex;
  flex-direction: column;
}
.sup-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-top: 1px solid var(--sfly-border);
  font-size: 13px;
}
.sup-where {
  color: var(--sfly-text-dim);
  font-size: 11px;
}
.sup-msg {
  flex: 1 1 auto;
}

.comment-body {
  margin: 0;
  padding: 14px;
  background: var(--sfly-bg);
  border: 1px solid var(--sfly-border);
  border-radius: 6px;
  font-family: var(--sfly-mono);
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 60vh;
  overflow-y: auto;
}

.taskid {
  color: var(--sfly-text-dim);
}
.dim {
  color: var(--sfly-text-dim);
  font-size: 12px;
}
</style>
