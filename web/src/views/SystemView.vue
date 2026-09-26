<script setup lang="ts">
/**
 * 系统状态页 —— 原「首页状态页」的归宿。
 *
 * 它回答的是面试官看完运行记录之后的第二个问题：**「这东西是怎么搭的」**。
 *
 * 这一版相比 M0 那张状态页有一处**刻意的删减**：里程碑清单没了。
 * 那张清单在开发过程中是好东西（进度看得见），但它有个只会朝坏的方向走的
 * 性质 —— 每完成一个里程碑它就需要有人去改一次，而忘了改的结果是页面在
 * 撒谎。现在进度归 README 和 CLAUDE.md，页面上只留**不会过期**的东西：
 * 依赖连通性、两种拓扑的对照、三个 Worker 的职责、以及后端配置的实时回显。
 *
 * 配置回显（`queue_backend` / `llm_provider` / `wait_strategy`）是这里最值钱
 * 的一格：它让「同一套代码跑在哪种拓扑上」变成一眼可见，而不是需要读 `.env`。
 */
import { computed, onMounted } from 'vue'

import { API_BASE, type CheckStatus } from '@/api/client'
import { useHealthStore } from '@/stores/health'

const store = useHealthStore()

/** 依赖检查按固定顺序展示，不按后端的返回顺序 —— 位置稳定才扫得快。 */
const DEP_ORDER = ['postgres', 'redis']
const DEP_LABEL: Record<string, string> = {
  postgres: 'PostgreSQL',
  redis: 'Redis',
}

