<script setup lang="ts">
/**
 * 时间线：上面一条节点进度，下面一行行事件。
 *
 * ### 为什么是正序（最早的在上）+ 自动滚到底
 *
 * 倒序（最新在上）实现更简单，但读起来是反的 —— 而这一屏的用途正是**讲一遍
 * 它怎么跑完的**。所以按时间正序，并在有新事件时自动滚到底部。
 *
 * 「自动滚」这个行为有个必须处理的副作用：**用户往上翻的时候就该停下来**。
 * 否则他正在看第三条事件，第十条一来就把他拽回底部 —— 一个只有真用起来
 * 才会发现的问题。所以跟随是状态：滚到底部附近就自动开启，往上滚就关掉。
 */
import { computed, nextTick, ref, watch } from 'vue'

import type { RunEvent, RunStatus } from '@/api/client'
import { EVENT_TYPE, NODE_LABEL, fmtTime } from '@/lib/format'
import { isKeyEvent, nodeStates, summarize, type NodeState } from '@/lib/events'

const props = defineProps<{
  events: RunEvent[]
  status: RunStatus
  /** 流的状态。`unsupported` = 环境里没有 EventSource，只有历史事件可看。 */
  stream: 'connecting' | 'live' | 'closed' | 'unsupported'
}>()

const keyOnly = ref(false)
const expanded = ref<Set<number>>(new Set())

const nodes = computed(() => nodeStates(props.events, props.status))

const shown = computed(() => (keyOnly.value ? props.events.filter(isKeyEvent) : props.events))

const NODE_MARK: Record<NodeState, string> = {
  done: '✓',
  active: '◐',
  pending: '·',
  failed: '✕',
}

const STREAM_TEXT: Record<string, string> = {
  connecting: '连接中…',
  live: '实时连接中',
  closed: '已收流（run 已结束）',
  unsupported: '当前环境不支持实时推送，以下是历史事件',
}

function toggle(seq: number): void {
  const next = new Set(expanded.value)
  if (next.has(seq)) next.delete(seq)
  else next.add(seq)
  expanded.value = next
}

// ── 自动滚到底 ───────────────────────────────────────────────────────────── //

const list = ref<HTMLElement | null>(null)
const follow = ref(true)

/** 距底部 40px 以内就算「在跟着」。给一点余量，否则像素级抖动就会关掉跟随。 */
function onScroll(): void {
  const el = list.value
  if (!el) return
  follow.value = el.scrollHeight - el.scrollTop - el.clientHeight < 40
}

watch(
  () => props.events.length,
  async () => {
    if (!follow.value) return
    await nextTick()
    const el = list.value
    if (el) el.scrollTop = el.scrollHeight
  },
)
</script>

<template>
  <div class="timeline">
    <!-- 节点进度 -->
    <div class="nodes">
      <div v-for="(n, i) in nodes" :key="n.node" class="node" :class="n.state">
        <div class="node-box">
          <span class="mark">{{ NODE_MARK[n.state] }}</span>
          <span class="idx">{{ i + 1 }}</span>
          <span class="name">{{ NODE_LABEL[n.node] ?? n.node }}</span>
        </div>
        <div v-if="i < nodes.length - 1" class="arrow" :class="{ lit: n.state === 'done' }">→</div>
      </div>
    </div>

    <!-- 工具行 -->
    <div class="tools">
      <span class="stream" :class="stream">
        <span class="sdot" />
        {{ STREAM_TEXT[stream] }}
      </span>
      <span class="count">{{ events.length }} 条事件</span>
      <el-button size="small" text @click="keyOnly = !keyOnly">
        {{ keyOnly ? '显示全部' : '只看关键节点' }}
      </el-button>
      <el-button v-if="!follow" size="small" text @click="follow = true">↓ 跟随最新</el-button>
    </div>

    <!-- 事件列表 -->
    <div ref="list" class="list" @scroll="onScroll">
      <div
        v-for="e in shown"
        :key="e.seq"
        class="row"
        :class="{ open: expanded.has(e.seq) }"
        @click="toggle(e.seq)"
      >
        <span class="time mono">{{ fmtTime(e.created_at) }}</span>
        <el-tag :type="EVENT_TYPE[e.kind]" size="small" effect="light" class="kind">
          {{ e.kind }}
        </el-tag>
        <span class="text">{{ summarize(e) }}</span>
        <span class="seq mono">#{{ e.seq }}</span>
        <pre v-if="expanded.has(e.seq)" class="raw" @click.stop>{{
          JSON.stringify(e.payload, null, 2)
        }}</pre>
      </div>
      <div v-if="shown.length === 0" class="empty">还没有事件</div>
    </div>
  </div>
