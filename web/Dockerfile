# 前端镜像：Node 构建 → nginx 托管。
#
# **构建上下文是仓库根目录，不是 web/**。因为要同时拿到两个东西：
#   web/                    前端源码
#   infra/nginx/default.conf nginx 配置（和 compose 共用一个文件，避免两份漂移）
# 根目录的 .dockerignore 负责把 node_modules / .venv 之类排除掉。
#
# 部署到 Vercel 时用不到这个文件 —— Vercel 自己识别 web/ 下的 Vite 项目。

# --------------------------------------------------------------------------- #
# 构建
# --------------------------------------------------------------------------- #
FROM node:24-alpine AS build

WORKDIR /app

# 先只拷清单，让依赖层可缓存：改业务代码不会重装 node_modules
COPY web/package.json web/package-lock.json* ./
# 有 lock 就用 ci（严格按 lock 装，可复现）；没有就退回 install 并生成一个
RUN npm ci --no-audit --no-fund || npm install --no-audit --no-fund

COPY web/ ./

# 精简模式下后端在 Render，构建时要注入绝对地址：
#   docker build --build-arg VITE_API_BASE=https://xxx.onrender.com/api ...
# 完整模式下保持默认的相对路径 /api，由 nginx 直通代理。
ARG VITE_API_BASE=/api
ENV VITE_API_BASE=${VITE_API_BASE}
RUN npm run build

# --------------------------------------------------------------------------- #
# 运行
# --------------------------------------------------------------------------- #
# stable-alpine 跟着 nginx 的 stable 分支走。
# 不写死 1.x.y 是因为 nginx 的次版本号发布节奏不好预测，
# 写错了会在某次构建时突然 404。
FROM nginx:stable-alpine

# 配置放在 infra/ 而不是 web/ —— 它同时被运维和前端用，放中性地带
COPY infra/nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

# 基础镜像自带 nginx 的启动命令和健康检查所需的一切
