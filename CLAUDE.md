# CLAUDE.md

本文件是给 Claude Code 的项目上下文。**每次改动前先读「不可违反的约定」一节。**

---

## 项目是什么

`sfly` — 基于多 Agent 的分布式代码审查系统。GitHub Webhook 触发 PR 审查，3 个专业 Worker（安全 / 性能 / 风格）通过 Redis Streams 消费者组并行消费任务，编排器用 LangGraph 状态机协调，主 Agent 汇总去重、消解冲突、重算置信度，生成最终报告并回写 PR 评论。

定位：**分布式执行 + 集中式决策**的轻量级 PR 审查平台。不引入 K8s，不引入 Kafka。

## 两种拓扑（整个项目的核心卖点）

同一套代码，两种部署形态，由 `QUEUE_BACKEND` 环境变量选择：

| | 完整模式 | 精简模式 |
|---|---|---|
| 启动 | `docker compose up` | Render 单容器 |
| 容器数 | 7+（api / orchestrator / worker×3 / redis / postgres / web） | 1 |
| 队列 | `RedisStreamsQueue`（消费者组、XAUTOCLAIM、死信） | `InMemoryQueue`（asyncio 队列） |
| 锁 | `RedisLock`（SET NX PX + Lua 释放） | `InMemoryLock`（asyncio.Lock 字典） |
| 存储 | 本地 Postgres 容器 | Neon Postgres |
| 前端 | nginx 托管，同源代理 `/api` | Vercel 独立部署，跨域 + CORS |
| 水平扩展 | `docker compose up --scale worker-security=3` | 不支持（单进程 asyncio 并发） |

**`GraphRunner`、`WorkerPool`、全部 LangGraph 节点在两种模式下是同一批对象。** 这不是巧合而是约束——任何让节点感知到具体队列实现的改动都是在破坏项目的主要论点。

---

## 仓库地图

```
apps/
  api/           FastAPI 网关：HMAC 校验、投递去重、runs 接口、SSE
  orchestrator/  LangGraph 图 + coordinator 协程 + 超时扫描器
  workers/       一套代码三种部署：python -m sfly_workers --spec {security|performance|style}
  lite/          单事件循环，同时跑 API + GraphRunner + WorkerPool（Render 用）
packages/
  shared/        sfly_shared — 领域契约（contracts.py）、配置、ID 生成
  bus/           sfly_bus — TaskQueue / RunStore / Lock 协议 + 两种实现 + migrations
                 （锁和队列放在一起：memory.py 有 InMemoryQueue + InMemoryLock，
                   redis_streams.py 有 RedisStreamsQueue + RedisLock）
  agent-core/    sfly_agent — LLM 抽象与结构化输出、RAG、聚合算法、GitHub 客户端
web/             Vue 3 + Element Plus + Vite SPA
infra/           postgres init.sql、redis.conf、nginx 配置、GitHub 限流桩
fixtures/        diff 样例、webhook payload、大 PR fixture
scripts/         replay_webhook.py、measure_overhead.py、seed_db.py、demo_reclaim.py
tests/           contracts/（两后端共用的契约，被 unit 与 integration 同时导入）
                 unit（无 Docker 无密钥）/ integration（需 Docker 里的 Redis）/ e2e / eval
reports/         评测报告，提交进仓库
```

**`tests/contracts/` 是「两种拓扑」这个卖点的证据本身**：`queue_contract.py`
和 `lock_contract.py` 各是一份行为契约，两种实现各继承一次 ——
`tests/unit/bus/test_memory_*.py` 与 `tests/integration/bus/test_redis_*.py`。
在下面加一条测试，四个文件同时受益；而**放宽某一条断言就等于毁掉证据**，
所以契约里只准用 Protocol 上的方法（见该文件开头的三条纪律）。

**数据流**：`review_bootstrap → review_tasks → review_results → dead_letter`

注意 API **不直接写 `review_tasks`**。它只写 `review_bootstrap`；文件风险排序和规则检索由编排层的 `plan` 节点负责。Worker 收到的 `TaskMessage` 已经带上检索好的规则，所以 Worker 保持无状态且不需要 RAG 依赖。

---

## 不可违反的约定

这五条每一条都对应一个具体的、已经想清楚的失败模式。改代码时如果发现自己在违反其中任何一条，先停下来问。

### 1. 投递顺序铁律：存 Postgres → XADD 结果 → XACK

```python
result = await self._run_llm(task)  # 可能耗时 60s
await store.save_result(result)  # 1. 先落库
await queue.publish_result(result)  # 2. 再唤醒编排器
await handle.ack()  # 3. 最后才离开 PEL
```

- 先 `XADD` 后写库 → coordinator 被唤醒去读一个还不存在的结果，屏障检查失败
- 先 `XACK` 后写库 → 结果同时从 PEL 和数据库消失，永久丢失

