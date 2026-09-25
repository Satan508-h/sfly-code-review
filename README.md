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

**M5 已完成**：LangGraph 图（七个节点）跑通了，`wait` 节点的 `interrupt()` 真的会
挂起、真的能恢复，编排层有了协调协程和超时扫描器，主 Agent 的聚合（去重 / 置信度
重算 / 阻断决策 / 评论渲染）**全确定性、零 LLM 调用**。验收是一条命令：

```bash
python tasks.py review
```

```text
── 时间线（12 条事件）─────────────────────────
  #23  run.created         files=4 head_sha=fb7353185e1b-… pr_number=1
  #24  node.finished       node=plan planned_workers=['security','performance','style'] rules=8
  #25  worker.dispatched   worker_type=security message_id=1790341750660-0
  #30  worker.result       worker_type=style findings=4 status=ok
  #31  aggregate.done      findings=14 suppressed=2 degraded=False tokens=6216
  #32  node.finished       node=finalize decision_reason=secrets_found
  #33  publish.done        posted=False reason=github_client_not_implemented
  #34  run.finished        status=published duration_ms=49

── 审查结果 ─────────────────────────────────────────
  状态       published
  结论       🔴 建议修改后再合并（secrets_found）
  发现       14 条：严重 4 · 高危 5 · 中危 3 · 低危 2
             另有 2 条置信度不足，只入库不发布

  [严重]    app/db.py:17  sqli  置信度 60%  来自 安全
          SQL 语句用字符串拼接/格式化构造，用户输入可直接改写查询语义
```

报告 JSON 走 stdout（`python tasks.py review | jq` 直接用），时间线和中文摘要走 stderr。

下面按里程碑倒序排列，最近完成的在最前。

---

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

373 个单测 + 59 个集成测试通过；ruff + mypy strict（含 tests）全绿。

### M5 已完成：LangGraph 图 / 断点恢复 / 主 Agent 聚合

```
ingest → plan → dispatch → wait → aggregate → finalize → publish
```

七个节点各管一件事，每个都有自己的失败模式：`ingest` 是唯一会被重复执行的节点
（bootstrap 重投），`plan` 是唯一可能提前结束整个 run 的，`dispatch` 是唯一有
不可撤销副作用的，`wait` 是唯一会挂起的。

#### `interrupt()`：全项目唯一的高风险项

`wait` 节点在屏障没闭合时调 `interrupt()` —— 它会 checkpoint 然后**退出图执行**，
进程里不再持有这个 run 的任何状态。唤醒由**图之外**的协调协程完成：它消费
`review_results`，查一次屏障，闭合了就 `ainvoke(Command(resume=...))`。

**判断依据是数据库，不是唤醒信号。** 节点被唤醒后 LangGraph 会从头重跑它，
而它做的第一件事是再查一次 `worker_results` —— 所以协调协程、超时扫描器、
甚至手工 resume，走的都是同一条路径、得到同一个结论。唤醒信号里带什么值都不影响
结果（`interrupt()` 的返回值在这个节点里根本没用）。

两条命保着这套机制：

| 机制 | 救的是 |
|---|---|
| **超时扫描器**（每 15 秒） | `due_runs()` 捞出过了 `deadline_at` 还在 `dispatched`/`waiting` 的 run 并唤醒。**图挂起期间 orchestrator 崩了，恢复靠的是这一条 SQL，不是内存里的定时器** |
| **唤醒选举**（`SETNX resume:{task_id}`） | 扫描器在每个副本里都跑，同一个 run 会被多个副本同时盯上；而 LangGraph 不阻止同一个 thread 被并发 invoke。锁只省重复劳动，**它不是正确性机制** —— 真正的兜底是 M7 的两道防重复评论闸 |

逃生开关是 `WAIT_STRATEGY=poll`：节点签名完全相同，原地轮询。它有已知代价 ——
轮询期间整条消费协程被占住，一次只能推进一个 run —— 所以它是开关，不是默认值。

#### 屏障读数据库，不读消息流

`wait` 查的是 `worker_results` 表。这不是实现细节，是**唯一正确的选择**：
Worker 是「先写库、再 `XADD`」（约定 #1），所以编排器被唤醒时结果一定已经在了。
反过来会有真实的竞态 —— `XADD` 先到、去查库查不到、屏障看起来没闭合，
然后那条消息被 ack 掉，再也没人来叫醒这个 run。

#### 主 Agent 的聚合：全确定性、零 LLM 调用

