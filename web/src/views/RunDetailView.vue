<script setup lang="ts">
/**
 * 单个 run 的详情。
 *
 * 这一页要在一屏里回答四个问题，顺序就是页面从上往下的顺序：
 *
 *   1. **这是在审什么？** —— 仓库、PR、提交、谁提的
 *   2. **审完了吗、出了问题吗？** —— 状态、降级徽章、阻断标记
 *   3. **审出了什么？** —— 按文件分组的发现（下一步接）
 *   4. **它到底怎么跑的？** —— 时间线（下一步接）
 *
 * ### 「run 不存在」要当成正常状态处理，不是错误
 *
 * 投递之后立刻打开详情页，可能拿到 **404** —— 因为 run 由编排器创建，
 * 而编排器要先消费到那条 bootstrap。正常在百毫秒级，但**这就是两个进程
 * 之间的真实延迟**，不是异常。所以这一页对 404 的处置是自动重试几次，
 * 而不是弹一个红色错误框。
 */
import { computed, onMounted, ref, watch } from 'vue'

import { fetchRun, prUrl, type RunDetailResponse } from '@/api/client'
import { fmtCost, fmtInt, fmtDuration, fmtTime, RUN_STATUS_LABEL, RUN_STATUS_TYPE, shortSha } from '@/lib/format'

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

async function load(auto = false): Promise<void> {
  if (!auto) loading.value = true
  try {
    data.value = await fetchRun(props.taskId)
    error.value = null
    notFound.value = false
    retries.value = 0
  } catch (e) {
    // axios 的错误对象里才有状态码；拿不到就按「其他错误」处理。
    const status = (e as { response?: { status?: number } }).response?.status
    if (status === 404) {
      notFound.value = true
      error.value = null
      // 刚投递完的窗口期：等编排器把 run 建出来。
      if (auto && retries.value < MAX_RETRY) {
        retries.value += 1
        setTimeout(() => void load(true), RETRY_MS)
        return
      }
    } else {
      error.value = e instanceof Error ? e.message : String(e)
    }
  } finally {
    loading.value = false
  }
}

onMounted(() => void load())
// 同一个组件复用于不同 run 时（从详情页跳到另一个 run），必须重新拉 ——
// 否则页面会显示上一个 run 的数据，而且**看不出哪里不对**。
watch(() => props.taskId, () => void load())
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
          <el-button @click="load()">再试一次</el-button>
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
        </div>

        <div class="bar-meta">
          <span>提交 <code>{{ shortSha(run.head_sha) }}</code></span>
          <span class="sep">·</span>
          <span>基线 <code>{{ shortSha(run.base_sha) }}</code></span>
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

      <!-- ── 报告与时间线（下一步接） ──────────────────────────────── -->
      <el-card shadow="never" class="placeholder">
        <el-empty description="报告与时间线在下一步接入">
          <p class="dim">
            这一步（M8-1）打通的是「列表 → 详情」的取数与错误处理。
            下一步接按文件分组的发现列表，再下一步接 SSE 实时时间线。
          </p>
        </el-empty>
      </el-card>
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

.placeholder {
  margin-top: 12px;
}
.taskid {
  color: var(--sfly-text-dim);
}
.dim {
  color: var(--sfly-text-dim);
  font-size: 12px;
}
</style>