### 2. 失败也是结果

Worker 放弃之前**必须先发一条 `status="failed"` 的 `WorkerResult` 再 ack**。否则 `wait` 节点的屏障永远闭合不了，整个 run 挂到超时。

推论：死信队列**不是**完成机制。它只做运维可见性（什么失败了、为什么、多频繁）。如果你发现自己在用死信去解除 run 的阻塞，那是 bug。结果进死信只能是**副本**，与补发的 failed 结果并存。

### 3. 状态累加器一律用 dict，不用 list

`ReviewState` 里每一个会被多个 Worker 或重放写入的字段都是 `dict[str, X]`，键是稳定 ID（`task_id`、`worker_type`、`fingerprint`）。

原因：LangGraph 在恢复和重放时会重新执行节点。list 累加器（`operator.add`）遇到重放会产生重复条目；dict 按键合并天然幂等，同一个结果合并两次得到同一个 dict。

### 4. 只有 factory.py 分支环境变量

**`QUEUE_BACKEND` / `LOCK_BACKEND` 这两个值，只允许 `packages/bus/sfly_bus/factory.py` 拿来分支。**

一旦某个节点或 Worker 开始判断「我用的是不是 Redis」，两种拓扑就跑在不同的代码路径上，共用代码这件事从「事实」退化成「宣传」—— 而且不会有任何东西报错。

这条约定现在有**可执行的检查**：`tests/unit/bus/test_protocols.py::test_only_the_factory_branches_on_the_transport_backend` 用 AST 扫 `packages/` 和 `apps/`，找出所有「拿这两个设置做判断」的位置（`if` / `elif` / 三元 / `match` / 推导式的 `if`）。

> 早先这条写的是「grep 应该只返回一个文件」，那句话已经不准了，现在改成上面这条 —— 因为这两个名字**必然**会出现在别处，而且都不算违规：`config.py` 里定义它们，`api/main.py` 把当前值回显给健康页和前端（`App.vue` 顶部的后端回显），`lite/__main__.py` 的文档字符串里提到它们。把定义和展示也算成违规，这条约定就只能靠人肉判断，最后一定会漂移。
>
> 检查也**不是防火墙**：把 `s.queue_backend` 先存进一个变量再判断，它就抓不到了。真正的防线仍然是评审 —— 它是烟雾报警器。

### 5. 契约变更先改 contracts.py

`packages/shared/sfly_shared/contracts.py` 是唯一的真相来源，`TaskMessage`/`WorkerResult`/`Finding` 在 Rust 意义上被 5 个 app 消费。

改动顺序永远是：先改 contracts.py → 跑 `pytest tests/unit/contracts` → 再改消费方。反过来做会产生静默的字段丢失，因为 Pydantic 默认忽略多余字段。

### 6. `/healthz` 永不探测依赖

存活与就绪是**两个不同的接口**，判据必须不同：

| | 接口 | 决定什么 | 依赖挂了时 |
|---|---|---|---|
| 存活 | `/healthz` | Docker 要不要重启这个容器 | **仍返回 200** |
| 就绪 | `/api/health` | 要不要把流量打过来 | **返回 503** |

合成一个接口就必然二选一：要么在数据库抖动时重启一堆无辜的容器（而 `restart: unless-stopped` 会让它们进入退避循环，日志被重启信息冲掉），要么让负载均衡把流量送进一个干不了活的服务。

新增任何依赖时，探测**只加到 `/api/health`**。容器 healthcheck 用的是心跳文件（`/tmp/sfly-heartbeat`）和 HTTP 存活探针，都不该知道数据库的存在。

---

## 几个容易被直觉带错的技术决定

这些是刻意选择的，不是疏忽。改之前先看理由。

**幂等靠数据库唯一约束，不靠 Redis SETNX。**
`SETNX sfly:w:done:{task_id}:{worker_type}` 只是省 token 的快路径。真正的保证是 `worker_results` 的 `PRIMARY KEY (task_id, worker_type)` + `INSERT ... ON CONFLICT DO NOTHING`。Redis 键会过期、会随 Redis 重启丢失、可能在后续写库失败时已经被设上。约束不会。

**去重用 `rapidfuzz` + 并查集，不用 embedding。**
确定性、可复现（评测需要）、无模型下载、微秒级。相似度阈值：同 Worker 0.75，跨 Worker 0.55。

**冲突消解用确定性规则引擎，不用 LLM。**
LLM 裁判会引入非确定性，直接毁掉评测的可复现性。`CONFLICT_RESOLVER=llm` 保留为 M11 的 A/B 对照，不做默认。

