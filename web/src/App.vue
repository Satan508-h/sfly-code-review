<script setup lang="ts">
/**
 * 应用外壳：顶栏 + 路由出口。
 *
 * 它只做四件事，业务全在 views/ 里：
 *   1. 顶栏 —— 品牌、两个导航项、以及**当前部署形态的回显**
 *   2. `<el-config-provider>` —— Element Plus 的中文文案，见下面那段注释
 *   3. 后端还没答应时的**唤醒面板**（Render 免费档冷启动，见 stores/health.ts）
 *   4. 连不上时的全局提示条
 *
 * ### 顶栏那个徽章为什么值得留
 *
 * 它显示的是 `/api/health` 回显的 `queue_backend` / `llm_provider` / `mode`。
 * 排查「为什么本地是这样、线上是那样」时，第一句话就是「你现在连的是哪个
 * 后端、它用的是 redis 还是 memory 队列」—— 把它挂在每一页的右上角，
 * 比让人去翻 `.env` 快得多。截图放进 README 时它也顺带把「两种拓扑」
 * 这件事印在了每一张图上。
 *
 * ### 唤醒期间为什么不挂载路由
 *
 * 视图一挂载就会发自己的请求，而那些请求会在同一个冷启动窗口里**一起挂
 * 90 秒**（HTTP 超时是 90 秒），然后各自报一次错 —— 访客看到的是三四个红色
 * 的错误提示，而真相只有一个：容器还没醒。
 *
 * 让整棵视图树等后端答应之后再挂载，是同一件事只做一次。它顺带保证了
 * **「先 JSON 唤醒、再开 SSE」**这条顺序（详情页那条流是在挂载时开的，
 * 对着一个休眠的容器开流只会得到一个会自动重连的错误）。
 */
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { computed, onMounted, onUnmounted } from 'vue'
import { RouterLink, RouterView, useRoute } from 'vue-router'

import { useHealthStore } from '@/stores/health'

const healthStore = useHealthStore()
const route = useRoute()

/** 连不上，而且已经过了等待期（或者超过了等待上限）。 */
const down = computed(() => !healthStore.waiting && healthStore.error !== null)

onMounted(() => void healthStore.startWatch())
// **必须停**：那个循环会每 3 秒探一次，不停的话它会一直跑下去。
onUnmounted(() => healthStore.stopWatch())
</script>

<template>
  <!--
    locale 必须走 provider，不能写成 `app.use(ElementPlus, { locale })` ——
    那样在 vue-tsc 严格模式下会报 TS2769（ConfigProviderProps 把 locale 声明成了
    PropType 包装对象而不是值本身），绕过去需要一次类型断言，而断言会把真实的
    类型错误一起掩盖掉。这是 Element Plus 官方的用法。
  -->
  <el-config-provider :locale="zhCn">
    <div class="shell">
      <header class="appbar">
        <RouterLink to="/runs" class="brand">
          <span class="brand-name">sfly</span>
          <span class="brand-sub">多 Agent 代码审查</span>
        </RouterLink>

        <nav class="nav">
          <RouterLink to="/runs" class="nav-item" :class="{ active: route.name === 'run-detail' }">
            运行记录
          </RouterLink>
          <RouterLink to="/system" class="nav-item">系统状态</RouterLink>
        </nav>

        <RouterLink to="/system" class="health" :title="healthStore.error ?? '查看系统状态'">
          <span class="dot" :class="healthStore.state" />
          <span v-if="healthStore.health" class="health-text">
            {{ healthStore.health.mode === 'lite' ? '精简模式' : '完整模式' }}
            <span class="sep">·</span>
            <span class="mono">{{ healthStore.health.config.queue_backend }}</span>
            <span class="sep">·</span>
            <span class="mono">{{ healthStore.health.config.llm_provider }}</span>
          </span>
          <span v-else class="health-text">{{ healthStore.error ? '后端未连通' : '连接中…' }}</span>
        </RouterLink>
      </header>

      <main class="main">
        <div v-if="down" class="backend-down">
          <strong>后端未连通。</strong>
          <span class="mono">{{ healthStore.error }}</span>
          <button type="button" @click="healthStore.retryNow()">重试</button>
        </div>

        <section v-if="healthStore.waiting" class="waking">
          <div class="waking-spinner" />
          <h2>正在唤醒服务</h2>
          <p class="waking-lead">
            Render 免费档在 15 分钟没人访问后会休眠，下一次访问要重新拉起容器， 通常需要 30–60 秒。
          </p>
          <p class="waking-stats mono">
            已等待 {{ healthStore.waitedS }} 秒 · 第 {{ healthStore.attempts }} 次探测
          </p>
          <button type="button" class="waking-retry" @click="healthStore.retryNow()">
            立刻重试
          </button>
        </section>

        <!--
          见上面那段：**唤醒期间**不挂载路由，否则每个视图各挂 90 秒再各报一次错。
          但「已经放弃等待」时仍然挂载 —— 那时视图自己的错误提示和这里那条
          说的是同一件事（重复一次不致命），而导航是可用的，系统状态页正好
          是排查时要看的那一页。
        -->
        <RouterView v-else />
      </main>

      <footer class="footer">
        <span>sfly v{{ healthStore.health?.version ?? '0.1.0' }}</span>
        <span class="sep">·</span>
        <span>分布式执行，集中式决策</span>
      </footer>
    </div>
  </el-config-provider>