`results → 合并 → 置信度重算 → 分档 → ReviewReport`，整条链路上除了 Worker 本身
一个模型都不调 —— 因为评测要可复现：同一批结果跑一百遍必须得到一模一样的报告，
否则「改了聚类阈值，精确率涨了 3%」就说不清是改动带来的还是模型抖动带来的。

置信度**重算而不是照抄**，因为模型自报的数有两个已知偏差（系统性偏高、跨 Worker
不可比）：

```
c = 严重度先验 × (0.55 × 模型自报 + 0.25 × 一致度 + 0.20 × 跨 Worker 度) + 0.10 × 有规则依据
```

一致度是**饱和**的（1 / 2 / 3 个来源 → 0 / 0.49 / 0.74）：从「没人印证」到
「有人印证」是最值钱的一步，之后迅速递减。低于 `0.35` 的**入库但不发布** ——
评测需要它们来测量这道闸砍掉了多少召回，只存发布出去的话阈值就只能盲调。

阻断决策是规则引擎，**永不发 APPROVE**（机器人审批人类 PR 是策略漏洞）：
命中 `secrets` 直接拦（不看严重度也不看置信度，因为代价不对称）、
高危类目的 `critical` 且置信度 ≥ 0.60、或者 ≥ 3 条高危且置信度 ≥ 0.70。

#### 过程中改掉的东西

| 现象 | 真因 |
|---|---|
| 报告里 `degraded=True` 但 `missing_workers=[]` | 「谁没上报」和「谁没产出可用结果」是**两个问题**。`wait` 会给超时的 Worker 补写 failed 结果（约定 #2），所以 aggregate 跑的时候「谁没上报」永远是空集 —— 前端那个降级徽章找不到任何一个可以显示的名字。现在按「没有可用结果」算，顺带覆盖了「上报了一条失败结果」那一类 |
| `worker.result` 事件排在 `run.finished` **后面** | 事件由协调协程写，而图和协调协程是并发的两条路径 —— 图判断屏障读的是数据库，所以「三条结果都在库里了、图已经 aggregate 完、协调协程才开始消费第一条消息」完全可能。**那不是排序问题，是写事件的人站错了位置**：这件事的因果起点是「Worker 写完了结果」，就该由 Worker 在那一刻记下来 |
| `comment_chars` 在 finalize 和 publish 两处差 1 | `_Contract` 开了 `str_strip_whitespace=True`，评论正文结尾那个换行存进 jsonb 再读回来时被吃掉了。渲染时干脆不留 —— 让它一开始就等于最终形态 |
| 报告里有 emoji 时 CLI 以非零码退出 | Windows 上重定向到文件用的是 cp936，`🤖` 编码不了 → `UnicodeEncodeError`，**而报告已经生成好了**。这套判断（`isatty()` 决定编码）原本只写在 Worker 的 CLI 里，现在抽成了 `sfly_shared.console` 给两个入口共用 |
| `python tasks.py review > report.json` 得到的不是合法 JSON | `tasks.py` 的 `run()` 往 **stdout** 打了一行 `$ <命令>`，于是 `json.loads` 在第二个字节上失败，报错指向 JSON 语法。这违反项目自己的约定（stdout 只留给程序输出），现在那行走 stderr |
| pydantic 对象存进 checkpoint 时每次读都warn | `JsonPlusSerializer` 能把契约对象存回来，但会打印「Deserializing unregistered type … This will be blocked in a future version」。所以**图状态里只放 JSON**，节点边界上 `model_validate` —— 顺带让 `SELECT checkpoint FROM checkpoints` 直接可读 |

#### 一处刻意的顺序

`publish` **先写状态、后写事件**。两次写库不可能原子，所以必然有一个窗口，
而两种顺序的失败方向不一样：状态先写的话，崩在中间的表现是「run 读作已完成、
时间线少了最后一条」—— 客户端跟着 `run.finished` 事件走，等不到就读一次状态，
发现已经完成，是安全的失败方向。反过来会让 run 永远停在 `aggregating`，
而**没有任何东西能唤醒它**（扫描器的 `due_runs` 只看 `dispatched`/`waiting`）。

### M4 已完成：Postgres schema / 仓储 / 迁移 / Worker 消费循环

```bash
python tasks.py tables     # 六张表 + schema_version，见下
```

```
                List of relations
 Schema |      Name       | Type  | Owner
--------+-----------------+-------+-------
 public | findings        | table | sfly
 public | llm_calls       | table | sfly
 public | review_reports  | table | sfly
 public | review_runs     | table | sfly
 public | run_events      | table | sfly
 public | schema_version  | table | sfly
 public | worker_results  | table | sfly

 version | name |          applied_at
---------+------+-------------------------------
       1 | init | 2026-09-25 12:15:03.412+00
```

