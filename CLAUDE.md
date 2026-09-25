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
                 webhook.py 签名（**签发与校验只此一份**，回放脚本 import 它）
                 github_payload.py 载荷 → BootstrapMessage（纯函数，不碰网络/数据库）
                 sse.py 收流条件；routes/{webhook,runs,events}.py；deps.py 注入点
                 **它不建 run** —— run 的生命周期属于编排器（见 routes/runs.py）
  orchestrator/  LangGraph 图 + coordinator 协程 + 超时扫描器
                 nodes/ 七个节点各一个文件；context.py 是节点拿依赖的唯一入口
                 checkpointer.py **自己一个连接池**（autocommit，见下面那条）
  workers/       一套代码三种部署：python -m sfly_workers --spec {security|performance|style}
                 pool.py 是消费循环本体（一个容器一条 lane，或一个进程三条协程）
  lite/          单事件循环，同时跑 API + GraphRunner + WorkerPool（Render 用）
packages/
  shared/        sfly_shared — 领域契约（contracts.py）、配置、ID 生成、console 编码、
                 **diff.py**（unified diff 解析 —— 网关/编排器/Worker 三处共用，
                 所以它在这里。放在 agent-core 会让网关拖进整个 LLM 栈）
  bus/           sfly_bus — TaskQueue / RunStore / Lock 协议 + 两种实现
                 （锁和队列放在一起：memory.py 有 InMemoryQueue + InMemoryLock，
                   redis_streams.py 有 RedisStreamsQueue + RedisLock；
                   postgres.py 有 PostgresPool + PostgresRunStore —— 仓储只有这一个实现）
                 migrations/ 纯 SQL 迁移 + 迁移器（001 六张业务表、002 webhook 投递账本）
                 注意：迁移 SQL 是**数据文件**，Dockerfile 靠 `COPY packages` 带进镜像
  agent-core/    sfly_agent — LLM 抽象与结构化输出（含 pricing.py）、RAG、
                 state.py 图状态、risk.py 文件风险排序、labels.py 显示层标签、
                 aggregate/ 主 Agent 的聚合（全确定性、零 LLM 调用）、GitHub 客户端
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

**数据流**：`webhook → webhook_deliveries（账本）→ review_bootstrap → review_tasks
→ review_results → dead_letter`

注意 API **不直接写 `review_tasks`**。它只写 `review_bootstrap`；文件风险排序和规则检索由编排层的 `plan` 节点负责。Worker 收到的 `TaskMessage` 已经带上检索好的规则，所以 Worker 保持无状态且不需要 RAG 依赖。

---

## 不可违反的约定

每一条都对应一个具体的、已经想清楚的失败模式。改代码时如果发现自己在违反其中任何一条，先停下来问。

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

### 6. 图节点必须幂等

**LangGraph 恢复时会重新执行节点，这是正常路径不是异常路径。** ``wait`` 在
``interrupt()`` 挂起后被唤醒时，整个函数会**从头重跑一遍**（这是 ``interrupt()``
的语义，不是实现细节）。

两个直接后果：

* 节点里的每一次写入都要能重放。``create_run`` / ``set_plan`` / ``save_report``
  本来就是幂等的（upsert）；``dispatch`` 会重复发消息，而它靠的是 Worker 那一侧的
  ``exists_result`` 快路径 + ``worker_results`` 的主键 —— **不是靠在自己状态里记
  一份「已派发」的账**（那份账在崩溃时同样会丢）。
* ``wait`` 的屏障检查写成 ``while`` 循环：被唤醒 → 重新查库 → 闭合了就往下走、
  没闭合就**再挂起一次**。写成 ``if`` 的话，一次提前唤醒（扫描器或重复唤醒）
  就会带着不完整的屏障进 ``aggregate``。

### 7. `/healthz` 永不探测依赖

存活与就绪是**两个不同的接口**，判据必须不同：