</template>

<style scoped>
.shell {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.appbar {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 0 24px;
  height: 56px;
  background: #1f2937;
  color: #f8fafc;
  /* 顶栏吸顶：时间线可以很长，滚到下面还得能切页面 */
  position: sticky;
  top: 0;
  z-index: 10;
}

.brand {
  display: flex;
  align-items: baseline;
  gap: 8px;
  text-decoration: none;
  color: inherit;
}
.brand-name {
  font-size: 19px;
  font-weight: 700;
  letter-spacing: -0.02em;
}
.brand-sub {
  font-size: 12px;
  color: #94a3b8;
}

.nav {
  display: flex;
  gap: 4px;
  margin-left: 8px;
}
.nav-item {
  padding: 5px 12px;
  border-radius: 6px;
  font-size: 13px;
  color: #cbd5e1;
  text-decoration: none;
}
.nav-item:hover {
  background: #334155;
  color: #fff;
}
/* 详情页也把「运行记录」点亮 —— 它是列表的下钻，不是第 3 个并列的地方 */
.nav-item.router-link-exact-active,
.nav-item.active {
  background: #334155;
  color: #fff;
}

.health {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
  padding: 5px 10px;
  border-radius: 999px;
  background: #111827;
  text-decoration: none;
  color: #cbd5e1;
  font-size: 12px;
}
.health:hover {
  background: #0b1220;
}
.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #64748b;
  flex: 0 0 auto;
}
.dot.ok {
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.18);
}
.dot.bad {
  background: #ef4444;
  box-shadow: 0 0 0 3px rgba(239, 68, 68, 0.18);
}
.health-text {
  white-space: nowrap;
}
.sep {
  color: #475569;
  margin: 0 2px;
}

.main {
  flex: 1 1 auto;
  width: 100%;
  max-width: 1240px;
  margin: 0 auto;
  padding: 20px 24px 48px;
}

/* --------------------------------------------------------------------- */
/* 冷启动的两种状态                                                        */
/*                                                                        */
/* 手写而不是用 el-result：这一段要在**后端还没起来**的时候渲染，而它正是   */
/* 最不该依赖一堆组件注册的时刻（缺一个注册就是一片没样式的裸文本）。       */
/* --------------------------------------------------------------------- */

.backend-down {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 10px 14px;
  margin-bottom: 16px;
  border-radius: 8px;
  border: 1px solid #fecaca;
  background: #fef2f2;
  color: #991b1b;
  font-size: 13px;
}
.backend-down .mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  word-break: break-all;
}
.backend-down button {
  margin-left: auto;
}

.waking {
  max-width: 460px;
  margin: 96px auto 0;
  text-align: center;
  color: var(--sfly-text);
}
.waking h2 {
  margin: 18px 0 8px;
  font-size: 18px;
}
.waking-lead {
  margin: 0 0 14px;
  color: var(--sfly-text-dim);
  font-size: 13px;
  line-height: 1.7;
}
.waking-stats {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
  color: var(--sfly-text-dim);
  margin: 0 0 18px;
}
.waking-spinner {
  width: 28px;
  height: 28px;
  margin: 0 auto;
  border: 3px solid #e2e8f0;
  border-top-color: #1f2937;
  border-radius: 50%;
  animation: sfly-spin 0.9s linear infinite;
}
@keyframes sfly-spin {
  to {
    transform: rotate(360deg);
  }
}
/* 尊重系统的「减少动态效果」—— 转圈是这里唯一的动效，而它对前庭敏感的人
   不友好。停下来之后那个圈仍然在那儿，说明「在等」这件事没有丢。 */
@media (prefers-reduced-motion: reduce) {
  .waking-spinner {
    animation: none;
    border-top-color: #94a3b8;
  }
}

button {
  padding: 5px 14px;
  border-radius: 6px;
  border: 1px solid #cbd5e1;
  background: #fff;
  color: #334155;
  font-size: 13px;
  cursor: pointer;
}
button:hover {
  background: #f1f5f9;
}

.footer {
  display: flex;
  gap: 6px;
  justify-content: center;
  padding: 16px;
  color: var(--sfly-text-dim);
  font-size: 12px;
  border-top: 1px solid var(--sfly-border);
}

@media (max-width: 720px) {
  .appbar {
    height: auto;
    flex-wrap: wrap;
    gap: 8px;
    padding: 10px 16px;
  }
  .brand-sub {
    display: none;
  }
  .health {
    margin-left: 0;
  }
  .main {
    padding: 16px 12px 32px;
  }
}
</style>