六张业务表各自对应一件事：`review_runs`（一次审查一行，`deadline_at` 是所有恢复
逻辑的主干）、`worker_results`（幂等靠它的复合主键）、`findings`（逐条展开，
供评测统计）、`review_reports`（最终报告 jsonb + 冗余的汇总列）、`run_events`
（SSE 的权威来源）、`llm_calls`（成本账）。

**建表的是应用，不是 `init.sql`。** `infra/postgres/init.sql` 里只有扩展，
表结构由每个进程启动时的幂等 `migrate()` 创建 —— 因为 Neon 上根本执行不到
那个 init 脚本，「本地能跑、线上缺表」是这类项目最常见的部署事故。

#### 三件不显然的事

| 事 | 为什么 |
|---|---|
| **五个容器同时建表**是常态 | api / orchestrator / 三个 Worker 启动时都调 `migrate()`，靠 `pg_advisory_xact_lock` 串行化。没有它，表现是 `schema_version` 主键冲突或 `CREATE TABLE` 竞态 —— 然后容器进重启循环，看起来像数据库有问题 |
| **漂移要炸，连不上不能炸** | 已应用的迁移被改过（sha256 对不上）→ 启动失败，因为代码期待的结构和库里的不是一回事，继续跑就是往错的结构上写数据。而数据库连不上时**记一条 error 继续** —— M0 定下的规矩：进程要能起来在 `/api/health` 里说清楚哪里坏了 |
| **失败也要写库**（约定 #2 的落地） | Worker 放弃前先补一条 `status="failed"` 的结果，否则 `wait` 节点的屏障永远闭合不了。`completed_workers` 因此**不带 status 过滤** —— 写成「哪些 Worker 成功了」，一个 Worker 失败就会让整个 run 挂到超时 |

#### Worker 的常驻循环：三行的顺序

```python
await store.save_result(result)  # 1. 先落库
await queue.publish_result(result)  # 2. 再唤醒编排器
await handle.ack()  # 3. 最后离开 PEL
```

反过来两种写法各有各的灾难：先 `XADD` 后写库 → 编排器被唤醒去读一个还不存在的
结果，屏障检查失败；先 `XACK` 后写库 → 结果同时从 PEL 和数据库消失，**永久丢失**。

而 `save_result` 失败时**不 ack** 也是这条约定的一部分：消息留在 PEL 里，
等 Postgres 恢复后被 `reclaim()` 捞回来重跑，两处都不丢。这几条都有测试钉着
（`tests/integration/workers/test_consume_loop.py`，对着真 Redis + 真 Postgres 跑）。

每个 Worker 还跑一条回收协程（`RECLAIM_INTERVAL_S`，默认 30 秒一次
`XAUTOCLAIM`）—— 这是「副本猝死」能被兜住的那一半，另一半是幂等写入。

#### 过程中改掉的东西

| 现象 | 真因 |
|---|---|
| `python tasks.py up` 一直抛 `TypeError` | `cmd_up` 一直在给 `_compose` 传 `timeout=`，而那个参数从没被接上。**最常用的那条命令坏了，没有任何东西发现它** —— 它不在任何自动化路径上，而手工跑它的人只会以为是自己环境的问题。现在两层的超时都补齐了（compose 自己的 `--wait-timeout` + subprocess 兜底） |
| 迁移器第一次跑就 `KeyError: 0` | 池子的连接设了 `dict_row`（按列名取值），而 `_applied_versions` 写的是 `row[0]`。**行工厂会传染给每一个在它上面执行的 helper** —— 所以现在在自己开 cursor 的地方显式声明它 |
| Worker 一启动就 `AttributeError: executemany` | `AsyncConnection` **没有**这个方法（同步连接才有），它得在 cursor 上调用 |
| 「指纹不该被换行符影响」的测试不成立 | `Path.write_text` 会把 `\n` 翻译成 `os.linesep`，Windows 上于是把「写 CRLF」变成了 `\r\r\n`。测试里构造特定换行符必须 `newline=""` |

另外两处**只有对着真库才会暴露**的设计细节，写进了代码注释：`ON CONFLICT DO NOTHING`
的 `RETURNING` 在冲突时返回空（幂等写入靠的就是它）；而 `create_run` 用一次
**空更新**而不是 `DO NOTHING` + SELECT —— 后者在两个副本同时收到同一个 webhook 时
可能看不见对方还没提交的那一行。