| | 接口 | 决定什么 | 依赖挂了时 |
|---|---|---|---|
| 存活 | `/healthz` | Docker 要不要重启这个容器 | **仍返回 200** |
| 就绪 | `/api/health` | 要不要把流量打过来 | **返回 503** |

合成一个接口就必然二选一：要么在数据库抖动时重启一堆无辜的容器（而 `restart: unless-stopped` 会让它们进入退避循环，日志被重启信息冲掉），要么让负载均衡把流量送进一个干不了活的服务。

新增任何依赖时，探测**只加到 `/api/health`**。容器 healthcheck 用的是心跳文件（`/tmp/sfly-heartbeat`）和 HTTP 存活探针，都不该知道数据库的存在。

### 8. 未认证的输入永不写库

`POST /api/webhook` 上，**验签和「拿到 delivery id」都必须在第一次写库之前**。

理由不是「脏数据不好看」，而是**去重机制会被反过来当攻击面用**：任何人都能用
**未来的** delivery id 投一条垃圾载荷提前占位，于是真实的投递到达时被判成重复、
**永久丢掉**。而丢掉的表现是「GitHub 显示 200，那个 PR 没被审」—— 查不出任何东西。

同一个坑的另一面：**投递失败时不结算那条投递，而是释放它**。账本记的是
**处置结果**，而那次处置没有发生。留一行永远停在 `received` 的记录，
下一次排查时会被读成「处理过但没结果」—— 一个查不下去的状态。

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

**迁移器（`sfly_bus/migrations/`）有三条不能破的规则。**
纯 SQL 文件 + `schema_version` 记账表，一个事务整批应用，`pg_advisory_xact_lock`
串行化 —— 完整模式下五个容器**同时**调 `migrate()` 是常态，不是边角情况。

1. **已应用的迁移不能再改。** 内容变了（sha256）直接报 `MigrationDriftError`，
   而且 `migrate_on_startup` 会**让它抛出去**（和「连不上数据库」相反：那种情况
   记一条 error 继续跑，进程要能起来在 `/api/health` 里说话）。改结构加
   `002_*.sql`。本地试验要重来就是 `python tasks.py clean`。
2. **版本号必须从 1 连续。** 跳号 = 有文件在合并里丢了，而这件事**只在空库上暴露**：
   已经迁到 3 的库照常跑，新克隆的库少建一张表，然后在某条查询上炸掉。
3. **迁移 SQL 永远不传参数。** 实测：psycopg 3 在不传参数时走**简单查询协议**，
   Postgres 自己按分号切分，于是一整份文件可以一次 `execute()`；一旦带上参数就
   改用扩展协议，而它**一次只允许一条语句** ——
   `SyntaxError: cannot insert multiple commands into a prepared statement`。
   所以 `run_migrations` 里的 `execute` 一律不带参数，带参数的语句单独发。

**checkpointer 必须有自己的连接池 —— 不能复用 `PostgresPool`。**
两个理由，第二个是硬的：

1. 仓储的池是非 autocommit 的（每处写入都显式 `conn.transaction()`），
   而 checkpointer 的写入路径是一串裸 `conn.execute()`，**它依赖连接的
   autocommit**。给它非 autocommit 的连接，那些写入会停在一个永不提交的事务里：
   图能跑、日志正常、状态读出来也对（同一个连接看得见自己的未提交数据），
   **然后进程一重启，checkpoint 全没了**。
2. `AsyncPostgresSaver.setup()` 里有 `CREATE INDEX CONCURRENTLY`，而 Postgres
   **禁止**它出现在事务块里。就算解决了第 1 条，建表那一步也会直接报
   `cannot run inside a transaction block`。

所以 `checkpointer.py` 自己开一个池，参数照抄 LangGraph 的 `from_conn_string`
（`autocommit=True, prepare_threshold=0, row_factory=dict_row`）。`prepare_threshold=0`
是给 Neon 的连接池端点（pgbouncer，transaction 模式）准备的 —— 它不支持
prepared statements，而报出来的错是 `prepared statement "s0" already exists`，
看起来像并发 bug，其实是端点类型不对。

