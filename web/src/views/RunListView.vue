<script setup lang="ts">
/**
 * 运行记录列表 —— 应用的门面。
 *
 * 面试官点开链接第一眼看到的就是它，所以这一页要回答的问题很具体：
 * **「这东西真的跑过吗、跑出来什么了」**。
 *
 * 三处刻意的设计：
 *
 * * **筛选在客户端做。** 后端 `/api/runs` 只有 `limit`/`offset`，没有过滤参数 ——
 *   这里也不打算加：一次取 50 条（ULID 主键倒序，就是「最近 50 次」），
 *   在浏览器里过滤是零成本的。给一个只会被用来筛这 50 条的接口加参数，
 *   换来的是后端多一个要测的分支。**什么时候该翻案**：等列表默认要显示
 *   几百条、或者要看「上个月的失败率」时，那才该是服务端的查询。
 * * **空状态给出命令而不是一句「暂无数据」。** 第一次跑起来的人看到空表格，
 *   最需要知道的就是「怎么让它有东西」——把那行命令印在页面上。
 * * **整行可点。** 表格里的链接（PR、评论）要 `@click.stop`，否则点它会先跳详情页。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { prUrl, type RunRow, type RunStatus } from '@/api/client'
import {
  fmtCost,
  fmtDuration,
  fmtRelative,
  isTerminal,
  RUN_STATUS_LABEL,
  RUN_STATUS_TYPE,
  shortSha,
  WORKER_LABEL,
  WORKER_VAR,
} from '@/lib/format'
import { useRunsStore } from '@/stores/runs'

const store = useRunsStore()
const router = useRouter()

/** 状态筛选。`active` 是「还没跑完」，不是某个具体状态 —— 演示时最常用的一档。 */
type Filter = 'all' | 'active' | 'published' | 'failed'
const filter = ref<Filter>('all')
const keyword = ref('')

const FILTERS: { value: Filter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '进行中' },
  { value: 'published', label: '已发布' },
  { value: 'failed', label: '异常' },
]

function matchStatus(run: RunRow, f: Filter): boolean {
  if (f === 'all') return true
  if (f === 'active') return !isTerminal(run.status)
  if (f === 'failed') return run.status === 'failed' || run.status === 'publish_failed'
  return run.status === 'published'
}

const rows = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return store.runs.filter((run) => {
    if (!matchStatus(run, filter.value)) return false
    if (!kw) return true
    return (
      run.repo_id.toLowerCase().includes(kw) ||
      String(run.pr_number).includes(kw) ||
      run.head_sha.toLowerCase().startsWith(kw)
    )
  })
})

/** 每一档的条数，直接标在按钮上 —— 省得切过去才发现是空的。 */
const counts = computed<Record<Filter, number>>(() => ({
  all: store.runs.length,
  active: store.runs.filter((r) => matchStatus(r, 'active')).length,
  published: store.runs.filter((r) => matchStatus(r, 'published')).length,
  failed: store.runs.filter((r) => matchStatus(r, 'failed')).length,
}))

function open(run: RunRow): void {
  void router.push({ name: 'run-detail', params: { taskId: run.task_id } })
}

function statusText(status: RunStatus): string {
  return RUN_STATUS_LABEL[status]
}

onMounted(() => store.start())
// 不清掉的话切走页面后轮询还在跑 —— 开发时表现为「改一行代码，请求数翻倍」
onUnmounted(() => store.stop())
</script>