### M3 已完成：Redis Streams（队列 / 锁 / 回收）

M2 交出了「第二种实现」，M3 交出的是**第一种实现的真实版本** ——
`RedisStreamsQueue`（`XADD` / `XREADGROUP` / `XACK` / `XAUTOCLAIM` / `MAXLEN`
/ 死信 / 重试计数）和 `RedisLock`（`SET NX PX` + Lua 比对释放）。

关键不在于写了多少行，而在于**证据现在是被执行出来的**：

```
tests/contracts/queue_contract.py       ← 一份契约（14 条）
tests/contracts/lock_contract.py        ← 一份契约（6 条）
   ├── tests/unit/bus/test_memory_queue.py          → InMemoryQueue（无 Docker）
   ├── tests/unit/bus/test_memory_lock.py           → InMemoryLock
   ├── tests/integration/bus/test_redis_queue.py    → RedisStreamsQueue（真 Redis）
   └── tests/integration/bus/test_redis_lock.py     → RedisLock
```

**两个后端继承同一个类，一行断言都没有为 Redis 放宽。** CI 里有一个专门的
`contract` job 起一个真 Redis 跑这条路径 —— 因为「只有一种后端在跑」这件事
不会报错，它只会让测试全绿、覆盖率不降，而证据悄悄消失。

M3 找到并修掉的三处问题，都不是写代码时能想到的：

| 现象 | 真因 |
|---|---|
| 内存实现 `reclaim()` 返回 1，Redis 返回 0 | 内存实现给被裁掉的条目留了「墓碑」，而 Redis 里那条消息**已经不存在了**，没有东西可以重投。改的是内存实现，见 `memory.py` 的 `_purge` |
| 契约里「无参回收覆盖全部组」在内存实现上漏了 `review_bootstrap` | 编排器在 `ingest` 中途崩掉时，那条 bootstrap 会永远躺在 PEL 里 —— 症状是「这个 run 再也不动了」，没有任何日志 |
| `InMemoryQueue.start()` 的顺序：先判 `_started` 再判 `_closed` | 「start → close → start」会命中捷径、安静地返回，于是一个已经关闭的队列看起来启动成功了 |

另外实测纠正了一个**我一开始读错的现象**：第一次跑 `redis-cli` 时看到
「`XTRIM` 之后 `XPENDING` 少了一条」，于是照着「Redis 在裁剪时会清 PEL」去写了
内存实现。真相是那段脚本里紧跟着的 `XAUTOCLAIM` 干的 —— `XTRIM` 只从流里删条目，
PEL 里那个 id 会悬着，直到下一次回收才被清掉。这类错误没有报错、没有异常，
只有一条会红的测试能挡住它，所以现在有这么一条。

**可运行的演示**（不需要全栈，只要一个 Redis）：

```bash
python tasks.py demo-reclaim
```

它跑的是「`--scale` 与 `docker kill` 那些场景」依赖的机制本身：两个消费者竞争
同一个消费者组 → 一个副本在处理中途猝死（不 ack）→ 现场只剩 PEL 里一条没人认领的
记录 → 同伴 `reclaim()` 抢回来 → **重投时 `attempt` 变成 2**。

### M2 已完成：传输层的内存实现与共用契约

* `InMemoryQueue` / `InMemoryLock` —— 精简模式的实现
* `tests/contracts/` —— 两个后端**共用**的行为契约（M3 已把 Redis 接进来）
* 发现指纹（`sfly_agent/aggregate/fingerprint.py`）—— 去重快速路径的键，
  全确定性，跨进程稳定

`InMemoryQueue` **不是「一个 asyncio 队列加几个方法」**，否则契约就只能断言
「能收发消息」。它实现的是真正的 Streams 语义：

| 语义 | 为什么不能省 |
|---|---|
| 每组独立游标，**发布是扇出** | 组是独立游标而不是分工：一条任务三个组各读一遍，各自按 `worker_type` 过滤并 ack。做成「谁先抢到归谁」的话，`--scale` 和 lag 口径全错 |
| 每组的 PEL + 投递计数 | `attempt` 是死信判定的依据，必须跨回收保留 |
| 回收是「放回可投递」而非直接返回 | `reclaim()` 返回计数，所以消费者的消息来源只有一个 |
| 裁剪是从所有人的视角消失 | 被裁掉的条目既不会被投递，也不会永久卡在 PEL 里。清理**时机**两个后端不同（内存即时、Redis 惰性），但可观察的结果必须一样 |

