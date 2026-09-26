import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
// 从 `vitest/config` 导入而不是 `vite` —— 它只是 vite 那个 defineConfig 的
// 超集（多一个 `test` 字段的类型），`vite build` 读同一份配置照样工作。
// 拆成 vitest.config.ts 的话，别名要维护两份，而**别名不一致的症状是
// 测试里 import 不到、构建却完全正常**。
import { defineConfig } from 'vitest/config'

// 前端代码里**始终**用相对路径 `/api` 访问后端，两种模式的差异全在这里处理：
//
//   完整模式（本地）  nginx 托管静态文件并把 /api 直通到 api:8000，
//                    开发时则由下面的 devServer.proxy 转发到 localhost:8000
//   精简模式（Vercel）后端在 Render，构建时用 VITE_API_BASE 指定绝对地址：
//                     VITE_API_BASE=https://sfly-api.onrender.com/api npm run build
//
// 这样业务代码里不会出现任何硬编码的后端地址。
export default defineConfig({
  plugins: [vue()],

  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },

  server: {
    // 开发服务器独占 5273，**刻意避开 5173**。
    //
    // 5173 被 docker compose 的 web 容器占用（它把 nginx 托管的构建产物
    // 发布在宿主机的 WEB_HOST_PORT 上，默认就是 5173）。这不是「端口冲突
    // 会报错」那种好事 —— 实测两者能同时绑上：
    //
    //     0.0.0.0:5173 / [::]:5173   → Docker 的端口转发
    //     127.0.0.1:5173 / [::1]:5173 → Vite
    //
    // 两边的 bind 都成功，谁都不报错，而 `localhost` 在这台机器上优先解析到
    // ::1 —— 于是浏览器（和 curl）打开的是**镜像里那份旧构建**，改代码看不到
    // 任何变化，控制台一句话都没有。
    //
    // strictPort 是第二道防线：真被占了就报错退出，而不是自动换一个端口
    // 然后照常打印「Local: http://localhost:5174」—— 那种「一切都是好的，
    // 只是你访问错了地方」是同一个坑的另一副面孔。
    port: 5273,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // SSE 必须关掉代理缓冲，否则进度事件会攒到最后一次性到达 ——
        // 表现为「进度条不动，刷新一下结果全出来了」。
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            if (proxyRes.headers['content-type']?.includes('text/event-stream')) {
              proxyRes.headers['x-accel-buffering'] = 'no'
            }
          })
        },
      },
    },
  },

  build: {
    outDir: 'dist',
    sourcemap: true,
    // Element Plus 体积不小，拆出去让主包小一点、首屏快一点。
    // 精简模式跑在 Render 免费版上，冷启动本来就慢，首屏能省则省。
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ['vue', 'vue-router', 'pinia'],
          element: ['element-plus', '@element-plus/icons-vue'],
        },
      },
    },
  },

  // ------------------------------------------------------------------------- //
  // 测试
  // ------------------------------------------------------------------------- //
  // 前端真正值得测的是**纯逻辑与渲染出来的内容**（排序、分组、置信度怎么显示），
  // 而不是把组件的 DOM 结构钉死 —— 后者改一次样式就要改一次断言，最后所有人
  // 都学会了「测试红了就改期望值」，那等于没有测试。
  //
  // 所以这里没有装 @vue/test-utils 的快照插件，也没有覆盖率门槛。断言写的都是
  // 「页面上出现了什么字」，那种断言只有真的做错了才会红。
  test: {
    // jsdom 而不是 happy-dom：Element Plus 的组件会摸 document 上不少 API，
    // jsdom 的覆盖更全，而这里的测试量级（毫秒级）完全不在乎那点性能差。
    environment: 'jsdom',
    // 不注入全局的 describe/it/expect —— 显式 `import { it } from 'vitest'`
    // 能让 IDE 和 vue-tsc 都认得出这些符号，而全局变量需要用 types 声明，
    // 声明漏了的表现是「类型检查报找不到 describe」。
    globals: false,
  },
})
