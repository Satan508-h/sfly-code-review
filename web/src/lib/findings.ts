/**
 * 发现的排序、分组与统计 —— 全是纯函数。
 *
 * **为什么按文件分组是显示层的事，而不是后端的事。** 后端给的是一个扁平数组，
 * 顺序是聚合算法认为重要的顺序；而人读报告的方式是「这个文件改了什么、
 * 有什么问题」。两个顺序都对，只是服务的对象不同 —— 所以分组放在前端做，
 * 后端不用为了 UI 多返回一个嵌套结构（那会让「加一个只在界面里用的字段」
 * 变成跨服务的契约变更）。
 *
 * ### 一条要写清楚的现状：分组不等于聚类
 *
 * 这里的分组是**纯显示**的「同一个文件」，与后端的「并查集聚类」是两回事。
 * 聚类（同路径 + |Δline| ≤ 3 + 相似度阈值，合并跨 Worker 的重复发现）是
 * **M9 的活，目前还没实现** —— 所以现在 `sources` 恒为单个 Worker、
 * `corroboration_count` 恒为 1、`conflicts` 恒为空数组。
 *
 * 界面因此**不能**显示「跨 Worker 印证 3 个」这类统计：那会把「还没算」
 * 显示成「算出来是 1」，而这两件事在界面上长得一模一样。
 */

import type { AggregatedFinding, Severity } from '@/api/client'

import { SEVERITY_ORDER, severityRank } from './format'

/** 一个文件下的发现。 */
export interface FileGroup {
  file: string
  findings: AggregatedFinding[]
  /** 该文件里最严重的等级，用来给分组标题上色。空组不存在（不会生成）。 */
  worst: Severity
}

/**
 * 排序：严重度从重到轻 → 文件路径 → 行号。
 *
 * 同严重度时按**文件路径**而不是按置信度排：报告是拿来照着改代码的，
 * 同一文件的问题挨在一起比「最高的那条散落在中间」好用得多。
 */
export function sortFindings(list: readonly AggregatedFinding[]): AggregatedFinding[] {
  return [...list].sort((a, b) => {
    const bySeverity = severityRank(a.severity) - severityRank(b.severity)
    if (bySeverity !== 0) return bySeverity
    if (a.file !== b.file) return a.file.localeCompare(b.file)
    return a.line - b.line
  })
}

/**
 * 按文件分组。组的顺序 = 组内最严重的那条决定的顺序。
 *
 * 不用 `Object.groupBy` / `Map` 后直接 `Object.values`：那两种写法的组顺序
 * 取决于插入顺序或键的字典序，而**字典序的组顺序会让最危险的文件排到最后**。
 */
export function groupByFile(list: readonly AggregatedFinding[]): FileGroup[] {
  const sorted = sortFindings(list)
  const groups = new Map<string, AggregatedFinding[]>()
  for (const f of sorted) {
    const bucket = groups.get(f.file)
    if (bucket) bucket.push(f)
    else groups.set(f.file, [f])
  }
  return [...groups.entries()].map(([file, findings]) => ({
    file,
    findings,
    // 组内已按严重度降序排过，第一条就是最重的
    worst: findings[0]?.severity ?? 'info',
  }))
}

/** 各严重度的条数，**固定顺序**（从重到轻）—— 返回值直接拿去做分布条，不需要再排。 */
export function severityCounts(
  list: readonly AggregatedFinding[],
): { severity: Severity; count: number }[] {
  return SEVERITY_ORDER.map((severity) => ({
    severity,
    count: list.filter((f) => f.severity === severity).length,
  }))
}

/**
 * 置信度条要显示的两个数。
 *
 * `raw` 是模型自报的（系统性偏高、跨 Worker 不可比），`adjusted` 是主 Agent
 * 用确定性公式重算的 —— **界面必须以后者为准**，但把前者一并显示出来，
 * 因为「0.90 被重算成 0.60」这件事本身就是这个项目最值得讲的一处设计。
 *
 * 两者差值小于 0.02 时不显示原始值：那是显示噪音，不是信息。
 */
export function confidencePair(f: AggregatedFinding): { adjusted: number; raw: number | null } {
  const adjusted = f.adjusted_confidence
  const raw = f.confidence
  return { adjusted, raw: Math.abs(raw - adjusted) >= 0.02 ? raw : null }
}

/**
 * `decision_reason` → 人读说明。
 *
 * **这是 `aggregate/decision.py` 里 `REASON_TEXT` 的一份镜像，两处必须一起改。**
 * 为什么不干脆让后端把这句话塞进报告里：那需要动 `ReviewReport` 契约
 * （加字段 = 跨五个 app 的契约变更），而这只是展示层的一句文案。
 *
 * 查不到时**回显 slug 本身**，和后端 `decision_text()` 的策略一致 ——
 * 回显至少能让人去 grep，而「未知原因」只能让人来问作者。
 */
const REASON_TEXT: Record<string, string> = {
  secrets_found: '发现凭据泄露 —— 这类问题一旦合入就可能已经不可逆（密钥需要轮换）',
  blocking_critical: '发现高危类目的严重问题，且置信度足够高',
  too_many_high: '高危及以上问题的数量达到阈值',
  below_threshold: '没有达到阻断门槛，仅供作者参考',
}

export function decisionText(reason: string): string {
  return REASON_TEXT[reason] ?? reason
}

/** 命中 RAG 规则时置信度会加 0.10 —— 界面上值得说明这一条的来历。 */
export function ruleHint(ruleId: string | null): string | null {
  return ruleId ? `命中规则库 ${ruleId}（置信度 +0.10）` : null
}