**契约测试自己也做了验证**：拿两个故意坏掉的实现跑了一遍 ——
一个让每次投递都重复一遍、一个让 `ack()` 变成空操作 —— 各自都被抓住
（`test_one_group_delivers_each_message_to_exactly_one_consumer`、
`test_acked_message_is_not_reclaimed`）。一份永远通过的契约测试比没有更糟，
因为它让人以为有人在守。

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

**还差什么**：传输层两种实现都齐了（M2/M3），Worker 的常驻消费循环与 Postgres
仓储也齐了（M4）—— 投递顺序铁律「先落库、再 XADD、最后 XACK」现在是代码，
而且有对着真 Redis + 真 Postgres 的测试。还差的是**把它们串起来的那一层**：

* `review_bootstrap → review_tasks → review_results → dead_letter` 四条的端到端
  流转、屏障闭合、`docker kill` 之后 run 照样跑完，要等 **M5** 的 LangGraph 图
  与 coordinator —— 现在还没有东西往 `review_tasks` 里写（`dispatch` 是图的一环）

所以现在 Worker 收到任务会真的处理并写库（手工投一条就能看到），但一条完整的
run 还走不通；`--scale` 与 `docker kill` 那两个场景的**机制**已经验证过了，
只是还没有一条真正的 run 从它们上面走过去。

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
| **两种拓扑共用一套代码** | `python tasks.py test` + `python tasks.py test-int` | **同一份**契约（`tests/contracts/queue_contract.py`）在内存后端与真 Redis 上各跑一遍 —— 14 条 + 6 条，一条都没有为哪一端放宽。集成层还会在真 Postgres 上跑仓储/迁移/消费循环 |
| 依赖真实可达 | `python tasks.py health` | Postgres / Redis 的**版本号**与毫秒延迟，不是照抄配置 |
| 依赖挂了不误伤 | `docker pause sfly-postgres-1` | `/healthz` 仍 200、容器仍 healthy、`/api/health` 503；`docker unpause` 后自动恢复 |
| **表是应用建出来的** | `python tasks.py tables` | 六张业务表 + `schema_version`（第 1 版已应用）。空库上也能建 —— 每个容器启动时都跑一遍幂等 `migrate()` |
| **副本猝死，同伴接手** | `python tasks.py demo-reclaim` | 4 条任务被两个副本瓜分 → 一个副本中途消失 → PEL 里留下 1 条没人认领的记录 → `reclaim()` 抢回 → 重投的 `attempt` 是 2（不需要全栈，只要一个 Redis） |
| Worker 水平扩展 | `python tasks.py scale 3` | `worker-security` 变成 3 个副本，**同一个消费者组里三个消费者在竞争**（常驻循环 M4 已就绪，`XINFO CONSUMERS` 数得出来） |
| **Worker 真的在写库** | `docker compose logs worker-security \| grep worker.consuming` | 每个副本报出消费者组、并发数、回收间隔；收到任务时按「存库 → 发结果 → ack」处理（集成测试逐条验证这个顺序） |
| **一条命令跑完整条链路** | `python tasks.py review` | 一张图从 bootstrap 跑到报告：三次派发、三个 Worker 上报、聚合、阻断决策。stdout 是报告 JSON（`\| jq` 直接可用） |
| **图会挂起，也能被唤醒** | `python tasks.py review` 的 stderr 时间线 | 若某个 Worker 慢一步，日志里会出现 `node.wait_suspend`，然后是协调协程的 `coordinator.barrier_closed` 把它叫醒 —— 中间进程不持有这个 run 的任何状态 |
| **断点恢复靠一条 SQL** | `RUN_DEADLINE_S=10 python tasks.py review`（不启动 Worker） | 15 秒内扫描器捞出这个 run 并唤醒，`wait` 走超时分支给三个掉队的 Worker 各补一条 failed 结果，报告带降级徽章 |
| 幂等：同一份 diff 只审一次 | `python tasks.py review --replay` | 复用同一个 run（幂等键 = `repo:pr:head_sha`），打印上一次的报告而不是重新审查 |
| Worker 猝死不影响结果 | `python tasks.py kill-worker` | run 照样跑完并显示降级徽章（M5 起屏障能被编排器闭合了） |
| Webhook 幂等 | 同一 payload 连投 3 次 | 1 个 run + 2 个 `duplicate` 响应 |
| 断点恢复 | `docker restart sfly-orchestrator-1` | 从 Postgres 的 checkpoint 续跑，不重复发评论 |
| Redis 重启不丢任务 | `docker restart sfly-redis` | 扫描器按 `attempt+1` 重派，消息排空 |
| Postgres 不可达不丢结果 | `docker pause sfly-postgres` | 消息堆在 PEL 里；`unpause` 后排空（证明「先落库再 ack」的顺序，集成测试里有一条专门钉它） |
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
  orchestrator/  graph.py 装配图；nodes/ 七个节点各一个文件
                 runner.py 消费 bootstrap；coordinator.py 屏障 + 唤醒
                 sweeper.py 超时扫描；checkpointer.py **自己一个连接池**
  workers/       一套代码三种部署：--spec {security|performance|style}
                 pool.py 是消费循环本体 —— 一个容器一条 lane，或者一个进程
                 三条协程（精简模式），**同一份代码**
  lite/          单事件循环，一个进程跑完整个系统（Render 用）
