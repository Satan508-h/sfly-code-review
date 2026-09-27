---
# **这个文件是 Hugging Face Spaces 的配置头，不是文档。**
#
# 它由 `python tasks.py deploy-hf` 在上传那一刻拼到 README 最前面 ——
# 所以它不占项目首页，也不进 GitHub 的渲染。
#
# 精简模式的线上演示跑在 HF Spaces 上，而 Spaces 的配置（用哪种 SDK、
# 监听哪个端口）**只从仓库根目录这个 README 的 frontmatter 读** —— 没有
# 别的文件可以放。所以它必须在最上面。
#
# 代价要知道：GitHub 会把它渲染成一张表（这是 GitHub 的行为，不是我们的选择），
# 所以项目首页最上面会多出这几行。这是本仓库唯一一处为部署让步的地方。
# 换平台（比如 Render）时删掉整块即可，别的文件都不用动 —— 见 docs/DEPLOY.md。
#
# `app_port` 用 7860 而不是程序默认的 8000，是**刻意**的：7860 是 Spaces 的
# 默认端口，而平台有可能自己往容器里注入一个 PORT 环境变量。两边都写 7860
# 之后，无论它注不注入、注进来是哪个值，端口都是一致的 —— 不一致的症状是
# 「构建成功、日志正常、页面打不开」，而那种错很难往端口上想。
# 线上那个 7860 由 `python tasks.py deploy-hf` 设上去（见 docs/DEPLOY.md 第 2 步）。
title: sfly — 多 Agent 代码审查
emoji: 🔍
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 7860
---
