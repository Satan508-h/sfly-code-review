<script setup lang="ts">
/**
 * 严重度标签。
 *
 * 自己画而不是用 `el-tag` 的 `type`：Element Plus 只有五种语义色
 * （primary/success/warning/danger/info），而我们有**五档严重度** ——
 * 硬套的结果是 `high` 和 `critical` 同色、「中」和「低」也分不开，
 * 而严重度分档正是这张卡片存在的全部意义。
 *
 * 配色仍然只来自 `main.css` 里的 CSS 变量（`--sfly-critical` 等）：
 * 需求文档、评测报告、PR 评论用的是同一套色，截图放在一起才对得上。
 */
import { computed } from 'vue'

import type { Severity } from '@/api/client'
import { SEVERITY_LABEL, SEVERITY_VAR } from '@/lib/format'

const props = withDefaults(defineProps<{ severity: Severity; count?: number | null }>(), {
  count: null,
})

const color = computed(() => `var(${SEVERITY_VAR[props.severity]})`)
</script>

<template>
  <span class="chip" :style="{ '--c': color }">
    <span class="dot" />
    <span class="text">{{ SEVERITY_LABEL[severity] }}</span>
    <span v-if="count !== null" class="count">{{ count }}</span>
  </span>
</template>

<style scoped>
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 1px 8px 1px 6px;
  border-radius: 999px;
  /* 用 color-mix 调淡色底：同一个变量既要当文字色又要当底色，
     写死两套色值就一定会漂移。 */
  background: color-mix(in srgb, var(--c) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--c) 35%, transparent);
  color: var(--c);
  font-size: 12px;
  font-weight: 600;
  line-height: 18px;
  white-space: nowrap;
}
.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--c);
}
.count {
  padding-left: 5px;
  border-left: 1px solid color-mix(in srgb, var(--c) 35%, transparent);
  font-variant-numeric: tabular-nums;
  opacity: 0.85;
}
</style>
