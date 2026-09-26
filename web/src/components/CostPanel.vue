<script setup lang="ts">
/**
 * 成本与规模明细。
 *
 * 这一页回答面试里几乎一定会被问的那句：**「跑一次要多少钱、多快」**。
 *
 * 三处刻意的处理：
 *
 * * **耗时按 Worker 拆开画**（零依赖手绘的横条）。三个 Worker 是并行的，
 *   总量看不出谁慢 —— 而「哪个 Worker 该换更小的模型」正是这张图要回答的问题。
 * * **成本为 0 时要说清是「没花钱」而不是「没算」**。Mock LLM 下 `cost_usd`
 *   恒为 0，一个光秃秃的 `$0` 会让人以为这块没接上。
 * * **缓存命中率的分母是 `tokens_in`**。DeepSeek 有自动前缀缓存，而这个项目
 *   刻意把提示词组织成「稳定前缀 + 变化后缀」就是为了命中它 —— 命中率直接
 *   决定有效开销比。分母为 0 时显示「—」而不是 0%。
 */
import { computed } from 'vue'

import type { ReviewReport, RunTotals } from '@/api/client'
import { cacheHitRate, fmtCost, fmtDuration, fmtInt, WORKER_LABEL, WORKER_VAR } from '@/lib/format'

const props = defineProps<{
  totals: RunTotals
  filesTotal: number
  filesReviewed: number
  diffTruncated: boolean
  /** 报告里的条数。run 还没跑完时是 undefined。 */
  findings?: number | undefined
  suppressed?: number | undefined
  report?: ReviewReport | null | undefined
}>()

/** 每个 Worker 的耗时，按从慢到快排。空的（没上报的）不画。 */
const workers = computed(() =>
  Object.entries(props.totals.per_worker_ms ?? {})
    .map(([type, ms]) => ({ type, ms }))
    .sort((a, b) => b.ms - a.ms),
)

const maxMs = computed(() => Math.max(1, ...workers.value.map((w) => w.ms)))

const isMock = computed(() => props.totals.llm_calls === 0 && props.totals.cost_usd === 0)

function label(type: string): string {
  return WORKER_LABEL[type as keyof typeof WORKER_LABEL] ?? type
}
function color(type: string): string {
  const v = WORKER_VAR[type as keyof typeof WORKER_VAR]
  return v ? `var(${v})` : 'var(--sfly-text-dim)'
}
</script>