**RAG 用手写 YAML 规则库 + BM25，不用向量库。**
60–120 条手写规则（引 OWASP Top 10 / CWE Top 25 / Google 风格指南）。规则库本身是可被审阅的作品。向量库是 M11 可选升级。

**LLM 输出契约用 `response_format={"type":"json_object"}`。**
DeepSeek 的 `json_schema` 未文档化，`strict` 工具模式要 Beta 端点。两者都不能作为核心契约。这也是 provider 可换的关键。

**永不发 `APPROVE`。**
机器人审批人类 PR 是策略漏洞。只发 `REQUEST_CHANGES` 或 `COMMENT`。

**`wait` 节点用 `interrupt()` 而非轮询。**
图暂停期间 orchestrator 崩溃，恢复靠的是一条 SQL 查询（`review_runs.status + deadline_at`）而不是内存状态。这是断点恢复故事成立的地方。
逃生开关：`WAIT_STRATEGY=poll`，节点签名完全相同，约 20 行。卡住超过一天就切过去——**先跑通优于先优雅**。

**任何可能卡住的状态都必须是一行带 `deadline_at` 的记录。**
写不成一条 `SELECT` 的恢复查询，说明状态藏在了会丢失的地方。

**裁剪和回收是两件事，别把它们当成一件事。** 实测（Redis 7.4）：`XTRIM` 只从流里
删条目，**不动 PEL** —— 被裁掉的消息的 id 会悬在 PEL 上，直到下一次 `XAUTOCLAIM`
才被摘掉并在返回值的第三段里报出来。两个后端的**清理时机**因此不同（内存实现在
裁剪那一刻就清，Redis 惰性），但**可观察的结果必须一样**：既不会被投递，也不会
被当成「待重投」送回来。契约只断言结果，不断言时机。

> 这条是被一次读错纠正的：第一次跑 `redis-cli` 看到「`XTRIM` 之后 `XPENDING`
> 少了一条」，就照着「Redis 在裁剪时会清 PEL」去写了内存实现 —— 而那个读数是
> 同一段脚本里紧跟着的 `XAUTOCLAIM` 造成的。**照着读错的现象写实现不会有任何
> 报错**，只会让两个后端在某条路径上悄悄分叉。现在 `tests/integration/bus/
> test_redis_queue.py` 里有一条专门钉住真实机制的测试。

**`redis.exceptions.ConnectionError` 不是内置的 `ConnectionError`。**
它继承自 `RedisError`，与内置的那个没有关系 —— 写 `except ConnectionError` 时
捕到的是内置的，于是 redis-py 的连接错误会**穿过**这个分支。同一件事也发生在
`retry_on_error=[ConnectionError, TimeoutError]` 上：redis-py 的 `Retry` 默认就认
它自己那两个异常，写内置版本不但没有效果，还会让人以为已经配了重试。
`redis_streams.py` 里用 `RedisConnectionError` / `RedisTimeoutError` 别名区分。

**Windows 上必须用 `sfly_shared.aio.run()`，不能用 `asyncio.run()`。**
Windows 默认的 `ProactorEventLoop` 不支持 `add_reader`，而 psycopg v3 的异步模式正是靠它实现的 —— 不换循环，进程能起来、日志正常、然后在第一次查询时炸掉，报
`Psycopg cannot use the 'ProactorEventLoop' to run in async mode`。所有 `__main__.py` 和
`tests/conftest.py` 都已经调用，写新的入口时别漏。详见 `packages/shared/sfly_shared/aio.py`。

**健康探测必须走独立连接，不能复用连接池。**
`pool.connection(timeout=N)` 只限制「等池子分配连接」的时间；一旦拿到连接，后续查询
**没有任何超时**。`docker pause` 之下 `SHOW server_version` 会永远阻塞，把 `/api/health`
整个挂死 —— 而 `docker pause postgres` 正是 README 里承诺要演示的场景。
`PostgresPool.ping()` / `RedisStreamsQueue.ping()` 因此都新开一条一次性连接，
外面套 `asyncio.wait_for`，超时取消的是一条马上要销毁的连接，不牵连池子。

---

## 常用命令

**主入口是 `python tasks.py <命令>`**，跨平台、零依赖。`make <命令>` 是等价的
瘦壳（内部就是委托给 tasks.py），给 Linux/macOS/CI 用。**开发机是 Windows，
默认没有 make，所以一律以 tasks.py 为准。**