**图状态里只放 JSON，不放 pydantic 对象。**
实测：`JsonPlusSerializer`（checkpointer 的默认序列化器）确实能把 `FilePatch` / `Rule`
存进去再读回来，**但每次读都会打印**
「Deserializing unregistered type sfly_shared.contracts.FilePatch from checkpoint.
This will be blocked in a future version」。要在将来继续可用，就得维护一份
「允许的契约类型」白名单 —— 而那份清单一定会漂移（加个字段、引个新类型，
忘了登记只会在运行时看到一行警告，而那是最容易被忽略的信号）。
换成纯 JSON 之后这件事就不存在了，顺带让 `SELECT checkpoint FROM checkpoints`
直接可读。代价是节点边界上要做一次 `model_validate` —— 显式的、看得见的成本。

**「谁没上报」和「谁没产出可用结果」是两个问题。**
M5 实测踩到：报告显示 `degraded=True` 而 `missing_workers=[]`，前端那个降级徽章
找不到任何一个可以显示的名字。原因是 `aggregate` 按「谁不在 `worker_results` 里」
算 missing —— 而 `wait` 会给超时的 Worker **补写 failed 结果**（约定 #2），
所以 aggregate 跑的时候那个集合永远是空的。

现在分开命名：`wait` 算的是 `deadline_missed`（屏障能否闭合，判据是「有没有结果」，
失败也算），报告里的 `missing_workers` 算的是「有没有**可用**的结果」。

**事件要由因果起点写。**
`worker.result` 一开始是协调协程写的，实测会写出错误的顺序：协调协程和图是
**并发的两条路径**，而图判断屏障读的是数据库 —— 所以「三条结果都在库里了、
图已经 aggregate 完、协调协程才开始消费第一条消息」完全可能，时间线上于是出现
`worker.result` 排在 `run.finished` 后面。那不是排序问题，是**写事件的人站错了
位置**：这件事的因果起点是「Worker 写完了结果」，就该由 Worker 在那一刻记下来。

**验签必须对原始字节做，不对重新序列化后的 JSON 做。**
`sha256=HMAC(secret, raw_body)`。先 `json.loads` 再 `json.dumps` 回去验签，
键顺序/空白/非 ASCII 转义都可能变，于是 HMAC 一定对不上 —— 而症状是
「密钥明明配对了却验不过」，排查会先跑偏到密钥上。所以路由里先
`await request.body()` 拿字节、再解析。同一件事的另一面：
`hexdigest` 与 `sha256=hexdigest` 混用，症状一模一样。
（`sfly_api/webhook.py` 的 `sign` 返回**带前缀的完整头部值**就是为了这个。）

**「正在处理」和「死在中途」只能靠时间区分，不能靠状态。**
两条撞上同一条没结算的投递记录时，可能是「另一个请求此刻正在处理它」
（绝不能重复投递），也可能是「上一次处理到一半就崩了」（必须接管，
否则那个 PR 永远不会被审）—— 而它们的**状态一模一样**（都是 `received`）。
唯一的区别是时间：一次处理只有几次 IO（毫秒级），崩溃留下的是一个
**再也不会动**的时间戳。所以有 `INFLIGHT_WINDOW_S`（60 秒）。
这条是被集成测试逼出来的：并发投同一个 delivery id 时，5 个请求里有 4 个
「接管」并各自投了一条 bootstrap —— **5 条消息**。

**`docker compose` 只把 `env_file` / `environment:` 里的变量传进容器。**
命令行前面写的 `FOO=bar docker compose up -d api` **不会**让容器看到 `FOO`
（那只用于 compose 文件里的 `${FOO}` 插值）。M6 验收「配了密钥之后真的会拒绝
错误签名」时踩到过：那次实验什么也没验证，却看起来通过了 —— 因为服务端
根本没读到那个密钥。**验证配置类功能的实验，要先确认配置真的进去了**
（`/api/health` 的 `config` 回显就是干这个的）。

