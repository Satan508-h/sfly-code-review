<script setup lang="ts">
/**
 * Step 0 状态页。
 *
 * 它的作用是**证明前后端已经连通**，不是最终形态。M8 会把这里换成
 * 真正的仪表盘：run 列表、实时时间线、按文件分组的 findings、
 * 冲突面板、降级徽章。
 *
 * 这一版刻意保留两个东西，因为 M8 之后仍然有用：
 *   1. 后端配置回显 —— 一眼看出当前跑的是 redis 还是 memory 队列，
 *      是 mock 还是 deepseek。排查「为什么本地是这样、线上是那样」时省事。
 *   2. 流水线节点图 —— 把架构印在首页上，面试官点开链接就知道你在做什么。
 */
import { computed, onMounted, ref } from 'vue'

import { API_BASE, fetchHealth, type CheckStatus, type HealthResponse } from '@/api/client'

const health = ref<HealthResponse | null>(null)
const error = ref<string | null>(null)
const loading = ref(true)
const elapsedMs = ref(0)

/** 依赖检查按固定顺序展示，不按后端的返回顺序 —— 位置稳定才扫得快。 */
const DEP_ORDER = ['postgres', 'redis']
const DEP_LABEL: Record<string, string> = {
  postgres: 'PostgreSQL',
  redis: 'Redis',
}

const deps = computed(() => {
  const checks = health.value?.checks ?? {}
  return Object.values(checks).sort(
    (a, b) => DEP_ORDER.indexOf(a.name) - DEP_ORDER.indexOf(b.name),
  )
})

/** 三态各自的样式。`skipped` 用中性灰，不能是红 —— 它表示「本就不需要」。 */
const TAG_TYPE: Record<CheckStatus, 'success' | 'danger' | 'info'> = {
  ok: 'success',
  down: 'danger',
  skipped: 'info',
}
const STATUS_TEXT: Record<CheckStatus, string> = {
  ok: '正常',
  down: '不可达',
  skipped: '不适用',
}

const PIPELINE = [
  { name: 'ingest', desc: '解析 diff、算出变更行集合' },
  { name: 'plan', desc: '文件风险排序 + BM25 检索规则' },
  { name: 'dispatch', desc: '写 review_tasks，按类型分派' },
  { name: 'wait', desc: 'interrupt() 暂停图，等屏障闭合' },
  { name: 'aggregate', desc: '去重 / 消解冲突 / 重算置信度' },
  { name: 'finalize', desc: '生成报告正文' },
  { name: 'publish', desc: '回写 PR 评论' },
]

const WORKERS = [
  { type: 'security', label: '安全', color: 'var(--sfly-security)', items: 'SQLi · XSS · 密钥泄露 · 越权 · 反序列化' },
  { type: 'performance', label: '性能', color: 'var(--sfly-performance)', items: 'N+1 · 无界查询 · 阻塞 IO · 二次复杂度' },
  { type: 'style', label: '风格', color: 'var(--sfly-style)', items: '命名 · 死代码 · 可读性 · 重复代码' },
]

const MILESTONES = [
  { id: 'Step 0', label: '文档 · 契约 · 骨架 · compose', done: true },
  { id: 'M0', label: 'workspace + 依赖连通 + /api/health', done: true },
  { id: 'M1', label: 'diff 解析 + Mock LLM + 修复阶梯 + 独立 CLI', done: true },
  { id: 'M2', label: '队列协议 + 内存实现 + 指纹', done: false },
  { id: 'M3', label: 'Redis Streams（回收 / 死信 / 重试）', done: false },
  { id: 'M4', label: 'Postgres schema + 幂等 migrate', done: false },
  { id: 'M5', label: 'LangGraph 图（含 interrupt 断点恢复）', done: false },
  { id: 'M6', label: 'FastAPI + SSE（Last-Event-ID 补齐）', done: false },
  { id: 'M7', label: 'GitHub 客户端 + publish 节点', done: false },
  { id: 'M8', label: 'Vue 仪表盘', done: false },
  { id: 'M9', label: '聚合硬化 + 评测集', done: false },
  { id: 'M10', label: '精简模式 + Render / Vercel 部署', done: false },
]