packages/
  shared/        领域契约、配置、ID、日志、异常、console（编码）
  bus/           TaskQueue / RunStore / Lock 协议 + 两种实现
                 postgres.py 里是池子 + 仓储；migrations/001_init.sql 是六张表
                 （**迁移 SQL 是数据文件**，靠 Dockerfile 的 COPY packages 进镜像）
  agent-core/    LLM 抽象与结构化输出（含 pricing.py 的价格表）、RAG、
                 state.py 图状态、risk.py 文件风险排序、
                 aggregate/ 主 Agent 的聚合（fingerprint / confidence /
                 decision / pipeline / render，**全确定性、零 LLM 调用**）
web/             Vue 3 SPA
infra/           postgres init（只有扩展，表由应用建）、redis conf、nginx conf、限流桩
fixtures/        diff 样例、webhook payload、大 PR
scripts/         运维与演示脚本
tests/           contracts/（两个后端共用的行为契约，被 unit 与 integration 同时继承）
                 factories.py（领域对象工厂，三层测试共用同一批形状）
                 unit / integration / e2e / eval
reports/         评测报告（提交进仓库）
```

数据流：`review_bootstrap → review_tasks → review_results → dead_letter`

API 不直接写 `review_tasks` —— 文件风险排序和规则检索由编排层的 `plan` 节点负责，
所以 Worker 收到的任务已经带着检索好的规则，Worker 因而保持无状态且不依赖 RAG。

---

## 已知限制

写在前面而不是藏在最后：一个未被说明的限制会被当成经验不足，一个被说明的限制
则是严谨。

- **`publish` 节点现在不发任何东西。** GitHub 客户端是 M7 的交付物，所以
  `publish.done` 事件里明写 `posted: false`，`review_runs.github_comment_id`
  保持为空 —— **没有这个 id 的 run 就是没发过评论**，UI 不该显示「已评论」。
  报告本身是完整的（正文已经在 `review_reports.comment_body` 里），
  M7 只负责把它投出去。
- **聚合只做指纹精确合并，还没有相似度聚类。** 两个 Worker 用不同措辞说同一处
  问题、或者模型这次报第 10 行下次报第 12 行时，指纹不同 —— 而它们其实是同一件事。
  那一步（并查集 + rapidfuzz，同 Worker 0.75 / 跨 Worker 0.55）是 M9。
  指纹那一步**永远不会被替换掉**（它是并查集的快速路径），M9 加的是它后面的兜底。
- **`conflicts` 恒为空。** 冲突消解（同路径 + 邻近行号 + 不同 Worker + 严重度差 ≥ 2）
  和聚类一起排在 M9。前端要在空列表上正常工作。
- **`WAIT_STRATEGY=poll` 一次只能推进一个 run。** 轮询期间整条消费协程被占住，
  而 `interrupt` 之下 `ainvoke` 几十毫秒就返回了。这是逃生开关的已知代价，
  不是缺陷 —— 但它意味着那个开关只适合兜底，不适合长期开着。
- **窗口期内的 `worker.result` 事件可能排在 `aggregate.done` 之后。** Worker 写结果行
  和写事件之间有微秒级的窗口，而图判断屏障读的是**数据库**。这个顺序不该被断言
  （写了就是偶尔会红的测试），客户端也不该依赖它。
- **数据量没有验证过。** 六张表的索引是按查询形状设计的（部分索引给超时扫描、
  复合主键给屏障查询），但整个 M4 阶段的数据都是个位数行。
  真实规模下的表现只有 M9 的评测集能回答。
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