<template>
  <div class="cost">
    <el-row :gutter="12">
      <el-col :xs="12" :sm="8" :md="6">
        <div class="cell">
          <div class="label">单次成本</div>
          <div class="value mono">{{ fmtCost(totals.cost_usd) }}</div>
          <div class="sub">
            {{ isMock ? 'mock 模式：没花钱，不是没算' : `${fmtInt(totals.llm_calls)} 次 LLM 调用` }}
          </div>
        </div>
      </el-col>
      <el-col :xs="12" :sm="8" :md="6">
        <div class="cell">
          <div class="label">输入 token</div>
          <div class="value mono">{{ fmtInt(totals.tokens_in) }}</div>
          <div class="sub">三个 Worker 各自收到一份 diff</div>
        </div>
      </el-col>
      <el-col :xs="12" :sm="8" :md="6">
        <div class="cell">
          <div class="label">缓存命中</div>
          <div class="value mono">
            {{ cacheHitRate(totals.cached_tokens, totals.tokens_in) }}
          </div>
          <div class="sub">{{ fmtInt(totals.cached_tokens) }} / {{ fmtInt(totals.tokens_in) }}</div>
        </div>
      </el-col>
      <el-col :xs="12" :sm="8" :md="6">
        <div class="cell">
          <div class="label">总耗时</div>
          <div class="value mono">{{ fmtDuration(totals.duration_ms) }}</div>
          <div class="sub">含屏障等待，非三次调用之和</div>
        </div>
      </el-col>
    </el-row>

    <!-- 按 Worker 拆开的耗时。并行跑的三条 lane，只看总量看不出谁慢 -->
    <div class="section">
      <h4>按 Worker 拆分</h4>
      <div v-if="workers.length === 0" class="dim">这次没有 Worker 上报耗时</div>
      <div v-for="w in workers" :key="w.type" class="bar-row">
        <span class="bar-label">{{ label(w.type) }}</span>
        <div class="bar-track">
          <div
            class="bar-fill"
            :style="{ width: `${Math.max(2, (w.ms / maxMs) * 100)}%`, background: color(w.type) }"
          />
        </div>
        <span class="bar-value mono">{{ fmtDuration(w.ms) }}</span>
      </div>
      <p class="note">
        三条 lane 是**并行**的（各自一个消费者组，Redis 保证一条消息只投给组内一个成员），
        所以三个数加起来不等于总耗时。这张图要看的是**谁慢**。
      </p>
    </div>

    <!-- 审查范围 -->
    <div class="section">
      <h4>审查范围</h4>
      <div class="rows">
        <div class="row">
          <span class="k">文件</span>
          <span class="v mono">{{ filesReviewed }} / {{ filesTotal }}</span>
          <span class="hint">{{
            diffTruncated ? 'diff 过大，只审了风险最高的前几个' : '全部审完'
          }}</span>
        </div>
        <div v-if="findings !== undefined" class="row">
          <span class="k">发现</span>
          <span class="v mono">{{ findings }} 条已发布</span>
          <span class="hint">
            {{ suppressed ? `${suppressed} 条置信度不足，入库但不发布` : '没有被拦下的' }}
          </span>
        </div>
        <div v-if="report" class="row">
          <span class="k">token 流向</span>
          <span class="v mono">
            {{ fmtInt(report.totals.tokens_in) }} in / {{ fmtInt(report.totals.tokens_out) }} out
          </span>
          <span class="hint">结构化 JSON 的输出 token 占比不高，但每一条都要能解析</span>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.cell {
  padding: 10px 14px;
  border: 1px solid var(--sfly-border);
  border-radius: 8px;
  background: var(--sfly-surface);
  height: 100%;
}
.label {
  color: var(--sfly-text-dim);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.value {
  font-size: 17px;
  font-weight: 600;
  margin: 4px 0 2px;
}
.sub {
  color: var(--sfly-text-dim);
  font-size: 11px;
  line-height: 1.4;
}

.section {
  margin-top: 18px;
}
.section h4 {
  margin: 0 0 10px;
  font-size: 13.5px;
}

.bar-row {
  display: grid;
  grid-template-columns: 64px 1fr 76px;
  align-items: center;
  gap: 10px;
  padding: 4px 0;
}
.bar-label {
  font-size: 12.5px;
}
.bar-track {
  height: 12px;
  background: var(--sfly-bg);
  border: 1px solid var(--sfly-border);
  border-radius: 6px;
  overflow: hidden;
}
.bar-fill {
  height: 100%;
  border-radius: 5px;
  transition: width 0.3s ease;
}
.bar-value {
  font-size: 11.5px;
  color: var(--sfly-text-dim);
  text-align: right;
}
.note {
  margin: 10px 0 0;
  font-size: 12px;
  color: var(--sfly-text-dim);
  line-height: 1.6;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.row {
  display: flex;
  align-items: baseline;
  gap: 10px;
  font-size: 13px;
  padding: 6px 0;
  border-bottom: 1px solid var(--sfly-border);
}
.row:last-child {
  border-bottom: none;
}
.k {
  flex: 0 0 72px;
  color: var(--sfly-text-dim);
  font-size: 12px;
}
.v {
  flex: 0 0 auto;
}
.hint {
  color: var(--sfly-text-dim);
  font-size: 12px;
}
.dim {
  color: var(--sfly-text-dim);
  font-size: 12px;
}
</style>
