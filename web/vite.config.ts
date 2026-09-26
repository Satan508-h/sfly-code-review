import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

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
})
