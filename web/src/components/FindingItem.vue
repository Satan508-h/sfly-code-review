<script setup lang="ts">
/**
 * 一条发现。
 *
 * 信息按「读一条代码审查意见」的顺序排：
 *
 *   1. **多严重、是什么问题** —— 一句话说完
 *   2. **凭什么这么说** —— 证据（那段代码本身）
 *   3. **那该怎么办** —— 建议
 *   4. **这条有多可信、谁报的、命中了哪条规则** —— 元信息，放最后
 *
 * 把置信度放在最后而不是最前，是刻意的：人先判断「这条说得对不对」，
 * 再看「系统有多确定」；反过来会让人被数字带着走。
 */
import { computed } from 'vue'

import type { AggregatedFinding } from '@/api/client'
import { WORKER_LABEL, WORKER_VAR } from '@/lib/format'
import { ruleHint } from '@/lib/findings'

import ConfidenceBar from './ConfidenceBar.vue'
import SeverityChip from './SeverityChip.vue'

const props = withDefaults(
  defineProps<{
    finding: AggregatedFinding
    /** 分组视图里文件名在组标题上，单条就不必重复；平铺视图里要显示。 */
    showFile?: boolean
  }>(),
  { showFile: false },
)

const f = computed(() => props.finding)
const hint = computed(() => ruleHint(f.value.rule_id))
</script>

<template>
  <article class="item" :class="{ suppressed: f.stage === 'suppressed' }">
    <header class="head">
      <SeverityChip :severity="f.severity" />
      <h4 class="msg">{{ f.message }}</h4>
      <el-tag v-if="f.needs_human_review" type="warning" size="small" effect="plain">
        需人工复核
      </el-tag>
    </header>

    <!-- 证据：那段代码本身。**逐字来自 diff**，不是模型转述的 -->
    <pre v-if="f.evidence" class="evidence">{{ f.evidence }}</pre>

    <p v-if="f.suggestion" class="suggestion"><span class="lead">建议</span>{{ f.suggestion }}</p>

    <footer class="meta">
      <span v-if="showFile" class="where">
        <code>{{ f.file }}</code
        >:{{ f.line }}
      </span>
      <span v-else class="where">行 {{ f.line }}{{ f.end_line ? `–${f.end_line}` : '' }}</span>

      <el-tooltip
        v-if="!f.source_line_verified"
        content="行号没有落在 diff 的变更行上，已降级为文件级评论（GitHub 会拒绝锚定到未变更的行）"
      >
        <span class="flag">行号未校验</span>
      </el-tooltip>

      <span class="cat">{{ f.category }}</span>

      <el-tooltip v-if="hint" :content="hint">
        <span class="rule">{{ f.rule_id }}</span>
      </el-tooltip>

      <span class="sources">
        <el-tooltip v-for="w in f.sources" :key="w" :content="`${WORKER_LABEL[w]} Worker 上报的`">
          <span class="wdot" :style="{ background: `var(${WORKER_VAR[w]})` }" />
        </el-tooltip>
      </span>

      <ConfidenceBar :finding="f" class="conf" />
    </footer>
  </article>
</template>

<style scoped>
.item {
  padding: 12px 14px;
  border-bottom: 1px solid var(--sfly-border);
}
.item:last-child {
  border-bottom: none;
}
.item.suppressed {
  background: repeating-linear-gradient(
    45deg,
    transparent,
    transparent 8px,
    rgba(100, 116, 139, 0.05) 8px,
    rgba(100, 116, 139, 0.05) 16px
  );
}

.head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.msg {
  margin: 0;
  font-size: 13.5px;
  font-weight: 600;
  line-height: 1.5;
  flex: 1 1 auto;
}

.evidence {
  margin: 8px 0 0;
  padding: 8px 10px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 6px;
  font-family: var(--sfly-mono);
  font-size: 12px;
  line-height: 1.5;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}

.suggestion {
  margin: 8px 0 0;
  font-size: 13px;
  color: var(--sfly-text);
  line-height: 1.5;
}
.lead {
  display: inline-block;
  margin-right: 6px;
  padding: 0 6px;
  border-radius: 3px;
  background: color-mix(in srgb, #2563eb 12%, transparent);
  color: #2563eb;
  font-size: 11px;
  font-weight: 600;
}

.meta {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 10px;
  font-size: 11px;
  color: var(--sfly-text-dim);
  flex-wrap: wrap;
}
.where {
  font-family: var(--sfly-mono);
  color: var(--sfly-text-dim);
}
.cat {
  font-family: var(--sfly-mono);
  padding: 1px 6px;
  border-radius: 3px;
  background: var(--sfly-bg);
  border: 1px solid var(--sfly-border);
}
.rule {
  font-family: var(--sfly-mono);
  color: #7c3aed;
  cursor: help;
}
.flag {
  color: #b45309;
  cursor: help;
}
.sources {
  display: inline-flex;
  gap: 3px;
}
.wdot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  display: inline-block;
}
.conf {
  margin-left: auto;
}
</style>
