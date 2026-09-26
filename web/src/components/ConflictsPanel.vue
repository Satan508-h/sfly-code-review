<script setup lang="ts">
/**
 * 冲突面板：同一个位置被两个 Worker 给出不同程度结论时的裁决记录。
 *
 * ### 现在它必然是空的，而且必须说清楚为什么
 *
 * 冲突消解属于**聚类**的一部分，而聚类是 **M9 还没做的部分**
 * （见 `agent-core/sfly_agent/aggregate/__init__.py` 里那行 `cluster ...（M9）`）。
 * 所以 `report.conflicts` 目前恒为空数组 —— **不是因为「这次没冲突」**。
 *
 * 这两件事在界面上长得一模一样，而含义正好相反：一个是「算过，没有」，
 * 一个是「还没算」。空表格会让人读成前者。所以这个组件在空的时候**必须
 * 主动解释**，而不是交给调用方去摆一个 `el-empty`。
 *
 * 同理，`FindingItem` 上那个「跨 Worker 印证」的圆点组现在也只会有一个点 ——
 * 那份数据同样来自聚类。
 */
import type { ConflictRecord } from '@/api/client'
import { SEVERITY_LABEL, WORKER_LABEL, WORKER_VAR } from '@/lib/format'

defineProps<{ conflicts: ConflictRecord[] }>()

/**
 * 四条规则的说明。**slug 和文案都是从 `aggregate/decision.py` 那边抄过来的**
 * —— 那边是真相来源，改了这里要跟着改（同 `lib/findings.ts` 里那张表）。
 */
const RULE_TEXT: Record<string, string> = {
  category_authority: '职责域优先 —— 高危项属于该 Worker 的职责范围，直接胜出',
  out_of_lane_downgrade: '越界降级 —— 报了不属于自己职责域的高危项，降一级',
  evidence_adjudication: '证据裁决 —— 证据能在 diff 里逐字匹配到的一方胜出',
  unresolved: '未决 —— 两条都站得住，取较低的置信度并标记需人工复核',
}
</script>

<template>
  <div class="conflicts">
    <div v-if="conflicts.length === 0" class="empty">
      <el-result icon="info" title="这次没有冲突记录">
        <template #sub-title>
          <p class="dim">
            <strong>这句话现在还不能当成结论。</strong>
            冲突消解依赖并查集聚类，而聚类是 M9 尚未实现的部分 ——
            <code>report.conflicts</code> 目前恒为空数组，它表示「还没算」， 不是「算过了、没有」。
          </p>
          <p class="dim">
            同样受影响的还有每条发现右下角那组 Worker 圆点：跨 Worker 印证同样来自聚类，
            所以现在每条发现的来源都只有一个 Worker。
          </p>
          <p class="dim">
            已经就位的是**规则引擎本身**：四条规则（职责域优先 / 越界降级 / 证据裁决 / 未决）和
            <code>ConflictRecord</code> 契约都在，M9 补上聚类之后这一页 会直接开始有内容。
          </p>
        </template>
      </el-result>
    </div>

    <template v-else>
      <p class="lead">
        同一个位置（路径相同、行号相差不超过 3 行）被两个 Worker 给出严重度差 ≥ 2
        的结论时，由确定性规则引擎裁决 —— **不用 LLM 裁判**：那会引入非确定性，
        直接毁掉评测的可复现性。
      </p>
      <div v-for="(c, i) in conflicts" :key="`${c.file}:${c.line}:${i}`" class="case">
        <div class="case-head">
          <code class="where">{{ c.file }}:{{ c.line }}</code>
          <span class="rule">{{ c.resolution_rule }}</span>
          <span v-if="c.resolution_rule === 'unresolved'" class="warn">需人工复核</span>
        </div>
        <div class="duel">
          <div class="side win">
            <span class="tag">胜出</span>
            <span class="wdot" :style="{ background: `var(${WORKER_VAR[c.winner_worker]})` }" />
            <span class="who">{{ WORKER_LABEL[c.winner_worker] }}</span>
            <span class="sev">{{ SEVERITY_LABEL[c.winner_severity] }}</span>
          </div>
          <div class="vs">vs</div>
          <div class="side lose">
            <span class="tag">让位</span>
            <span class="wdot" :style="{ background: `var(${WORKER_VAR[c.loser_worker]})` }" />
            <span class="who">{{ WORKER_LABEL[c.loser_worker] }}</span>
            <span class="sev">{{ SEVERITY_LABEL[c.loser_severity] }}</span>
          </div>
        </div>
        <p class="why">{{ RULE_TEXT[c.resolution_rule] ?? c.resolution_rule }}</p>
        <p v-if="c.rationale" class="rationale">{{ c.rationale }}</p>
      </div>
    </template>
  </div>
</template>

<style scoped>
.lead {
  margin: 0 0 12px;
  font-size: 13px;
  color: var(--sfly-text-dim);
  line-height: 1.6;
}
.empty {
  padding: 8px 0;
}
.dim {
  color: var(--sfly-text-dim);
  font-size: 12.5px;
  line-height: 1.7;
  text-align: left;
  margin: 6px 0;
}

.case {
  border: 1px solid var(--sfly-border);
  border-left: 3px solid #7c3aed;
  border-radius: 8px;
  padding: 12px 14px;
  margin-bottom: 10px;
  background: var(--sfly-surface);
}
.case-head {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.where {
  font-weight: 600;
}
.rule {
  font-family: var(--sfly-mono);
  color: #7c3aed;
}
.warn {
  margin-left: auto;
  color: #b45309;
}

.duel {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 10px 0 8px;
  flex-wrap: wrap;
}
.side {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 13px;
  background: var(--sfly-bg);
}
.side.lose {
  opacity: 0.55;
  text-decoration: line-through;
  text-decoration-color: var(--sfly-border);
}
.tag {
  font-size: 11px;
  color: var(--sfly-text-dim);
}
.wdot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.who {
  font-weight: 600;
}
.sev {
  color: var(--sfly-text-dim);
}
.vs {
  color: var(--sfly-text-dim);
  font-size: 11px;
}

.why {
  margin: 0;
  font-size: 12.5px;
  color: var(--sfly-text);
}
.rationale {
  margin: 6px 0 0;
  font-size: 12px;
  color: var(--sfly-text-dim);
}
</style>