<template>
  <div class="page">
    <header class="head">
      <div>
        <h2>运行记录</h2>
        <p class="hint">
          最近 {{ store.runs.length }} 次审查
          <template v-if="store.activeCount > 0">
            · <span class="live">{{ store.activeCount }} 个进行中</span>
          </template>
          <template v-if="store.loadedAt">
            · 每 5 秒自动刷新（{{ fmtRelative(new Date(store.loadedAt).toISOString()) }}）
          </template>
        </p>
      </div>
      <el-button :loading="store.loading" @click="store.load()">刷新</el-button>
    </header>

    <el-alert
      v-if="store.error"
      type="error"
      :closable="false"
      show-icon
      title="读不到运行记录"
      :description="store.error"
      class="mb"
    />

    <div class="toolbar">
      <el-radio-group v-model="filter" size="small">
        <el-radio-button v-for="f in FILTERS" :key="f.value" :value="f.value">
          {{ f.label }}<span v-if="counts[f.value]" class="n">{{ counts[f.value] }}</span>
        </el-radio-button>
      </el-radio-group>
      <el-input
        v-model="keyword"
        size="small"
        placeholder="按仓库 / PR 号 / 提交 SHA 过滤"
        clearable
        class="search"
      />
    </div>

    <!-- 首屏骨架屏；之后的自动刷新不会走这里（否则页面每 5 秒闪一次） -->
    <el-card v-if="store.loading && store.runs.length === 0" shadow="never">
      <el-skeleton :rows="6" animated />
    </el-card>

    <!-- 空状态：把「怎么让它有东西」印在页面上 -->
    <el-empty v-else-if="store.runs.length === 0 && !store.error" description="还没有审查记录">
      <div class="empty">
        <p>跑一次端到端演示，几秒后这里就会多出一行：</p>
        <pre class="cmd">python tasks.py demo</pre>
        <p class="dim">
          它会把 <code>fixtures/webhook_pr.json</code> 投递三次（1 个 run + 2 个重复），
          编排器接手后三个 Worker 并行审查，报告会回写到那个 PR 上。
        </p>
      </div>
    </el-empty>

    <el-empty v-else-if="rows.length === 0" description="当前筛选条件下没有记录" />

    <el-table
      v-else
      :data="rows"
      class="table"
      size="small"
      row-key="task_id"
      @row-click="open"
    >
      <el-table-column label="状态" width="112">
        <template #default="{ row }">
          <el-tag :type="RUN_STATUS_TYPE[(row as RunRow).status]" size="small" effect="light">
            {{ statusText((row as RunRow).status) }}
          </el-tag>
        </template>
      </el-table-column>

      <el-table-column label="仓库 / PR" min-width="230">
        <template #default="{ row }">
          <a
            class="repo"
            :href="prUrl(row as RunRow)"
            target="_blank"
            rel="noopener"
            @click.stop
          >
            {{ (row as RunRow).repo_id }} <span class="pr">#{{ (row as RunRow).pr_number }}</span>
          </a>
          <div class="sub">
            提交 <code>{{ shortSha((row as RunRow).head_sha) }}</code>
            <template v-if="(row as RunRow).attempt > 1">
              · 第 {{ (row as RunRow).attempt }} 次
            </template>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="文件" width="86">
        <template #default="{ row }">
          <span class="mono">
            {{ (row as RunRow).files_reviewed }}/{{ (row as RunRow).files_total }}
          </span>
          <!-- 大 PR 会被裁到前 N 个文件。这件事必须显示出来，否则「只审了 40 个」
               会被读成「只改了 40 个」 -->
          <el-tooltip v-if="(row as RunRow).diff_truncated" content="diff 过大，只审了风险最高的前几个文件">
            <span class="warn">裁剪</span>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="Worker" width="150">
        <template #default="{ row }">
          <span class="workers">
            <el-tooltip
              v-for="w in (row as RunRow).planned_workers"
              :key="w"
              :content="`${WORKER_LABEL[w]}：${(row as RunRow).missing_workers.includes(w) ? '超时未上报' : '已上报'}`"
            >
              <span
                class="wdot"
                :style="{ background: `var(${WORKER_VAR[w]})` }"
                :class="{ missing: (row as RunRow).missing_workers.includes(w) }"
              />
            </el-tooltip>
            <!-- 降级徽章：run 跑完了，但有 Worker 没交上东西。M5 的演示素材 -->
            <el-tag v-if="(row as RunRow).degraded" type="warning" size="small" effect="plain">
              降级
            </el-tag>
          </span>
        </template>
      </el-table-column>

      <el-table-column label="耗时" width="96">
        <template #default="{ row }">
          <span class="mono">{{ fmtDuration((row as RunRow).totals?.duration_ms) }}</span>
        </template>
      </el-table-column>

      <el-table-column label="成本" width="88">
        <template #default="{ row }">
          <!-- 评估的是**整个 run 的花费**，不是某一次调用的 -->
          <el-tooltip
            :content="`输入 ${(row as RunRow).totals?.tokens_in ?? 0} / 输出 ${(row as RunRow).totals?.tokens_out ?? 0} tokens`"
          >
            <span class="mono">{{ fmtCost((row as RunRow).totals?.cost_usd) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="时间" width="110">
        <template #default="{ row }">
          <el-tooltip :content="(row as RunRow).created_at">
            <span class="dim">{{ fmtRelative((row as RunRow).created_at) }}</span>
          </el-tooltip>
        </template>
      </el-table-column>

      <el-table-column label="评论" width="76">
        <template #default="{ row }">
          <a
            v-if="(row as RunRow).github_comment_id"
            class="comment"
            :href="`${prUrl(row as RunRow)}#pullrequestreview-${(row as RunRow).github_comment_id}`"
            target="_blank"
            rel="noopener"
            @click.stop
          >
            查看
          </a>
          <span v-else class="dim">—</span>
        </template>
      </el-table-column>
    </el-table>
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
.live {
  color: #d97706;
  font-weight: 600;
}
.mb {
  margin-bottom: 12px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.search {
  max-width: 260px;
}
.n {
  margin-left: 5px;
  opacity: 0.65;
  font-size: 11px;
}

.table {
  border: 1px solid var(--sfly-border);
  border-radius: 8px;
}
.table :deep(.el-table__row) {
  cursor: pointer;
}
.repo {
  color: #1d4ed8;
  text-decoration: none;
  font-size: 13px;
}
.repo:hover {
  text-decoration: underline;
}
.pr {
  color: var(--sfly-text-dim);
}
.sub {
  color: var(--sfly-text-dim);
  font-size: 12px;
  margin-top: 2px;
}
.warn {
  margin-left: 6px;
  color: #d97706;
  font-size: 11px;
  cursor: help;
}
.workers {
  display: flex;
  align-items: center;
  gap: 5px;
}
.wdot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}
/* 空心 = 这个 Worker 没上报。和空状态一样，形状变化比颜色变化更不依赖色觉 */
.wdot.missing {
  background: transparent !important;
  border: 2px solid var(--sfly-border);
}
.comment {
  color: #1d4ed8;
  font-size: 12px;
}
.dim {
  color: var(--sfly-text-dim);
  font-size: 12px;
}

.empty {
  max-width: 520px;
  margin: 0 auto;
  text-align: left;
}
.empty p {
  margin: 6px 0;
  font-size: 13px;
}
.cmd {
  margin: 10px 0;
  padding: 10px 14px;
  background: #0f172a;
  color: #e2e8f0;
  border-radius: 6px;
  font-family: var(--sfly-mono);
  font-size: 13px;
  overflow-x: auto;
}
</style>