async function load() {
  loading.value = true
  error.value = null
  const t0 = performance.now()
  try {
    health.value = await fetchHealth()
  } catch (e) {
    // 冷启动场景下这个失败是正常的。把 API_BASE 打进错误信息里，
    // 否则看到 "Network Error" 根本不知道它在连哪儿。
    error.value = `无法连接后端（${API_BASE}）：${e instanceof Error ? e.message : String(e)}`
  } finally {
    elapsedMs.value = Math.round(performance.now() - t0)
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="page">
    <header class="hero">
      <div class="hero-main">
        <h1>sfly</h1>
        <p class="tagline">
          基于多 Agent 的分布式代码审查系统 —— 分布式执行，集中式决策
        </p>
      </div>
      <el-tag v-if="health" :type="health.mode === 'lite' ? 'warning' : 'success'" size="large" effect="dark">
        {{ health.mode === 'lite' ? '精简模式（单容器）' : '完整模式（7 容器）' }}
      </el-tag>
    </header>

    <!-- 连通性 -->
    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      show-icon
      title="后端未连通"
      :description="error"
    >
      <template #default>
        <p style="margin: 4px 0 0">{{ error }}</p>
        <p style="margin: 4px 0 0; color: var(--sfly-text-dim)">
          先执行 <code>python tasks.py up</code>，等健康检查通过后重试。
          Render 免费版冷启动需要约 60 秒。
        </p>
      </template>
    </el-alert>

    <el-card v-else-if="loading" shadow="never">
      <el-skeleton :rows="3" animated />
    </el-card>

    <template v-else-if="health">
      <el-row :gutter="16">
        <el-col :xs="24" :sm="12" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">状态</div>
            <div class="stat-value" :class="health.ok ? 'ok' : 'bad'">
              {{ health.ok ? '● 可用' : '● 依赖异常' }}
            </div>
            <div class="stat-sub">后端响应 {{ elapsedMs }} ms</div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">部署形态</div>
            <div class="stat-value mono">{{ health.mode }}</div>
            <div class="stat-sub">
              {{ health.mode === 'full' ? '7 容器 · 可水平扩展' : '单容器 · 不可扩展' }}
            </div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">LLM</div>
            <div class="stat-value mono">{{ health.config.llm_provider }}</div>
            <div class="stat-sub">mock 模式不产生费用</div>
          </el-card>
        </el-col>
        <el-col :xs="24" :sm="12" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">等待策略</div>
            <div class="stat-value mono">{{ health.config.wait_strategy }}</div>
            <div class="stat-sub">
              {{ health.config.wait_strategy === 'interrupt' ? '图暂停，可断点恢复' : '轮询兜底' }}
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 依赖 -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">依赖连通性</span>
          <span class="section-hint">后端每次请求都实时探测，不是缓存的启动结果</span>
        </template>
        <div class="deps">
          <div v-for="d in deps" :key="d.name" class="dep">
            <span class="dep-name">{{ DEP_LABEL[d.name] ?? d.name }}</span>
            <el-tag :type="TAG_TYPE[d.status]" size="small" effect="dark">
              {{ STATUS_TEXT[d.status] }}
            </el-tag>
            <span class="dep-detail mono">{{ d.detail }}</span>
            <span v-if="d.status === 'ok'" class="dep-latency">{{ d.latency_ms }} ms</span>
          </div>
        </div>
      </el-card>

      <!-- 流水线 -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">审查流水线</span>
          <span class="section-hint">LangGraph 状态机 · 每个 superstep 落一个 checkpoint</span>
        </template>
        <div class="pipeline">
          <template v-for="(node, i) in PIPELINE" :key="node.name">
            <div class="node">
              <code>{{ node.name }}</code>
              <span>{{ node.desc }}</span>
            </div>
            <div v-if="i < PIPELINE.length - 1" class="arrow">→</div>
          </template>
        </div>
      </el-card>

      <!-- Worker -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">专业 Worker</span>
          <span class="section-hint">一套代码三种部署 · 可 --scale 水平扩展</span>
        </template>
        <div class="workers">
          <div v-for="w in WORKERS" :key="w.type" class="worker" :style="{ '--wc': w.color }">
            <div class="worker-head">
              <span class="dot" />
              <strong>{{ w.label }}</strong>
              <code class="worker-type">{{ w.type }}</code>
            </div>
            <p class="worker-items">{{ w.items }}</p>
          </div>
        </div>
      </el-card>

      <!-- 进度 -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">实施进度</span>
          <span class="section-hint">每一步都以可运行、可验证、可提交的状态结束</span>
        </template>
        <div class="milestones">
          <div
            v-for="m in MILESTONES"
            :key="m.id"
            class="milestone"
            :class="{ done: m.done }"
          >
            <span class="ms-id">{{ m.id }}</span>
            <span class="ms-label">{{ m.label }}</span>
          </div>
        </div>
      </el-card>
    </template>

    <footer class="footer">
      <span>sfly v{{ health?.version ?? '0.1.0' }}</span>
      <span>·</span>
      <span class="mono">uptime {{ health?.uptime_s ?? 0 }}s</span>
      <span>·</span>
      <a :href="`${API_BASE}/docs`" target="_blank" rel="noopener">API 文档</a>
    </footer>
  </div>
</template>

<style scoped>
.page {
  max-width: 1100px;
  margin: 0 auto;
  padding: 32px 20px 64px;
}

.hero {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 24px;
  flex-wrap: wrap;
}

.hero h1 {
  margin: 0;
  font-size: 30px;
  letter-spacing: -0.02em;
}

.tagline {
  margin: 4px 0 0;
  color: var(--sfly-text-dim);
}

.stat {
  text-align: left;
}
.stat-label {
  color: var(--sfly-text-dim);
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
}
.stat-value {
  font-size: 20px;
  font-weight: 600;
  margin: 6px 0 2px;
}
.stat-value.ok {
  color: #16a34a;
}
.stat-value.bad {
  color: #dc2626;
}
.stat-sub {
  color: var(--sfly-text-dim);
  font-size: 12px;
}

/* 依赖 */
.deps {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.dep {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13px;
}
.dep-name {
  flex: 0 0 96px;
  font-weight: 500;
}
.dep-detail {
  flex: 1 1 auto;
  color: var(--sfly-text-dim);
  font-size: 12px;
  word-break: break-all;
}
.dep-latency {
  flex: 0 0 auto;
  color: var(--sfly-text-dim);
  font-size: 12px;
}

.section {
  margin-top: 16px;
}
.section-title {
  font-weight: 600;
}
.section-hint {
  margin-left: 10px;
  color: var(--sfly-text-dim);
  font-size: 12px;
}

/* 流水线 */
.pipeline {
  display: flex;
  flex-wrap: wrap;
  align-items: stretch;
  gap: 6px;
}
.node {
  flex: 1 1 120px;
  min-width: 110px;
  padding: 10px 12px;
  background: var(--sfly-bg);
  border: 1px solid var(--sfly-border);
  border-radius: 8px;
}
.node code {
  display: block;
  font-weight: 600;
  color: #1d4ed8;
}
.node span {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--sfly-text-dim);
  line-height: 1.4;
}
.arrow {
  display: flex;
  align-items: center;
  color: var(--sfly-border);
  font-size: 18px;
}

/* Worker */
.workers {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
  gap: 12px;
}
.worker {
  padding: 14px 16px;
  border-radius: 10px;
  border: 1px solid var(--sfly-border);
  border-left: 3px solid var(--wc);
  background: var(--sfly-surface);
}
.worker-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--wc);
}
.worker-type {
  margin-left: auto;
  color: var(--sfly-text-dim);
  font-size: 12px;
}
.worker-items {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--sfly-text-dim);
}

/* 进度 */
.milestones {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 6px 16px;
}
.milestone {
  display: flex;
  gap: 10px;
  padding: 5px 0;
  font-size: 13px;
  color: var(--sfly-text-dim);
}
.milestone.done {
  color: var(--sfly-text);
  font-weight: 500;
}
.ms-id {
  flex: 0 0 52px;
  font-family: var(--sfly-mono);
  font-size: 12px;
  color: var(--sfly-text-dim);
}
.milestone.done .ms-id {
  color: #16a34a;
}

.footer {
  margin-top: 32px;
  padding-top: 16px;
  border-top: 1px solid var(--sfly-border);
  display: flex;
  gap: 8px;
  color: var(--sfly-text-dim);
  font-size: 12px;
}
.footer a {
  color: #2563eb;
}
</style>
