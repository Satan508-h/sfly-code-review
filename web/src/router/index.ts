import { createRouter, createWebHistory } from 'vue-router'

import RunDetailView from '@/views/RunDetailView.vue'
import RunListView from '@/views/RunListView.vue'
import SystemView from '@/views/SystemView.vue'

/**
 * 路由。
 *
 * ### 为什么用 history 模式而不是 hash
 *
 * `/#/runs/01M3...` 那种地址发给人看是没问题的，但**分享出去就变味了**：
 * 面试时你想把某个 run 的地址甩给对方，`/runs/01M3...` 显然比 `/#/runs/01M3...`
 * 更像一个正经产品。代价是服务端必须把未知路径回退到 `index.html` ——
 * 这个项目里有两个地方各自负责一次：
 *
 *   * 完整模式：`infra/nginx/default.conf` 的 `try_files $uri $uri/ /index.html`
 *   * Vercel（M10）：需要一条 rewrites 规则，否则刷新页面直接 404
 *
 * 漏掉任何一处，症状都是「点进去正常、刷新就白屏」—— 一个只在刷新时出现的
 * bug，很容易被当成浏览器缓存问题。
 *
 * ### 页面只有三个
 *
 * 列表、详情、系统。**没有「登录」「设置」这些页** —— 它是个只读的展示面板，
 * 所有写操作都在命令行或 GitHub 那边。少一个页面就少一处要维护的状态。
 */
const router = createRouter({
  history: createWebHistory(),
  routes: [
    // 默认落在列表上。首页放一张静态介绍图对面试官没用，他点开链接就是想看
    // 这个东西实际跑出来是什么样。
    { path: '/', redirect: '/runs' },
    {
      path: '/runs',
      name: 'runs',
      component: RunListView,
      meta: { title: '运行记录' },
    },
    {
      path: '/runs/:taskId',
      name: 'run-detail',
      component: RunDetailView,
      // 组件内部用 `props` 拿参数而不是读 `$route`：这样它对路由的存在
      // 没有感知，将来想在同一页里并排看两个 run 也不用改组件。
      props: true,
    },
    {
      path: '/system',
      name: 'system',
      component: SystemView,
      meta: { title: '系统状态' },
    },
    // 兜底回列表，而不是给一个「404」页面：地址拼错了、或者 run 被清理掉了，
    // 用户想去的地方都是列表。**注意这条不能拦 `/runs/:id` 的 404** ——
    // 「run 不存在」是接口的 404，由详情页自己显示，不走路由层。
    { path: '/:pathMatch(.*)*', redirect: '/runs' },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? (to.name === 'run-detail' ? '运行详情' : '')
  document.title = title ? `${title} · sfly` : 'sfly — 多 Agent 代码审查'
})

export default router