**本地 venv 装了全部 workspace 包，所以「缺依赖」在本地测不出来。**
`uv sync --all-packages` 让 `import sfly_agent` 在开发机上永远成功，
而 `apps/api/pyproject.toml` 里根本没有那个依赖 —— 容器里才发现
`ModuleNotFoundError: No module named 'sfly_agent'`。
`docker compose up` 的容器验收因此不是「顺手跑一遍」，它是唯一能发现这类问题的
路径。（顺带一条：一个模块该住在哪个包，判据是**它的消费者有没有权利依赖那个包**。
`diff.py` 因为网关也要用，从 `sfly_agent` 搬到了 `sfly_shared` ——
网关不该为了解析 diff 拖进 rapidfuzz / rank_bm25 / LLM 客户端。）

**`run_events.seq` 是**全表**自增，同一个 run 的 seq 会跳。**
它只保证「同一条 run 内递增」，中间那些号属于别的 run（同一张表上并行跑着多个
run 是常态）。所以**不能断言 `[1,2,3]` 这种连续性** —— 要验证「断线重连没有缺口」，
只能拿 SSR 收到的那批 seq 和 `GET /api/runs/{id}` 返回的全量 seq 对比
（`scripts/replay_webhook.py --drop-after` 就是这么做的）。
另外它也不是「事件序号」：任何按 seq 推断事件条数的代码都是错的。

**SSE 收流必须留宽限期（终态之后再等 5 秒）。**
`publish` 先写状态、后写事件，两步之间有真实的窗口 —— 一看到终态就收流，
客户端会**永远看不到最后那条 `run.finished`**，而且看起来完全正常
（它只是没再收到东西）。同一条规则的另外三处：
「断言 run 到终态之后不能立刻去读事件」（见下面 `publish` 那条）、
SSE 的 `id` 必须是 `seq`（它是 `Last-Event-ID` 的唯一来源）、
以及 run 不存在时要回 **404 而不是空流**（404 和空流的区别就是
「重试」和「永远等下去」的区别）。

**`publish` 先写状态、后写事件。**
两次写库不可能原子，必然有一个窗口，而两种顺序的失败方向不一样：状态先写的话，
崩在中间的表现是「run 读作已完成、时间线少了最后一条」—— 客户端等不到
`run.finished` 就读一次状态，发现已经完成，是安全的失败方向。反过来会让 run
永远停在 `aggregating`，而**没有任何东西能唤醒它**（`due_runs` 只看
`dispatched`/`waiting`）。推论：**断言「run 到终态」之后不能立刻去读事件**，
那两件事本来就没有先后保证。

**`uv sync` 不加 `--all-packages` 会把 workspace 的包全部卸掉。**
根 `pyproject.toml` 是 `package = false` 的虚拟工作区，所以裸 `uv sync` 只装
根项目自己的 dev 依赖 —— 表现是「同步成功了，然后 `import pydantic` 报
ModuleNotFoundError」，而 pydantic 是 `sfly-shared` 的依赖，跟着成员包一起被卸了。
**一律用 `python tasks.py sync`**（它内部就是 `uv sync --all-packages`）。

**`_Contract` 开了 `str_strip_whitespace=True`，它会吃掉首尾空白。**
评论正文结尾那个换行在存进 jsonb 再读回来时消失，于是
`finalize` 事件里的 `comment_chars` 和 `publish` 里的会差 1 —— 一个看起来像 bug
却什么也不说明的差异。渲染时干脆不留：**让它一开始就等于最终形态**。
写任何会被这个契约接住的字符串时，都要假设首尾空白不存在。

