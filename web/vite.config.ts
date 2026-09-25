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
    port: 5173,
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
