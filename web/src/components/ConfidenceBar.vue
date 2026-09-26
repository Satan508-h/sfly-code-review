<script setup lang="ts">
/**
 * 置信度条。
 *
 * 这个组件承载了项目里最值得讲的一处设计，所以它有两个刻度而不是一个：
 *
 *   * **实条** = `adjusted_confidence`，主 Agent 用确定性公式重算的值。
 *     **界面上一切排序、筛选、阻断判断都该以它为准。**
 *   * **空心竖线** = `confidence`，LLM 自报的值。
 *
 * 为什么要同时画出来：LLM 自报置信度**系统性偏高且跨 Worker 不可比**
 * （同一个 0.9 在 security 和 style 那里不是同一件事）。把两个刻度并排放在
 * 一起，「0.90 自报 → 0.60 重算」这一眼就能看见 —— 而只显示一个数字的话，
 * 这个设计就退化成了「一个没人看得懂的百分数」。
 *
 * 数字本身也给两个，但原始值只在差值 ≥ 0.02 时显示（更小的差值只是噪音）。
 * 置信度低于 0.35 的会被置信度闸拦下（`suppressed`），那种卡片上会多一行说明。
 */
import { computed } from 'vue'

import { confidencePair } from '@/lib/findings'
import type { AggregatedFinding } from '@/api/client'

const props = defineProps<{ finding: AggregatedFinding }>()

/** 展示层阈值。与后端那道闸的阈值一致（`confidence.py` 里的 SUPPRESS_BELOW）。 */
const SUPPRESS_BELOW = 0.35

const pair = computed(() => confidencePair(props.finding))

const pct = (v: number) => `${Math.round(Math.min(1, Math.max(0, v)) * 100)}%`

const tone = computed(() => {
  const v = pair.value.adjusted
  if (v >= 0.75) return 'high'
  if (v >= 0.55) return 'mid'
  return 'low'
})

/** 被闸拦下的条数在这里单独说明 —— 否则「0.28」看起来只是一个更小的数。 */
const suppressed = computed(() => props.finding.stage === 'suppressed')
</script>

<template>
  <div class="conf" :class="[tone, { suppressed }]">
    <div class="track">
      <div class="fill" :style="{ width: pct(pair.adjusted) }" />
      <!-- 模型自报值：一条空心竖线。它可能落在实条的右边（自报偏高，常态）
           也可能在左边（自报偏低，少见但更值得注意）。 -->
      <div
        v-if="pair.raw !== null"
        class="raw-mark"
        :style="{ left: pct(pair.raw) }"
        :title="`模型自报 ${pair.raw.toFixed(2)}`"
      />
    </div>
    <div class="nums">
      <span class="adjusted">{{ pair.adjusted.toFixed(2) }}</span>
      <span v-if="pair.raw !== null" class="raw">
        <span class="arrow">←</span> 自报 {{ pair.raw.toFixed(2) }}
      </span>
      <span v-if="suppressed" class="tag" :title="`低于 ${SUPPRESS_BELOW}，入库但不发布`">
        已拦下
      </span>
    </div>
  </div>
</template>

<style scoped>
.conf {
  --c: var(--sfly-text-dim);
  min-width: 150px;
}
.conf.high {
  --c: #16a34a;
}
.conf.mid {
  --c: #ca8a04;
}
.conf.low {
  --c: #dc2626;
}

.track {
  position: relative;
  height: 6px;
  border-radius: 3px;
  background: var(--sfly-border);
  overflow: visible;
}
.fill {
  height: 100%;
  border-radius: 3px;
  background: var(--c);
  transition: width 0.25s ease;
}
.raw-mark {
  position: absolute;
  top: -3px;
  width: 2px;
  height: 12px;
  background: var(--sfly-text);
  opacity: 0.55;
  border-radius: 1px;
}

.nums {
  display: flex;
  align-items: baseline;
  gap: 6px;
  margin-top: 3px;
  font-size: 11px;
  font-family: var(--sfly-mono);
}
.adjusted {
  font-weight: 600;
  color: var(--c);
}
.raw {
  color: var(--sfly-text-dim);
}
.arrow {
  opacity: 0.7;
}
.tag {
  margin-left: auto;
  padding: 0 5px;
  border-radius: 3px;
  background: var(--sfly-border);
  color: var(--sfly-text-dim);
  font-family: inherit;
  cursor: help;
}
.conf.suppressed .fill {
  opacity: 0.5;
}
</style>