**连接的行工厂会传染给每一个在它上面执行的 helper。**
`PostgresPool` 的连接设了 `dict_row`（仓储里所有查询都按列名取值），于是任何
拿这个连接执行 SQL 的辅助函数都会拿到 dict 而不是元组。M4 实测踩到：
`_applied_versions` 写的是 `row[0]`，报出来的是 `KeyError: 0` —— 一个指不到
真正原因的错误。**在自己开 cursor 的地方显式声明 `row_factory`**，别假设调用方。
（同一类：`AsyncConnection` **没有** `executemany`，那在 cursor 上，同步连接才有。）

**`ON CONFLICT DO NOTHING` 的 `RETURNING` 在冲突时返回空**，这是幂等写入能work
的全部机制：`save_result` 靠它判断「这条结果是不是已经有人写过了」。
而 `create_run` 用的是 `ON CONFLICT (idempotency_key) DO UPDATE SET updated_at = updated_at`
—— 一次**空更新**。它不是笔误：`DO UPDATE` 会**锁住那一行并等**对方提交，
于是两个 API 副本同时收到同一个 webhook 时，输的那个也能拿到行；
`DO NOTHING` + 随后的 SELECT 做不到这一点（那个 SELECT 可能看不见还没提交的行）。

**测试文件里的 `test_*` 名字会被 pytest 收集，包括 import 进来的。**
`postgres_support.py` 里那个 helper 曾经叫 `test_dsn`，于是每个 import 它的
测试文件都多出一条「测试」：不检查任何东西、返回一个字符串，而**用例总数看起来
完全正常**，所以不会有人注意到。取名叫 `postgres_test_dsn` 是从这里来的。

**`Path.write_text` 会把 `\n` 翻译成 `os.linesep`。** Windows 上「写一个 CRLF
文件」实际会得到 `\r\r\n`（`\r` 留着，`\n` 又被翻译一次）。测试里要构造
特定的换行符就得 `newline=""` —— M4 那条「指纹不该被换行符影响」的测试
第一次跑就是被这个坑掉的（两个文件都变成了 CRLF，测不出区别）。

**Mock LLM 靠 `iter_added_lines` 从提示词里读新增行，而它需要文件头。**
`diff --git` / `---` / `+++` 三行缺一不可；只给一个 `@@` 的话它**静默返回空**，
于是 Mock 报出「0 条发现」——测试不会失败，它只是什么也没验证。
写 fixture 或测试里的假补丁时照抄 `fixtures/security_demo.diff` 的形状。

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
python tasks.py tables     # 看建了哪些表、迁移到第几版（M4 验收）
python tasks.py review     # 端到端审查一条 diff：投递 → 三个 Worker → 报告（M5 验收）
                           # 报告 JSON 走 stdout，时间线与摘要走 stderr
python tasks.py demo       # 同一份 webhook 投 3 次 → 1 run + 2 duplicate（M6 验收）
                           # 加 --follow --drop-after 3 会断开重连，验证 SSE 无缺口

python tasks.py test       # 单测（无 Docker、无密钥；Linux/CI 约 2s，Windows 约 16s）
python tasks.py test-int   # 集成测试（需 Docker 里的 Redis + Postgres；Redis 用 db 15，
                           # Postgres 用 <库名>_test 且每次会话删掉重建）
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
python tasks.py demo --follow --drop-after 3   # 断开重连，SSE 无缺口
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
- [x] M4 — Postgres 六张表 + `PostgresRunStore` + 幂等 migrate + Worker 常驻消费循环
- [x] M5 — LangGraph 图（`interrupt()` 挂起/恢复）+ 协调协程 + 超时扫描器 + 主 Agent 聚合
- [x] M6 — FastAPI 网关（HMAC 验签 + 两层去重）+ runs 接口 + SSE 带 Last-Event-ID 补齐
- [ ] M7 — GitHub 客户端 + publish 节点
- [ ] M8 — Vue SPA
- [ ] M9 — 聚合硬化 + 评测集
- [ ] M10 — 精简模式 + Render / Vercel 部署
- [ ] M11 — 可选：pgvector、LLM 冲突消解 A/B

详细计划见 `~/.claude/plans/1-agent-pr-curried-unicorn.md`。