</template>

<style scoped>
.nodes {
  display: flex;
  align-items: stretch;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 12px;
}
.node {
  display: flex;
  align-items: center;
  gap: 4px;
}
.node-box {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 11px;
  border-radius: 8px;
  border: 1px solid var(--sfly-border);
  background: var(--sfly-surface);
  font-size: 12px;
  color: var(--sfly-text-dim);
  white-space: nowrap;
}
.node.done .node-box {
  border-color: color-mix(in srgb, #16a34a 35%, transparent);
  background: color-mix(in srgb, #16a34a 8%, transparent);
  color: #15803d;
}
.node.active .node-box {
  border-color: color-mix(in srgb, #2563eb 45%, transparent);
  background: color-mix(in srgb, #2563eb 10%, transparent);
  color: #1d4ed8;
  font-weight: 600;
  /* 当前节点呼吸一下 —— 一眼看出「就是这里在跑」 */
  animation: pulse 1.6s ease-in-out infinite;
}
.node.failed .node-box {
  border-color: var(--sfly-critical);
  background: color-mix(in srgb, var(--sfly-critical) 10%, transparent);
  color: var(--sfly-critical);
}
@keyframes pulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.62;
  }
}
.mark {
  font-weight: 700;
}
.idx {
  font-family: var(--sfly-mono);
  opacity: 0.5;
  font-size: 10px;
}
.arrow {
  color: var(--sfly-border);
  padding: 0 1px;
}
.arrow.lit {
  color: #16a34a;
}

.tools {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--sfly-text-dim);
}
.stream {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.sdot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--sfly-text-dim);
}
.stream.live .sdot {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.18);
  animation: pulse 1.6s ease-in-out infinite;
}
.stream.connecting .sdot {
  background: #d97706;
}
.stream.closed .sdot {
  background: #16a34a;
}
.count {
  margin-left: auto;
}

.list {
  max-height: 460px;
  overflow-y: auto;
  border: 1px solid var(--sfly-border);
  border-radius: 8px;
  background: var(--sfly-surface);
}
.row {
  display: grid;
  grid-template-columns: 74px 128px 1fr auto;
  align-items: baseline;
  gap: 10px;
  padding: 6px 12px;
  border-bottom: 1px solid var(--sfly-border);
  font-size: 12.5px;
  cursor: pointer;
}
.row:last-child {
  border-bottom: none;
}
.row:hover {
  background: var(--sfly-bg);
}
.row.open {
  background: var(--sfly-bg);
}
.time {
  color: var(--sfly-text-dim);
  font-size: 11px;
}
.kind {
  justify-self: start;
  font-family: var(--sfly-mono);
  font-size: 11px;
}
.text {
  color: var(--sfly-text);
  word-break: break-word;
}
.seq {
  color: var(--sfly-text-dim);
  font-size: 10px;
}
.raw {
  grid-column: 1 / -1;
  margin: 6px 0 2px;
  padding: 10px 12px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 6px;
  font-size: 11px;
  line-height: 1.5;
  overflow-x: auto;
  max-height: 260px;
}
.empty {
  padding: 24px;
  text-align: center;
  color: var(--sfly-text-dim);
  font-size: 13px;
}
</style>
