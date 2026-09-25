# sfly — 基于多 Agent 的分布式代码审查系统

[![CI](https://github.com/Satan508-h/sfly-code-review/actions/workflows/ci.yml/badge.svg)](https://github.com/Satan508-h/sfly-code-review/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Vue 3](https://img.shields.io/badge/vue-3-42b883.svg)](https://vuejs.org/)

> 分布式执行，集中式决策。GitHub PR 一提交，安全 / 性能 / 风格三个专业 Agent 并行开审，
> 主 Agent 汇总去重、消解冲突、重算置信度，把结论写回 PR 评论。

不依赖 Kubernetes，不依赖 Kafka。`docker compose up` 一键起。

---

## 当前状态

**M1 已完成并验证**：diff 解析、Mock LLM、JSON 修复阶梯、手写规则库、
单个 Worker 的独立命令行。**不接队列、不连数据库、不需要任何密钥**就能跑：

```bash
python -m sfly_workers --spec security --diff fixtures/security_demo.diff
```

```
── 安全审查结果 ─────────────────────────────
  文件       4 个（71 个变更行）
  规则       8 条
  状态       正常
  成本       输入 1609 tokens / 输出 893 tokens，耗时 2 毫秒，模型 mock-1
  发现       10 条：严重 4 · 高危 5 · 中危 1

  [严重]    app/db.py:8  secrets（规则 sec-secrets-001）
          疑似把凭据硬编码在源码里，会随仓库永久留存
  [严重]    app/db.py:17  sqli（规则 sec-sqli-001）
          SQL 语句用字符串拼接/格式化构造，用户输入可直接改写查询语义
  ...
```

JSON 走 stdout（可直接 `| jq`），人读的摘要走 stderr。
退出码刻意分成三个：`0` 有结果 / `2` 审查失败（模型返回的东西解析不出来）/
`3` 输入不是 diff —— 把「输入给错了」和「模型抽风了」混成一个码，
CI 就只能一律当成失败。

### 这一层解决的问题

**模型返回坏 JSON 是必然事件，不是事故。** 每一次解析失败都等于整个 Worker
的结果归零，所以 M1 的重心在这里，而不在提示词的花哨程度上。修复阶梯分五级：

| 级别 | 处理 | 真实形态 |
|---|---|---|
| L0 | 直接解析 | 干净输出（常态） |
| L1 | 花括号配平扫描 | ` ```json ` 围栏、前后有解释文字、**响应被 max_tokens 截断** |
| L2 | 清理后重试 | 尾逗号、全角引号 |
| L3 | 一次修复调用 | 单引号、Python 风格的无引号键 |
| L4 | 放弃，保留原文（8KB） | —— |

两个细节值得单独说：

* **L1 是手写字符状态机，不是正则。** `re.search(r"\{.*\}", text)` 用贪婪匹配
  在响应被截断时会跨过对象边界，把好几个 finding 连成一坨 —— 而且**不报错**。
  表现是「模型这次只报了 1 个问题」，于是你会去调提示词，而 bug 在解析器里。
* **逐元素校验，不是整包校验。** 12 条里有 1 条格式错，代价必须是 1 条而不是
  整个 Worker，产出 `status="partial"`。这也让「模型报对了 11 个」和
  「模型啥也没报」在系统里长得完全不同。

### Mock 的定位

**它不是「随机吐几条假 finding」。** 下游所有东西都建在它上面 —— 队列往返
测试、图端到端、评测基线。所以它是一个**确定性的、真的读 diff 的规则扫描器**：
逐行扫新增行、从 `@@` 头部跟踪新文件行号、命中规则时回填 `rule_id`。
同一个输入永远产出同一个输出，连故障注入也是（种子由提示词内容决定）。

故障注入（`MOCK_LLM_FAILURE_RATE`）是**验证修复阶梯真的在工作**的唯一手段 ——
七种坏法各自只能被某一级救回来，跑一遍就等于把整条阶梯走了一遍。

### 已验证

`fixtures/security_demo.diff`（真实 `git diff` 输出）在三个 Worker 上分别得到
**10 / 2 / 4 条**发现，全部落在变更行上；`fixtures/clean.diff`（参数化查询、
批量取数、有界分页的正确写法）三个 Worker **都保持沉默**。

容器内跑同一份 fixture 得到**逐字节相同**的输出（证明规则库随镜像正确分发）。

280 个单测通过；ruff + mypy strict（含 tests）全绿。

### 在此之前（M0）

`docker compose up` 起 8 个容器全部 healthy；`/api/health` 报告 Postgres 16.15 /
Redis 7.4.11 的真实版本与毫秒级延迟。依赖故障行为逐条验过：

| 操作 | `/healthz`（存活） | 容器状态 | `/api/health`（就绪） |
|---|---|---|---|
| `docker pause postgres` | 200 | 仍 healthy | **503**，3.0s 内给出「连接超时」 |
| `docker compose stop redis` | 200 | 仍 healthy | **503**，报出 `ConnectionError` 与地址 |
| 恢复 | 200 | healthy | 200，两项都回到 `ok` |

**存活与就绪是两个接口，判据刻意不同** —— 依赖挂了不该让容器被重启，
那只会把一次数据库抖动放大成一次全站重启，且 `restart: unless-stopped` 会让
日志被退避重启信息冲掉。详见 [CLAUDE.md](CLAUDE.md) 约定 #6。

**分布式部分尚未接入** —— Worker 的常驻消费循环、`review_bootstrap →
review_tasks → review_results → dead_letter` 四条流、LangGraph 图都是 M2 之后的交付物。
现在每个 Worker 只能对着一份 diff 跑一次。

进度见 [CLAUDE.md](CLAUDE.md) 末尾的清单，或前端首页。

---

## 架构

### 完整模式（本地 / `docker compose up`）

```mermaid
flowchart TB
    GH[GitHub Webhook] -->|HMAC 校验| API[api 容器<br/>FastAPI 网关]
    API -->|XADD BootstrapMessage| BS[[review_bootstrap]]
    BS -->|orchestrator-group| ORC

    subgraph ORC[orchestrator 容器 · LangGraph 状态机]
        direction TB
        IN[ingest] --> PL[plan] --> DI[dispatch] --> WA[wait<br/>interrupt]
        WA --> AG[aggregate 主 Agent] --> FI[finalize] --> PU[publish]
    end

    DI -->|XADD TaskMessage| TASKS[[review_tasks]]
    TASKS -->|security-group| WS[worker-security ×N]
    TASKS -->|performance-group| WP[worker-performance ×N]
    TASKS -->|style-group| WS2[worker-style ×N]

    WS & WP & WS2 -->|1 写 Postgres<br/>2 XADD 结果<br/>3 XACK| RES[[review_results]]
    WS & WP & WS2 -.失败/超限.-> DLQ[[dead_letter]]

    RES -->|aggregator-group| CO[coordinator 协程<br/>屏障检查 + 唤醒]
    CO -->|Command resume| WA
    PU -->|POST 评论| GHAPI[GitHub API]
    API -->|SSE| WEB[Vue 3 SPA]
    ORC & WS & WP & WS2 <--> PG[(PostgreSQL<br/>结果 + checkpoint)]
    API & ORC & WS & WP & WS2 <--> RD[(Redis<br/>Streams + 锁)]
```

### 精简模式（Render 单容器）

```mermaid
flowchart LR
    V[Vercel<br/>Vue 3 SPA] -->|HTTPS + CORS<br/>JSON 唤醒 → SSE| LT
    subgraph LT[lite 容器 · 单事件循环 · uvicorn workers=1]
        direction TB
        A[FastAPI 网关] --> B[InMemoryQueue]
        B --> C[GraphRunner<br/>同一个类]
        B --> D[WorkerPool<br/>同一个类]
        C & D --> E[RunStore → Postgres]
    end
    LT --> N[(Neon Postgres)]
```

**两种模式跑的是同一套代码。** `GraphRunner`、`WorkerPool`、全部 LangGraph 节点
是同一批对象，只有 `TaskQueue` 和 `Lock` 两个接口有两套实现。全代码库只有
`packages/bus/sfly_bus/factory.py` 一个文件读 `QUEUE_BACKEND`：

```bash
grep -rn "QUEUE_BACKEND" --include=*.py .   # 应该只返回一个文件
```

---

## 快速开始

**零密钥即可跑通全链路**（默认 Mock LLM + 本地 Docker 的 Postgres/Redis）。

```bash
git clone <this-repo> && cd sfly-code-review-system
cp .env.example .env          # 默认值就能跑
python tasks.py up            # 构建 + 启动 + 等健康检查通过
```

> Windows 上用 `python tasks.py <命令>`；Linux/macOS 上 `make <命令>` 等价。

打开 <http://localhost:5173> 应该看到状态页，四个指标全部连通。

```bash
python tasks.py health      # 依赖的真实连通性（版本号 + 延迟），不可用则非零退出
curl localhost:8000/healthz # 存活探针，永远 200
```

`/healthz` 与 `/api/health` 的分工见下面的「两个探针」。想直接看依赖挂掉时的
表现，`docker pause sfly-postgres-1` 之后再调一次 `tasks.py health`，然后
`docker unpause`。

### 端口被占用怎么办

本机已经有 Postgres / Redis 时（比如另一个项目在跑），默认端口会冲突，报错是：

```
Bind for 127.0.0.1:6379 failed: port is already allocated
```

这个报错**不会告诉你是谁占用的**。在 `.env` 里改掉即可：

```ini
POSTGRES_HOST_PORT=55432
REDIS_HOST_PORT=56379
# 改完记得同步这两行，否则宿主机上跑脚本连的是旧端口
DATABASE_URL=postgresql://sfly:sfly@localhost:55432/sfly
REDIS_URL=redis://localhost:56379/0
```

容器之间是用服务名互访的（`postgres:5432`、`redis:6379`），走 Docker 内网，
所以这两个映射**只影响你从宿主机连进去调试**，对应用本身零影响。

```bash
python tasks.py logs          # 跟踪日志
python tasks.py down          # 停止（保留数据）
python tasks.py clean         # 停止并清空数据卷
```

### 接入真实 LLM

`.env` 里改两行：

```ini
LLM_PROVIDER=deepseek
LLM_API_KEY=sk-xxxxxxxx
```

DeepSeek 走 OpenAI 兼容接口，所以换成 OpenAI、vLLM 或本地模型只要改 `LLM_BASE_URL`，
代码不用动。

---

## 想验证什么，用哪条命令

这个项目的核心主张都需要能被验证，而不是靠嘴说。每条主张对应一个可复现的操作：

| 主张 | 命令 | 应该看到 |
|---|---|---|
| 一键启动 | `python tasks.py up` | 全部容器 `healthy`，命令返回即代表可用 |
| **零密钥就能审代码** | `python -m sfly_workers --spec security --diff fixtures/security_demo.diff` | 10 条 finding，行号全部落在变更行上；`\| jq` 直接可用 |
| **干净代码上不乱报** | 同上，换成 `fixtures/clean.diff` | 三个 Worker 都返回 `{"findings": []}` |
| **坏 JSON 不会毁掉结果** | `MOCK_LLM_FAILURE_RATE=1.0` 再跑上一条 | 每一次调用都返回坏 JSON，仍然出结果（修复阶梯接住了） |
| 依赖真实可达 | `python tasks.py health` | Postgres / Redis 的**版本号**与毫秒延迟，不是照抄配置 |
| 依赖挂了不误伤 | `docker pause sfly-postgres-1` | `/healthz` 仍 200、容器仍 healthy、`/api/health` 503；`docker unpause` 后自动恢复 |
| Worker 水平扩展 | `python tasks.py scale 3` | `worker-security` 变成 3 个副本，日志里出现 3 个消费者实例 |
| Worker 猝死不影响结果 | `python tasks.py kill-worker` | run 照样跑完，UI 显示「1 个 Worker 降级」徽章 |
| Webhook 幂等 | 同一 payload 连投 3 次 | 1 个 run + 2 个 `duplicate` 响应 |
| 断点恢复 | `docker restart sfly-orchestrator-1` | 从 Postgres 的 checkpoint 续跑，不重复发评论 |
| Redis 重启不丢任务 | `docker restart sfly-redis` | 扫描器按 `attempt+1` 重派，消息排空 |
| Postgres 不可达不丢结果 | `docker pause sfly-postgres` | 消息堆在 PEL 里；`unpause` 后排空（证明「先落库再 ack」的顺序） |
| 质量可被度量 | `python tasks.py eval` | `reports/eval-<sha>.md`：精确率 / 召回率 / 误报率 / 单次成本 |

---

## 几个刻意的技术选择

这些不是随手选的，每一条都对应一个具体的失败模式。面试时被追问的就是这些。

**幂等靠数据库唯一约束，不靠 Redis SETNX。**
`worker_results` 的 `PRIMARY KEY (task_id, worker_type)` 才是保证。Redis 的
`SETNX` 只是省 token 的快路径 —— 它会过期、会随 Redis 重启丢失、也可能在后续
写库失败时已经被设上。约束不会。

**失败也是结果。**
Worker 放弃之前必须先发一条 `status=failed` 的结果再 ack。否则 `wait` 节点的
屏障永远闭合不了，整个 run 挂到超时。这条让 `docker kill` 那个演示变成真的，
而不只是理论上可恢复。

**图暂停期间 orchestrator 崩了，恢复靠一条 SQL 查询。**
`wait` 节点用 LangGraph 的 `interrupt()` 暂停并退出执行，进程不再持有该 run 的
任何内存状态。超时扫描器每 15 秒查一次 `due_runs()`，把过了 `deadline_at` 还停在
`dispatched`/`waiting` 的 run 捞出来唤醒。**任何可能卡住的状态都必须是一行带
`deadline_at` 的记录** —— 写不成一条 `SELECT` 的恢复查询，说明状态藏在了会丢的地方。

**去重不用向量库，用 `rapidfuzz` + 并查集。**
确定性、可复现（评测需要）、无模型下载、微秒级。同 Worker 阈值 0.75，
跨 Worker 0.55。

**冲突消解不用 LLM，用确定性规则引擎。**
LLM 裁判会引入非确定性，直接毁掉评测的可复现性。四条规则顺序匹配：
职责域优先 → 越界降级 → 证据裁决 → 标记待人工。`CONFLICT_RESOLVER=llm`
保留为可测量的 A/B 对照，不做默认。

**RAG 用手写规则库 + BM25，不用向量库。**
60–120 条手写规则（引 OWASP Top 10 / CWE Top 25 / Google 风格指南）。
规则库本身是可被审阅的作品。向量检索列为可选升级。

**永不发 `APPROVE`。**
机器人审批人类 PR 是策略漏洞。只发 `REQUEST_CHANGES` 或 `COMMENT`。

**存活与就绪是两个接口，判据不同。**
`/healthz` 决定 Docker 要不要重启容器，**永不探测依赖**；`/api/health` 决定要不要
把流量打过来，依赖挂了返回 503。合成一个接口就必然二选一：要么在数据库抖动时
重启一堆无辜的容器（而 `restart: unless-stopped` 会把它们拖进退避循环，
真正的错误被重启日志冲掉），要么让负载均衡把流量送进一个干不了活的服务。
后者听起来更无害，直到你发现健康检查绿灯、页面却在报错，而日志里什么都没写。

**健康探测走一次性连接，不走连接池。**
`pool.connection(timeout=N)` 只限制「等池子分配连接」的时间；拿到连接之后，
后面的查询**没有任何超时**。`docker pause` 之下 `SHOW server_version` 会永远阻塞
—— 实测把 `/api/health` 整个挂死，而 `docker pause postgres` 正是上面表格里
承诺要演示的场景。现在两种依赖的探测都新开一条一次性连接，外面套
`asyncio.wait_for`：超时取消的是一条马上要销毁的连接，不牵连池子。

---

## 技术栈

| 层 | 选择 | 为什么 |
|---|---|---|
| Agent 编排 | LangGraph 1.2 + `AsyncPostgresSaver` | 自带 checkpointer，`interrupt()` 的断点恢复是内建能力 |
| API | FastAPI + `sse-starlette` | SSE 是项目要求；`sse-starlette` 处理了断连检测和代理缓冲 |
| 队列 | Redis Streams 消费者组 | 比 Kafka 轻得多，且天然支持「同组竞争消费」的水平扩展语义 |
| 状态 | PostgreSQL（本地 Docker / Neon 云） | 同时是 LangGraph 的 checkpointer |
| 驱动 | `psycopg` v3（**不是 asyncpg**） | LangGraph 的 Postgres checkpointer 用 psycopg，混用会带来两份连接池 |
| 前端 | Vue 3 + Element Plus + Vite + TS | 中文资料最多，Element Plus 的表格/时间线组件省事 |
| LLM | DeepSeek（OpenAI 兼容接口） | 成本极低；接口兼容意味着 provider 可换 |
| 去重 | `rapidfuzz` | 见上 |
| 检索 | `rank_bm25` | 见上 |
| 打包 | `uv` workspace（单仓多包） | 一份 lock，每个服务只装自己的依赖闭包 |

---

## 目录结构

```
apps/
  api/           FastAPI 网关：HMAC 校验、投递去重、runs 接口、SSE
  orchestrator/  LangGraph 图 + coordinator 协程 + 超时扫描器
  workers/       一套代码三种部署：--spec {security|performance|style}
  lite/          单事件循环，一个进程跑完整个系统（Render 用）
packages/
  shared/        领域契约、配置、ID、日志、异常
  bus/           TaskQueue / RunStore / Lock 协议 + 两种实现
  agent-core/    LLM 抽象与结构化输出、RAG、聚合算法、GitHub 客户端
web/             Vue 3 SPA
infra/           postgres init、redis conf、nginx conf、GitHub 限流桩
fixtures/        diff 样例、webhook payload、大 PR
scripts/         运维与演示脚本
tests/           unit / integration / e2e / eval
reports/         评测报告（提交进仓库）
```

数据流：`review_bootstrap → review_tasks → review_results → dead_letter`

API 不直接写 `review_tasks` —— 文件风险排序和规则检索由编排层的 `plan` 节点负责，
所以 Worker 收到的任务已经带着检索好的规则，Worker 因而保持无状态且不依赖 RAG。

---

## 已知限制

写在前面而不是藏在最后：一个未被说明的限制会被当成经验不足，一个被说明的限制
则是严谨。

- **GitHub 真实二级限流只能用桩模拟。** `infra/github/stub_server.py` 模拟
  429 → 退避 → 201 的序列。桩是模型，不是真相。
- **Render 冷启动无法自动化测试。** 免费版休眠后首次请求约需 60 秒（Render 唤醒
  约 60s + Neon 唤醒约 1s）。需要人工冒烟测试并记录实测耗时。
- **Neon 免费版空闲 5 分钟挂起且无法关闭。** 连接池必须传
  `check=AsyncConnectionPool.check_connection`，否则挂起后的第一次查询会拿到死连接
  直接抛错。代码里已处理，但这个约 0.5–1.5 秒的首次查询惩罚无法消除。
- **Neon 免费版 0.5 GB 上限**，而 LangGraph 的 checkpoint 按 thread 无界增长。
  需要保留策略任务定期清理 14 天前的 checkpoint 与 `run_events`。
- **DeepSeek 在真实负载下的行为未验证。** 评测集只有 30 个 PR 的规模。
  OpenAI 兼容 provider 的**请求契约和错误翻译有单测覆盖**（发什么、怎么解析响应、
  超时/401/429 分别怎么报），但「真实模型返回的东西修复阶梯能不能救回来」
  只有 M9 的评测集能回答。Mock 的故障注入是对这一点的**模拟，不是证据**。
- **Mock LLM 是一个启发式扫描器，它的误报和漏报不代表模型的表现。**
  它的规则表是正则 + 一处循环上下文分析。下游的队列往返测试、图端到端、
  评测基线都建在它上面，但它的准确率**不能**用来推断真实模型的准确率 ——
  这是两件事，混淆它们会让评测数字失去意义。
- **`--max-files` 目前是「按 diff 里的顺序取前 N」，不是按风险排序。**
  文件风险排序属于编排层 `plan` 节点的职责（M5），因为它需要整个 PR 的上下文
  （哪些是核心模块、哪些是测试）。在那之前，截断是**看得见**的
  （摘要里会写「已按上限截取」），但不是**聪明**的。
- **`review_results` 流的裁剪（MAXLEN）** 在 M5 验证通过前不会开启。近似裁剪可能
  删掉「已投递未 ACK」的消息，导致 `XAUTOCLAIM` 取回空 payload。
- **Windows 上原生跑 Python 时必须换事件循环。** Windows 默认的
  `ProactorEventLoop` 不支持 `add_reader`，而 psycopg v3 的异步模式正是靠它实现的；
  不换的话进程能起来、日志正常，然后在第一次查询时抛
  `Psycopg cannot use the 'ProactorEventLoop' to run in async mode` ——
  看起来像代码写错了，其实是平台默认值不对。所有入口已统一走
  `sfly_shared.aio.run()`，但它只在**非容器**运行时才是关键路径
  （容器里是 Linux，两种循环的差异根本不存在）。uvicorn 在 Windows 上把循环工厂
  写死成 Proactor，所以 `sfly_api/__main__.py` 没有用 `uvicorn.run()`，
  而是自己驱动 `Server.serve()` —— 这是为了让"本地能不能跑"和"线上跑什么"
  保持一致，不是为了性能。
- **Windows 本地单测约 16 秒，CI 上约 2 秒。** 不是回归：探测测试连的是
  `127.0.0.1:1`，Linux 上拒绝连接是即时的，`SelectorEventLoop` 在 Windows 上
  要花约 2 秒才报 `ECONNREFUSED`。这一条同时影响了 `_PING_CONNECT_TIMEOUT_S`
  的取值（设成 2 秒会被平台自身的延迟抢先触发，把有用的
  `ConnectionRefusedError` 换成没有信息量的 `TimeoutError`）。

### 明确不做

Kubernetes、Kafka、为去重引入 embedding、索引 PR 自己的代码仓库、多查询检索、
cross-encoder 重排、独立的向量数据库容器、按 token 流式输出（批处理审查没有收益）。

---

## 评测

`python tasks.py eval` 会在 `reports/` 下生成带 git sha 的报告，**并提交进仓库**。
改了 prompt 或规则之后，PR 里就能看到精确率和召回率的 diff。

评测集 30 个 PR，三组人群刻意不同：

- **~10 个真实漏洞**：取有 CVE 修复 commit 的公开仓库代码，**回退修复来重建漏洞**。
  这样得到的是无人能质疑的 ground truth。
- **~15 个手写 diff**：注入覆盖三个 Worker 的 bug。
- **~5 个干净 diff**：**没有预期发现**，用来测误报率。多数人完全跳过这一组，
  这正是他们的精确率数字毫无意义的原因。

指标：严格/宽松两档精确率、召回率、F1、误报率、单 PR 成本、p50/p95 延迟、
缓存命中率、对照单 Agent 基线的有效开销比、1 Worker vs 3 Worker 消融。

> **关于「Agent 间通信 token 冗余 < 15%」**
>
> 这个指标作为常见宣传口径，实测会失败 —— 三个 Worker 会把 diff 各发一遍，
> 输入 token 显著高于单 Agent 基线。本项目改为报告可测量的两个口径：
> **缓存命中后的有效开销比**，以及**每多发现一个真实问题的边际成本**。
> 数字不好看，但站得住。

评测用 `temperature=0` 并把原始请求响应用 `vcrpy` 录到 `tests/eval/cassettes/`，
因此可以离线、无密钥、零成本重跑 —— 否则每次调 prompt 都要花钱，然后你就会停止迭代。

---

## 许可

MIT