```bash
python tasks.py up         # 构建 + 启动全部服务 + 阻塞到健康检查通过
python tasks.py down       # 停止（保留数据卷）
python tasks.py clean      # 停止并删除数据卷（改过 infra/postgres/init.sql 后必须）
python tasks.py logs -f    # 跟踪全部日志
python tasks.py ps         # 各容器健康状态
python tasks.py health     # 依赖真实连通性（探 /api/health，不是 /healthz）

python tasks.py test       # 单测（无 Docker、无密钥；Linux/CI 约 2s，Windows 约 16s）
python tasks.py test-int   # 集成测试（需 Docker 里的 Redis；用 db 15，跑前 flushdb）
python tasks.py test-e2e   # 端到端（需真实密钥，会花钱，有 $2 上限）
python tasks.py eval       # 评测集 → reports/eval-<sha>.md
python tasks.py demo-reclaim  # 队列容错演示：副本猝死 → 回收 → attempt=2（只要 Redis）

python tasks.py lint       # ruff check + format --check
python tasks.py fmt        # 自动格式化
python tasks.py typecheck  # mypy strict
```

> **单测在 Windows 上比 Linux 慢一个数量级，这是平台差异不是回归。**
> 探测类测试连的是 `127.0.0.1:1`（保证连不上）。Linux 上拒绝连接是即时的，
> Windows 的 `SelectorEventLoop` 要花约 2 秒才报 `ECONNREFUSED`。
> CI 跑在 `ubuntu-latest`，所以那边的耗时才是这条命令的真实成本。

```bash
# 单次审查：不接队列、不连数据库、不起容器，调提示词时用它
# （比走完整链路快一个数量级）。JSON 走 stdout，人读的摘要走 stderr，
# 所以可以直接 `| jq`。退出码 0=有结果 / 2=审查失败 / 3=输入不是 diff。
python -m sfly_workers --spec security --diff fixtures/security_demo.diff

# 分布式能力验证（这几条就是项目要证明的东西）
python tasks.py scale 3        # --scale worker-security=3，三个副本竞争消费
python tasks.py kill-worker    # 杀掉一个 Worker：run 仍应跑完并显示降级徽章
python tasks.py demo           # 端到端：投递 3 次同一 webhook → 1 run + 2 duplicate
```

**依赖管理**：改动任何 `pyproject.toml` 之后必须 `python tasks.py lock` 并提交
`uv.lock` —— Docker 构建用的是 `uv sync --frozen`，lock 过期会直接构建失败。

## 环境变量

见 `.env.example`。核心几项：`DATABASE_URL`、`REDIS_URL`、`GITHUB_TOKEN`、`GITHUB_WEBHOOK_SECRET`、`LLM_API_KEY`、`LLM_PROVIDER`（`mock|deepseek|openai`）、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`。

**默认全部可空**——`LLM_PROVIDER=mock` + 本地 Docker Postgres/Redis 时不需要任何密钥就能跑通全链路。这是刻意的：开发和 CI 不应该依赖外部服务。

## 代码约定

- Python 3.12（容器内），`uv` 管理 workspace，一份 `uv.lock`
- 全异步：`async def` + `psycopg` v3（**不是 asyncpg**，LangGraph 的 Postgres checkpointer 用 psycopg）
- 结构化日志用 `structlog`，每条日志带 `task_id` / `worker_type` 便于串联
- **日志一律写 stderr，stdout 只留给程序输出。** 只要有一行日志混进 stdout，
  `python -m sfly_workers --diff x.diff | jq` 就会在第一个字符上解析失败，
  而报错指向 jq 的语法错误 —— 完全看不出真正的原因。
  注意日志级别不能用 `logging.getLogger().setLevel()` 调：structlog 的
  `PrintLogger` 不经过标准库的 root logger，那一行看着像在静音，实际无效。
- 类型标注必须完整，`mypy` 在 CI 中跑（覆盖 `packages` `apps` `tests` 三处）
- 时间统一 UTC，`datetime.now(UTC)`

## 当前进度

- [x] Step 0 — 文档、契约、目录骨架、compose
- [x] M0 — workspace、`sfly_bus` 连接层、`/api/health` 真实依赖探测
- [x] M1 — diff 解析、Mock LLM、修复阶梯、规则库、`--diff` 独立 CLI
- [x] M2 — 队列协议 + InMemoryQueue / InMemoryLock + 指纹 + 两后端的契约测试骨架
- [x] M3 — RedisStreamsQueue（含 XAUTOCLAIM / 死信 / 重试计数）+ RedisLock + 契约跑真 Redis
- [ ] M4 — Postgres schema + 幂等 migrate
- [ ] M5 — LangGraph 图（高风险：`interrupt()`）
- [ ] M6 — FastAPI + SSE 带 Last-Event-ID 补齐
- [ ] M7 — GitHub 客户端 + publish 节点
- [ ] M8 — Vue SPA
- [ ] M9 — 聚合硬化 + 评测集
- [ ] M10 — 精简模式 + Render / Vercel 部署
- [ ] M11 — 可选：pgvector、LLM 冲突消解 A/B

详细计划见 `~/.claude/plans/1-agent-pr-curried-unicorn.md`。