const deps = computed(() => {
  const checks = store.health?.checks ?? {}
  return Object.values(checks).sort((a, b) => DEP_ORDER.indexOf(a.name) - DEP_ORDER.indexOf(b.name))
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
  { name: 'ingest', desc: '解析载荷、算出变更行集合' },
  { name: 'plan', desc: '文件风险排序 + BM25 检索规则' },
  { name: 'dispatch', desc: '写 review_tasks，按类型分派' },
  { name: 'wait', desc: 'interrupt() 暂停图，等屏障闭合' },
  { name: 'aggregate', desc: '去重 / 消解冲突 / 重算置信度' },
  { name: 'finalize', desc: '生成报告正文' },
  { name: 'publish', desc: '回写 PR 评论' },
]

const WORKERS = [
  {
    type: 'security',
    label: '安全',
    color: 'var(--sfly-security)',
    items: 'SQLi · XSS · 密钥泄露 · 越权 · 反序列化',
  },
  {
    type: 'performance',
    label: '性能',
    color: 'var(--sfly-performance)',
    items: 'N+1 · 无界查询 · 阻塞 IO · 二次复杂度',
  },
  {
    type: 'style',
    label: '风格',
    color: 'var(--sfly-style)',
    items: '命名 · 死代码 · 可读性 · 重复代码',
  },
]

/**
 * 两种拓扑的对照。整个项目的核心卖点，放在这里让人一眼看到差异。
 *
 * 写成对象数组而不是 `string[][]`：`el-table-column` 的 `prop` 只认属性名，
 * 写成 `prop="0"` 不会报错 —— 它只是**显示空白**。
 */
const TOPOLOGY = [
  { dim: '启动方式', full: 'docker compose up', lite: 'Render 单容器' },
  {
    dim: '容器数',
    full: '7+（api / orchestrator / worker×3 / redis / postgres / web）',
    lite: '1',
  },
  {
    dim: '队列',
    full: 'RedisStreamsQueue（消费者组、XAUTOCLAIM、死信）',
    lite: 'InMemoryQueue（asyncio 队列）',
  },
  { dim: '锁', full: 'RedisLock（SET NX PX + Lua 释放）', lite: 'InMemoryLock（asyncio.Lock）' },
  { dim: '存储', full: '本地 Postgres 容器', lite: 'Neon Postgres' },
  { dim: '前端', full: 'nginx 托管，同源代理 /api', lite: 'Vercel 独立部署，跨域 + CORS' },
  {
    dim: '水平扩展',
    full: 'docker compose up --scale worker-security=3',
    lite: '不支持（单进程 asyncio 并发）',
  },
]

onMounted(() => {
  // 从别的页面切过来时重新探一次 —— 「切回来看一眼」正是这个页面存在的意义，
  // 显示一个五分钟前的旧结论等于没说。
  if (store.checkedAt === null) void store.load()
})
</script>

<template>
  <div class="page">
    <header class="head">
      <div>
        <h2>系统状态</h2>
        <p class="hint">依赖探测是**每次请求实时做的**，不是启动时缓存的结果</p>
      </div>
      <el-button :loading="store.loading" @click="store.load()">重新探测</el-button>
    </header>

    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      title="后端未连通"
      :description="store.error"
    >
      <template #default>
        <p style="margin: 4px 0 0">{{ store.error }}</p>
        <p style="margin: 4px 0 0; color: var(--sfly-text-dim)">
          先执行 <code>python tasks.py up</code>，等健康检查通过后重试。 Render 免费版冷启动需要约
          60 秒。
        </p>
      </template>
    </el-alert>

    <el-card v-else-if="store.loading && !store.health" shadow="never">
      <el-skeleton :rows="4" animated />
    </el-card>

    <template v-else-if="store.health">
      <el-row :gutter="12">
        <el-col :xs="12" :sm="8" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">状态</div>
            <div class="stat-value" :class="store.health.ok ? 'ok' : 'bad'">
              {{ store.health.ok ? '● 可用' : '● 依赖异常' }}
            </div>
            <div class="stat-sub">
              探测耗时 {{ deps.length ? Math.max(...deps.map((d) => d.latency_ms)) : 0 }} ms
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">部署形态</div>
            <div class="stat-value mono">{{ store.health.mode }}</div>
            <div class="stat-sub">
              {{ store.health.mode === 'full' ? '7 容器 · 可水平扩展' : '单容器 · 不可扩展' }}
            </div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">LLM</div>
            <div class="stat-value mono">{{ store.health.config.llm_provider }}</div>
            <div class="stat-sub">mock 模式不产生费用</div>
          </el-card>
        </el-col>
        <el-col :xs="12" :sm="8" :md="6">
          <el-card shadow="never" class="stat">
            <div class="stat-label">等待策略</div>
            <div class="stat-value mono">{{ store.health.config.wait_strategy }}</div>
            <div class="stat-sub">
              {{
                store.health.config.wait_strategy === 'interrupt'
                  ? '图暂停，可断点恢复'
                  : '轮询兜底'
              }}
            </div>
          </el-card>
        </el-col>
      </el-row>

      <!-- 依赖 -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">依赖连通性</span>
          <span class="section-hint">
            `/healthz` 探的是存活、刻意不探依赖；这里看到的是就绪状态
          </span>
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
          <div v-if="deps.length === 0" class="dim">当前模式下没有需要探测的依赖</div>
        </div>
        <el-alert
          v-if="store.health.config.webhook_secret === 'missing'"
          type="warning"
          :closable="false"
          show-icon
          class="mt"
          title="GITHUB_WEBHOOK_SECRET 未配置"
          description="webhook 入口目前不验签 —— 本地演示没问题，公网部署下必须配上（见 README 的已知限制）。"
        />
      </el-card>

      <!-- 两种拓扑 -->
      <el-card shadow="never" class="section">
        <template #header>
          <span class="section-title">两种拓扑</span>
          <span class="section-hint">
            同一套代码，由 QUEUE_BACKEND 选择 —— GraphRunner / WorkerPool / 全部节点都是同一批对象
          </span>
        </template>
        <el-table :data="TOPOLOGY" size="small" :show-header="false" border>
          <el-table-column prop="dim" label="维度" width="110" />
          <el-table-column label="完整模式" min-width="320">
            <template #default="{ row }">
              <span class="mono">{{ row.full }}</span>
            </template>
          </el-table-column>
          <el-table-column label="精简模式" min-width="260">
            <template #default="{ row }">
              <span class="mono">{{ row.lite }}</span>
            </template>
          </el-table-column>
        </el-table>
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

      <footer class="footer">
        <span>sfly v{{ store.health.version }}</span>
        <span class="sep">·</span>
        <span class="mono">uptime {{ Math.round(store.health.uptime_s) }}s</span>
        <span class="sep">·</span>
        <a :href="`${API_BASE}/docs`" target="_blank" rel="noopener">API 文档</a>
      </footer>
    </template>
  </div>
</template>

<style scoped>
.head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;
}
.head h2 {
  margin: 0;
  font-size: 19px;
}
.hint {
  margin: 4px 0 0;
  color: var(--sfly-text-dim);
  font-size: 12px;
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
  font-size: 18px;
  font-weight: 600;
  margin: 5px 0 2px;
}
.stat-value.ok {
  color: #16a34a;
}
.stat-value.bad {
  color: #dc2626;
}
.stat-sub {
  color: var(--sfly-text-dim);
  font-size: 11px;
}

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
.mt {
  margin-top: 12px;
}

.section {
  margin-top: 12px;
}
.section-title {
  font-weight: 600;
}
.section-hint {
  margin-left: 10px;
  color: var(--sfly-text-dim);
  font-size: 12px;
}

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

.footer {
  margin-top: 20px;
  padding-top: 14px;
  border-top: 1px solid var(--sfly-border);
  display: flex;
  gap: 8px;
  color: var(--sfly-text-dim);
  font-size: 12px;
}
.footer a {
  color: #2563eb;
}
.dim {
  color: var(--sfly-text-dim);
  font-size: 12px;
}
</style>
