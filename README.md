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

**M8 已完成**：Vue 3 仪表盘。三个页面 —— 运行记录、运行详情（发现 / 实时时间线 /
冲突与成本）、系统状态。**66 条前端测试**（不需要后端与浏览器）、ESLint + Prettier、
CI 里多一个 `web` job。

```bash
python tasks.py up          # 起全栈 → 前端 http://localhost:5173
python tasks.py demo        # 投一次审查，几秒后列表顶上会自己多出一行
python tasks.py web-dev     # 前端热更新（5273）
python tasks.py test-web    # 前端测试
```

这一层挖出的四个问题写在
「[M8 已完成](#m8-已完成vue-仪表盘)」那一节 —— 其中两个（**后端发的 SSE 帧
让 `onmessage` 一条都收不到**、**详情页的 404 自动重试从来没执行过**）
在页面上完全看不出来。

**M7 已完成**：GitHub 客户端（重试策略 / 分页 / 三条降级路）+ `publish` 节点
（三道防重复闸 + 阶梯降级），三步走完并且**评论已经落在真实仓库的真实 PR 上**：
[Satan508-h/sfly-playground#1](https://github.com/Satan508-h/sfly-playground/pull/1)
—— 一次审查 = 一条 review + 最多 25 条行内评论，全部锚定在真实变更行上
（那个 PR 上现在有**两条**，因为演示跑了两轮不同的 `head_sha`：新提交本来就该
换来一次新审查。**同一个 run 重放不会有第二条**，那是下面第三步验的）。
限流退避的证据在桩上，见
「[M7 已完成](#m7-已完成github-客户端--publish-节点)」那一节。

**M6 已完成**：GitHub webhook 入口（HMAC 验签 + 两层去重）、runs 接口、
SSE 时间线（带 `Last-Event-ID` 补齐）。验收是一条命令 —— **同一份 webhook
投 3 次，只产生 1 个 run**：

```bash
python tasks.py demo
```

```text
  载荷 fixtures/webhook_pr.json
  地址 http://localhost:8000/api/webhook
  签名 未配置密钥（服务端只在 full 模式下放行）
  投递 3 次

  -> #1  HTTP 202  accepted  已投递，审查 4 个文件
  == #2  HTTP 200  duplicate 这次投递之前已经处理过（accepted）
  == #3  HTTP 200  duplicate 这次投递之前已经处理过（accepted）

  去重发生在 投递层（同一个 delivery id）
  accepted 1  duplicate 2  涉及的 run 1 个
  run: 01M3CE0H74GSPS8H3JFPZPSJMQ

[OK] 3 次投递 -> 1 个 run + 2 次 duplicate
```

加上 `--follow` 就能看到这条 run 的完整时间线，`--drop-after 3` 会在第 3 条
事件后**主动断开**再用 `Last-Event-ID` 重连 —— 断线前后无缺口、无重复：

```bash
python tasks.py demo --follow --drop-after 3
```

```text
  时间线（SSE http://localhost:8000/api/runs/01M3CEFMMZN4TH3CGF7PNCQPKQ/events）
  #131  run.created        files=4 repo_id=demo/sfly-playground head_sha=fb7353185e1b5e877575a95c8dd… pr_title=重构用户接口并加上备份入口
  #132  node.finished      node=plan deadline_at=2026-09-25T14:21:06.540797+… files_total=4 diff_truncated=False
  #133  worker.dispatched  files=4 rules=8 message_id=1790345466547-0 worker_type=security
  -- 模拟断线：收到 3 条后主动断开 --
  #134  worker.dispatched  files=4 rules=8 message_id=1790345466549-0 worker_type=performance
  #135  worker.dispatched  files=4 rules=8 message_id=1790345466552-0 worker_type=style
  #136  worker.result      status=ok findings=10 latency_ms=1 worker_type=security
  #137  worker.result      status=ok findings=2 latency_ms=1 worker_type=performance
  #139  aggregate.done     tokens=6132 cost_usd=0.0 degraded=False findings=14
  #140  node.finished      node=finalize block_merge=True comment_chars=3113 decision_reason=secrets_found
  #142  run.finished       status=published cost_usd=0.0 degraded=False findings=14

  SSE 收到 12 条，详情接口 12 条
[OK] 断线前后无缺口、无重复
```

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

**当前总数：520 个单测 + 77 个集成测试**通过；ruff + mypy strict（含 tests）全绿。
（M1 完成时是 373 + 59，之后每个里程碑都在往上加。）

### M6 已完成：webhook 入口 / 两层去重 / SSE 断线补齐

| 接口 | 用途 |
|---|---|
| `POST /api/webhook` | GitHub 唯一的入口。验签 → 认领投递 → 构造 bootstrap → 投递 |
| `GET /api/runs?limit=&offset=` | 运行列表（按 `task_id` 倒序 = 时间倒序，ULID 自带时间戳） |
| `GET /api/runs/{task_id}` | 状态 + 报告 + **完整时间线**（首屏一次拿全，不必再开流） |
| `GET /api/runs/{task_id}/events` | SSE。带 `Last-Event-ID` 重连时**从表里补齐缺口** |
| `GET /api/deliveries` | 投递账本：「我的 PR 为什么没被审」的第一个查询 |

#### 去重是两层，而且两层防的不是同一件事

| 层 | 机制 | 防的是 |
|---|---|---|
| 投递层 | `webhook_deliveries.delivery_id` 主键 | GitHub **超时重投**同一个 webhook（重投时 `X-GitHub-Delivery` 这个 GUID **不变**） |
| run 层 | `review_runs.idempotency_key` 唯一约束 | 不同的投递、**同一个提交** —— 比如 `push` 和 `pull_request` 两个事件同时到达 |

只做第一层的话，「两个事件、一个提交」会审两遍、留两条评论、花两份钱。
只做第二层的话，重投的那三次会各自走到 `create_run` 才被吸收掉 ——
结果对，但白跑了几圈。**两层都要有，而且第二层才是正确性保证**
（第一层只是省一次查询）。

`webhook_deliveries` 还有个别的表都没有的状态：`received` —— 认领了但没结算完。
它是唯一**可以被下一次重投接管**的状态，也是「记账了但没干成」唯一能被救回来的
路径（见下面那个 INFLIGHT 窗口）。

#### 验签必须对**原始字节**做

`sha256=HMAC(secret, raw_body)`，不是对 `json.dumps(parsed)` 做。
重新序列化会改变键顺序、空白、非 ASCII 的转义方式，于是 HMAC 必然对不上 ——
而症状是「密钥明明配对了却验不过」，排查会先跑偏到密钥上。所以路由里先
`await request.body()` 拿到字节、再解析，`sfly_api/webhook.py` 里有一条
专门的单测钉住这件事（三种「看起来一样」的重建方式，字节都不同）。

另外两条：**验签必须在任何写库之前**（未认证的请求能写库的话，任何人都能用
**未来的** delivery id 提前占位，让真实投递到达时被判成重复而永久丢掉 ——
那是把去重机制反过来当攻击面用）；`hmac.compare_digest` 而不是 `==`
（后者在第一个不同的字节上返回）。

密钥没配时会怎样，见下表 —— 这是**默认状态**（这个项目的默认是零密钥跑通全链路）：

| `GITHUB_WEBHOOK_SECRET` | `MODE=full`（本地 compose） | `MODE=lite`（Render，公网） |
|---|---|---|
| 配了 | 必须验签，401 拒绝 | 必须验签，401 拒绝 |
| 没配 | **放行**，每个请求记一条 warning，`/api/health` 回显 `webhook_secret: missing` | **503 拒绝** |

用 `mode` 而不是新加一个开关，是因为它恰好就是「这个进程有没有暴露在公网」的
代理变量 —— 多一个开关就多一个配错的机会，而配错的方向是「看起来配了、实际没验」。
lite 模式的防线是访问密钥和每日成本上限，而那是**花钱**的闸、不是**身份**的闸。

#### 每一步的顺序都有理由，而且错了不会报错

```
1. 验签          不碰数据库
2. 解析 JSON      不碰数据库
3. 认领投递       第一次写库
4. 查重 → XADD    第二次写库
5. 结算投递
```

* **认领在干活之前**：并发到达的两条同 id 投递，都想「先查有没有、没有就写」，
  于是两条都读到「没有」。主键冲突是唯一能在这里定胜负的东西。
* **结算在最后**：中途崩掉时那一行停在 `received`，而 `received` 可以被下一次
  重投接管。
* **队列不可用时释放认领**（而不是留着 `received`）：账本记的是**处置结果**，
  而那次处置没有发生。释放之后重投就是一次全新的认领，不需要等窗口。

#### 「正在处理」和「死在中途」只能靠时间区分

撞上一条没结算的记录时有两种可能，而正确处置正好相反：**另一个请求此刻正在
处理它**（不能重复投递），或者**上一次处理到一半就没了**（必须接管，否则那个
PR 永远不会被审）。区分它们的唯一依据是时间 —— 一次处理只有几次 IO
（毫秒级），而崩溃留下的是一个**再也不会动**的时间戳。窗口是 60 秒
（`INFLIGHT_WINDOW_S`）。

这一条是被集成测试逼出来的：并发投同一个 delivery id 时，
5 个请求里有 4 个撞上主键冲突、看到 `received`，于是「接管」并各自投了一条
bootstrap —— **5 条消息**。只认状态不看时间的实现在这里一定会错。

#### SSE：表是权威来源，流只是快路径

不是「推送」，而是**一个带游标的轮询循环，把结果按 SSE 的格式吐出去** ——
`seq > after_seq` 这一条 SQL 同时是实时推送和断线补齐的实现，于是两者不可能不一致。
（代价是最坏 1 秒延迟；对一条要跑十几秒的审查来说是噪音级的。）

三件必须做对的事：

* **`id` 必须是 `seq`**。它是重连时 `Last-Event-ID` 的来源，也就是唯一的游标。
  用别的东西（时间戳、随机数）当 id，重连会从错误的位置继续，而那种错误
  **只在断线时**才出现。
* **终态之后还要再等 5 秒**（`TERMINAL_GRACE_S`）。`publish` 是先写状态、后写
  事件的，两步之间有真实的窗口 —— 立刻收流会让客户端**永远看不到最后那条
  `run.finished`**，而且看起来完全正常（客户端只是没再收到东西）。
* **首屏要重试到 200 再开流**。run 是**编排器**建的，不是 API 建的，
  所以投递成功之后有一小段窗口里它还不存在。404 和空流的区别就是
  「重试」和「永远等下去」的区别。

客户端那边有一条必须知道：**收到 `run.finished` 要自己 `es.close()`** ——
EventSource 在服务端关流后会**自动重连**（这是规范行为），于是变成
「连上 → 没有新事件 → 收流 → 再连上」的循环。SSE 协议里没有「别连了」这个信号，
所以这件事只能由客户端做。

#### 过程中改掉的东西

| 现象 | 真因 |
|---|---|
| 并发投同一个 delivery id，5 个请求投出 **5 条 bootstrap** | 「正在处理」和「死在中途」只认状态是分不开的，见上面那一节。加了 `INFLIGHT_WINDOW_S` 之后是 1 条 |
| 载荷不是合法 JSON 时，**账本里什么都没有** | 记账发生在解析**之后**，而结算一个还不存在的行是 `UPDATE ... 0 rows` —— 不报错、不生效。现在是「先认领再结算」，且只在认领成功时才结算（重复到达的坏载荷不能把一条已了结的记录翻成 `rejected`） |
| 容器里 `ModuleNotFoundError: No module named 'sfly_agent'` | 网关 import 了 `sfly_agent.diff` 来解析补丁，而 api 的依赖里没有 agent-core（也不该有 —— 那是 LLM/RAG/聚合的包）。**本地永远测不出来**：开发机上的 venv 装了全部 workspace 包。修法是把 `diff.py` 搬到 `sfly_shared`（它只依赖 `contracts.FilePatch` 和标准库）—— 于是网关不再拖进 rapidfuzz / rank_bm25 / LLM 客户端 |
| `GITHUB_WEBHOOK_SECRET=xxx docker compose up -d api` 之后仍然不验签 | **compose 只把 `env_file` / `environment:` 里的变量传进容器**，命令行前面那个环境变量只用于 compose 文件的 `${VAR}` 插值。所以那次「验证」什么也没验证 —— 是手工改 `.env` 才测出真结果的 |
| run 的 `block_merge` 和 `totals` 一直是 null | `finalize` 只写了 `review_reports`（jsonb），没写 `review_runs` 上那两列。运行列表要显示「阻断 / 参考」和花了多少钱，而它不该为了两个值去解每一行的 jsonb。补了 `set_decision()` |

### M8 已完成：Vue 仪表盘

三个页面：**运行记录**（列表 + 5 秒轮询）、**运行详情**（发现 / 时间线 / 冲突与成本
三个标签 + 实时 SSE）、**系统状态**（依赖探测 + 两种拓扑对照 + 配置回显）。

```bash
python tasks.py web-dev     # 开发热更新 → http://localhost:5273
python tasks.py up          # 验收/演示  → http://localhost:5173（nginx 托管构建产物）
python tasks.py test-web    # 66 条前端测试，不需要后端、不需要浏览器
```

界面本身不在这里展开（截图见上），值得写下来的是**做这一层时暴露出来的四个问题**，
因为其中两个在页面上完全看不出来。

#### 一个后端 bug：它和自己的文档互相矛盾

`sse.frame()` 一直在发 `{"event": kind, "id": seq, "data": ...}`。而 SSE 规范里
**带 `event:` 字段的帧不会派发到 `message` 类型** —— 只会触发
`addEventListener("run.created")` 这类具名监听。

后果：用 `es.onmessage` 的客户端**连接成功、然后一条事件都收不到**，而 `onopen`
正常触发、服务端正常发帧、控制台一句话都没有。而 `routes/events.py` 文档里
那段示例客户端用的**正是** `es.onmessage` —— 也就是说照着这个项目自己的文档写
客户端，同样收不到。文档和实现里必有一个是错的。

改的是实现：事件的类型已经在 `data` 的 JSON 里作为 `kind` 存在，再发一份
`event:` 是同一个事实的两个来源；而反过来的修法（客户端枚举所有事件类型注册
监听器）会让服务端新增一种事件时**客户端悄悄不订阅它** —— 同一类静默失败。
现在全走默认的 `message`，`kind` 从 JSON 里读，
`tests/unit/api/test_sse.py::test_a_frame_has_no_named_event` 钉着这条。

判据是拿**真的** `EventSource` 验的（Node 24 内置的 undici 实现，遵守规范），
而不是我们自己的假实现：`onmessage` 收到 37 条事件、含 `run.finished`；
直连 `:8000` 与经 Vite 代理 `:5273` 两条路都验过。顺带现场看到服务端宽限期结束后
undici **自动重连了一次** —— 这正是「收到 `run.finished` 必须自己 `close()`」
那条纪律的由来（不收就是无限重连，而服务端没有任何办法说「别连了」）。

#### 一个前端 bug：那段重试从来没执行过

详情页的文档写着「刚投递完的窗口期里 404 是正常的，所以自动重试几次」，代码是
`if (auto && retries < MAX) setTimeout(...)`，而调用处是 `onMounted(() => void load())`
—— **不传参数，`auto` 恒为 `false`**，那句 `setTimeout` 一次都没执行过。
注释和代码说的是两件事，而没有任何东西报错。

是渲染测试抓出来的（`expected 1 to be greater than 1`）。现在 404 一定安排重试
（上限 8 次），`auto` 只决定要不要显示骨架屏；组件卸载时清掉定时器。

#### 一个更阴的：6 条测试全绿，而组件根本没工作

第一版渲染测试 6 条**全过**，但日志里躺着一行
`Failed to resolve component: el-tooltip` —— 测试环境没装 Element Plus，
Vue 把未注册的组件当**未知元素**渲染，插槽里的文字照样进 DOM，于是
`wrapper.text()` 的断言全部照常通过。

现在 `web/src/testing/mount.ts` 的 `mountWithUi()` 做两件事：按线上那样装上
Element Plus，并**让任何 Vue 警告直接判失败**。而这道闸**自己也有测试**
（`mount.spec.ts`）—— 一个闸失效的表现是所有渲染测试照样全绿，不会被发现。
（写那条探针时还踩了一次假阴性：用内联 `template` 字符串写的探针根本没渲染，
于是「没触发组件解析」被误读成「闸没起作用」。探针本身也得是对的。）

#### 一个开发机上的陷阱：5173 端口上蹲着两个东西，都不报错

| | |
|---|---|
| `0.0.0.0:5173` / `[::]:5173` | Docker 的端口转发（nginx，跑的是**镜像里那份构建产物**） |
| `127.0.0.1:5173` / `[::1]:5173` | Vite 开发服务器 |

两者**都能绑上、谁都不报错**，而本机 `localhost` 优先解析到 `::1` —— 于是打开
5173 看到的是旧构建、改代码毫无反应。开发服务器现在独占 **5273** 并设了
`strictPort`（抢不到就报错退出，而不是自己换个端口继续打印「Local: 5174」）。
判据：开发模板里有 `/@vite/client`，构建产物里是带哈希的 `/assets/*.js`。

#### 过程中改掉的东西

| 现象 | 原因与修法 |
|---|---|
| `JSON.parse` 的结果没验形状，`null` / `5` / `"x"` 会让流「活着但不再处理消息」 | 形状检查从 try 里分出来。一个抛穿 `onmessage` 的 TypeError，症状是流看起来还正常 |
| 「七个节点全绿」这条断言**连着红了两回** | 两回都是 fixture 里漏了 `finalize` 那条 `node.finished`（只有 `plan` 和 `finalize` 会发）。第二次不再改 fixture，而是把「一个完整跑完的 run 的事件序列」收进 `testing/factories.ts` 一份，两个 spec 共用 |
| 按数组下标取 fixture（`REAL_RUN[5]`） | 往中间补一条事件，所有下标错位 —— 测试红了，而红的原因和被测的东西无关。改成按事件类型取 |
| 成本 `$0.03` | 一次审查的成本在**分级**量级（实测 `$0.0342`），两位小数把有效数字砍掉一半，而「每 PR 成本」正是要拿出来讲的数字。一美元以下改成四位小数 |
| `vitest` 2 装完 `vue-tsc` 报了一屏 `Omit<UserConfig, "plugins">` 不兼容 | vitest 2 自带一份 vite 5，和项目的 vite 6 撞类型。升到 vitest 3 |



#### 第一步：对着桩看退避

下面这次是**真的 HTTP**，评论从**容器里**发出去，而且限流退避是真的等过：

```bash
python tasks.py stub-github                 # 另一个终端：前 2 次发布返回 429
# .env 里把 GITHUB_API_BASE 指向这个桩，重启编排器
python tasks.py demo
```

桩收到的请求序列（`GET http://127.0.0.1:8099/__state`）：

```text
GET  /repos/demo/sfly-playground/pulls/42/reviews     ← 闸 2：查有没有带标记的正文
GET  /repos/demo/sfly-playground/issues/42/comments   ← 闸 2 的另一半
GET  /user                                            ← 判断我是不是 PR 作者
POST /repos/demo/sfly-playground/pulls/42/reviews     ← 429（二级限流）
POST /repos/demo/sfly-playground/pulls/42/reviews     ← 429
POST /repos/demo/sfly-playground/pulls/42/reviews     ← 201 ✓
```

编排器日志里那两次退避（`retry-after: 1`，实测就等了 1 秒）：

```json
{"event": "github.retry", "attempt": 1, "status": 429, "wait_s": 1.0}
{"event": "github.retry", "attempt": 2, "status": 429, "wait_s": 1.0}
{"event": "node.publish", "posted": true, "form": "review:request_changes+inline",
 "comment_id": 1001, "inline_sent": 14, "inline_skipped": 0, "block_merge": true}
```

数据库里那一行：`status=published`、`github_comment_id=1001`。
`inline_sent=14` 说明 **14 条行内评论全部落在真实的变更行上** ——
桩在这方面比真 GitHub 还严（行号对不上就拒**整个** review，连汇总正文一起）。

#### 第二步：真实 PR 上的真实评论

靶场是 [`Satan508-h/sfly-playground`](https://github.com/Satan508-h/sfly-playground)
（公开仓库）：`main` 上是一个干净的小 Flask 应用，PR #1 引入了一批典型问题
—— 硬编码密钥、两条 SQL 拼接、`shell=True`、md5 当口令散列、`pickle.loads`、
裸 `except:`、`== None`、`innerHTML = content`。

```bash
python tasks.py record-fixture --repo Satan508-h/sfly-playground --pr 1
python tasks.py demo --follow
```

**fixture 是录出来的，不是编的。** `record-fixture` 用**产品自己的客户端**
（`GitHubClient.pull_files` —— 同一套分页、同一套重试）去取 `/pulls/1/files`，
连同仓库/PR 元数据写进 `fixtures/webhook_pr.json`。录完的那份载荷里，
仓库名和 PR 号是真的，所以 `publish` 发出去的评论落在真的 PR 上。

> 顺手验了一件事：被它替换掉的那份**手写** fixture，4 段 `patch` 和真实响应
> **逐字相同** —— 连 7 个 hunk 头和行号都一致，真实响应只多了 `blob_url` /
> `raw_url` / `contents_url` 三个我们不读的字段。当初照着 GitHub 的响应形状
> 手写它没写错，但这件事从此不需要靠手写。

结果（`gh api repos/Satan508-h/sfly-playground/pulls/1/reviews`）：

```text
  review id 5324355208   state=COMMENTED   user=Satan508-h
  正文第一行 <!-- sfly:run:01M3DVHP409TBPY721JN9VTA0P -->（隐藏标记）
  行内评论 14 条，全部锚定在真实变更行上
```

> 那个 PR 上现在有**两条** review（28 条行内评论），第二条是这之后又跑了一轮
> 留下的：`demo` 默认会给载荷换一个 `head_sha`（模拟「往 PR 推了新提交」），
> 而新提交换来一次新审查是**对的**。两条 review 的隐藏标记不同 ——
> 它们属于两个不同的 run。**标记相同才会被闸 2 拦住**，那正是第三步验的东西。

`state=COMMENTED` 而不是 `CHANGES_REQUESTED` 是**对的**，日志写着原因：

```json
{"event": "node.publish.own_pull_request", "repo": "Satan508-h/sfly-playground", "pr": 1,
 "note": "机器人就是 PR 作者本人，GitHub 不允许给自己的 PR 请求修改 → 发 COMMENT"}
```

也就是说：报告算出了 `block_merge=true`（发现凭据泄露），但 GitHub 拒绝让一个
账号给自己的 PR 请求修改。这条预检（`whoami()` 先问「我是谁」再决定发什么事件）
此前只有单测覆盖 —— 现在是被真 GitHub 的 422 逼出来的。

动手之前值得知道的三件事：

* **每条评论都是真的。** `demo` 每跑一次换一个 `head_sha`，也就是每跑一次
  往那个 PR 上多一条 review。演示时这正是要看到的，平时反复跑会让 PR 变乱。
* **但同一个提交不会审两遍**：`scripts/replay_webhook.py --verbatim`（不改
  `head_sha`）第二次起直接回 `duplicate 同一提交已经被审过（published）`，
  连 run 都不会新建。
* **完全不想碰 GitHub**：把 `.env` 里的 `GITHUB_TOKEN` 留空（`publish` 走
  dry-run，状态仍是 `published`），或者用
  `python -m sfly_workers --spec security --diff ...` 那条不接队列、不连数据库的路。

#### 第三步：两道防重复闸也拿真 GitHub 验了

这两道闸的**真实触发条件是「评论发出去了、写库/写状态那一步崩了」**，
那是个毫秒级窗口，等不到也撞不准。所以 `scripts/replay_bootstrap.py` 换个方向：
不去撞窗口，而是**把状态摆回窗口留下的样子**，然后把同一份 bootstrap 重投一遍。

```bash
python tasks.py replay-bootstrap --task-id 01M3DVHP409TBPY721JN9VTA0P --simulate-crash
python tasks.py replay-bootstrap --task-id 01M3DVHP409TBPY721JN9VTA0P \
       --simulate-crash --forget-comment-id
```

```text
  第一次：form=already        reason=数据库里已有 comment id（节点重放，不重发）
  第二次：form=adopted:review reason=评论已经在 PR 上（上次发出去之后写库失败了）
          comment_id 5324355208 ← 从 PR 正文的隐藏标记里认出来的
  库里 comment id -> 5324355208 ← 写回库了（自愈）
  PR 上：两次重投的前后，reviews 与 inline 计数**完全没变**
```

第二次顺带证明了一件事：**「发出去但没记住」是可以自愈的** —— 标记认出来之后
`mark_published` 会把 id 补回数据库，不需要人工介入。

这里还撞出一个以前没写下来的事实：`GraphRunner` 在入口就会把**终态 run 的重投
直接丢弃**（事件 `graph.bootstrap_ignored`）。那是对的（GitHub 超时重投同一个
webhook 是常态，不能因此再审一遍），代价是「重投」这条路根本走不到 `publish` ——
所以要验那两道闸，必须先 `--simulate-crash`。

#### 三道闸，各挡一种不同的重复评论

1. **`review_runs.github_comment_id`** —— 数据库说发过了。挡的是节点重放
   （LangGraph 恢复时重跑节点是**正常路径**）。
2. **正文里的隐藏标记** `<!-- sfly:run:{task_id} -->` —— PR 上已经有这条正文了。
   挡的是第 1 道挡不住的那种：**评论发出去了、写库那一步失败了**。
3. **`mark_published` 是 UPDATE** —— 重放时写的是同一行的同一列，天然幂等。

签发（`render.marker_for`）和识别（`publish`）**必须是同一个函数** —— 它们曾经
是两处字面量，而那种重复的失效方式很安静：格式一改，新标记照常写进正文，
查找的那一边却永远匹配不上，于是闸 2 变成一句空话。

#### 被拒了就退一格，不去解析错误消息

GitHub 拒绝一次 review（422）有两个来源，处置方式**正好相反**：给自己的 PR
请求修改（改用 `COMMENT`）、行号不在 diff 里（去掉行内评论）。所以这里是
一个从完整到保守的阶梯：`review+行内` → `review` → `COMMENT` →
**普通评论**（换端点，没有行号可以不对）。最后一格还能失败就只剩权限和网络了。

解析错误消息字符串来决定怎么办是脆的（GitHub 改个措辞就失效），而且
**两个来源可能同时出现**。阶梯不需要知道是哪一个。

#### 过程中改掉的（都是**测试逼出来**的）

| 现象 | 真因 |
|---|---|
| `publish` 真的把 `GitHubError` 抛了出去 | 模块文档写着「绝不向上抛」，代码里却没有那个 `try`。抛出去的表现是 run 停在 `aggregating`，而扫描器的 `due_runs` 只看 `dispatched`/`waiting` —— **没有任何东西能唤醒它** |
| dry-run 被记成了 `publish_failed` | `posted=False` 有两种来源：没配 token（本来就没打算发）和真的发失败了。用一个字段表示两件事，于是「本地不需要密钥就能跑通全链路」这句话在状态层面变成假的 —— 每个 run 都带着一个红灯。现在由 `delivery_failed` 决定状态和事件类型 |
| `python tests/github_stub.py` 报 `No module named 'sfly_api'` | 系统 Python ≠ 项目解释器。桩 import 了 `apps/api` 的补丁转换函数（避免把 diff 重建逻辑抄第二遍）。现在有 `python tasks.py stub-github`，它走 `_py()` |

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

打开 <http://localhost:5173> 是**运行记录**（nginx 托管的构建产物）；没有数据时
按页面上的提示跑一次 `python tasks.py demo`，几秒后列表顶上会自己多出一行。
点进去就是这次审查的全部：按文件分组的发现、实时时间线、成本明细。

想改前端代码用热更新那条路（**另一个端口**，见下）：

```bash
python tasks.py web-dev     # → http://localhost:5273
```

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
| **表是应用建出来的** | `python tasks.py tables` | 七张业务表 + `schema_version`（第 1、2 版都已应用）。空库上也能建 —— 每个容器启动时都跑一遍幂等 `migrate()` |
| **副本猝死，同伴接手** | `python tasks.py demo-reclaim` | 4 条任务被两个副本瓜分 → 一个副本中途消失 → PEL 里留下 1 条没人认领的记录 → `reclaim()` 抢回 → 重投的 `attempt` 是 2（不需要全栈，只要一个 Redis） |
| Worker 水平扩展 | `python tasks.py scale 3` | `worker-security` 变成 3 个副本，**同一个消费者组里三个消费者在竞争**（常驻循环 M4 已就绪，`XINFO CONSUMERS` 数得出来） |
| **Worker 真的在写库** | `docker compose logs worker-security \| grep worker.consuming` | 每个副本报出消费者组、并发数、回收间隔；收到任务时按「存库 → 发结果 → ack」处理（集成测试逐条验证这个顺序） |
| **一条命令跑完整条链路** | `python tasks.py review` | 一张图从 bootstrap 跑到报告：三次派发、三个 Worker 上报、聚合、阻断决策。stdout 是报告 JSON（`\| jq` 直接可用） |
| **图会挂起，也能被唤醒** | `python tasks.py review` 的 stderr 时间线 | 若某个 Worker 慢一步，日志里会出现 `node.wait_suspend`，然后是协调协程的 `coordinator.barrier_closed` 把它叫醒 —— 中间进程不持有这个 run 的任何状态 |
| **断点恢复靠一条 SQL** | `RUN_DEADLINE_S=10 python tasks.py review`（不启动 Worker） | 15 秒内扫描器捞出这个 run 并唤醒，`wait` 走超时分支给三个掉队的 Worker 各补一条 failed 结果，报告带降级徽章 |
| 幂等：同一份 diff 只审一次 | `python tasks.py review --replay` | 复用同一个 run（幂等键 = `repo:pr:head_sha`），打印上一次的报告而不是重新审查 |
| Worker 猝死不影响结果 | `python tasks.py kill-worker` | run 照样跑完并显示降级徽章（M5 起屏障能被编排器闭合了） |
| **Webhook 幂等（投递层）** | `python tasks.py demo` | 同一份 payload 投 3 次 → 1 个 run + 2 个 `duplicate`，队列上**只有一条** bootstrap |
| **Webhook 幂等（run 层）** | `python tasks.py demo --new-delivery` | 每次换一个 delivery id，但提交没变 → 还是 1 个 run。这一层由 `review_runs.idempotency_key` 的唯一约束兜住 |
| **断线无缺口** | `python tasks.py demo --follow --drop-after 3` | 收到 3 条后主动断开，用 `Last-Event-ID` 重连补齐剩下的 9 条；脚本会拿详情接口对一遍，缺一条就报 `[!!]` |
| **未验签的请求写不进库** | 配好 `GITHUB_WEBHOOK_SECRET` 后用错密钥投一次 | 401，且 `GET /api/deliveries` 里**不会**多出一行 |
| **限流了真的会退避重发** | `python tasks.py stub-github` + 把 `GITHUB_API_BASE` 指向它 + `python tasks.py demo` | 桩先回两次 429（带 `retry-after`），编排器日志里出现两条 `github.retry`，第三次成功；评论**真的发出去了**（桩的 `/__state` 里能看到） |
| **评论真的发到真实 PR 上** | `python tasks.py record-fixture --repo <owner/name> --pr <n>` 之后 `python tasks.py demo` | 那个 PR 上出现一条 review（正文开头是 `<!-- sfly:run:<id> -->`）+ 最多 25 条行内评论，全部锚在真实变更行上 |
| **重放到真实 PR 上也不重复评论** | `python tasks.py replay-bootstrap --task-id <id> --simulate-crash` | 事件里 `form=already`，PR 上评论条数不变（`gh api .../pulls/<n>/reviews --jq length`） |
| **界面能自己动起来** | `python tasks.py demo` 之后盯着 <http://localhost:5173> | 列表每 5 秒自动刷新，几秒后顶上多出一行；点进去看那条 run 的节点进度与事件时间线。**不需要刷新页面** |
| **实时时间线断线能补齐** | `python tasks.py demo --follow --drop-after 3` + 页面开着 | 服务端主动断开再重连，客户端按 `seq` 去重、服务端按 `Last-Event-ID` 补齐 —— 时间线上不重不漏 |
| **前端也能自己跑测试** | `python tasks.py test-web` | 66 条，jsdom 里跑真组件（含 Element Plus），不需要后端、不需要浏览器、几秒钟 |
| 前端 lint 与格式 | `python tasks.py lint-web` | ESLint（只管对错）+ Prettier（只管格式）—— 和 Python 那边 `ruff check` / `ruff format` 的分工一致 |
| **发出去但没记住，能自愈** | 同一条再加上 `--forget-comment-id` | `form=adopted:review`：靠正文里的隐藏标记从 PR 上认回自己的评论，并把 id 写回库 |
| 投递账本 | `GET /api/deliveries` | 每条投递的结局：`accepted` / `duplicate` / `ignored` / `rejected`，以及它转给了哪个 run |
| 断点恢复 | `docker restart sfly-orchestrator-1` | 从 Postgres 的 checkpoint 续跑 |
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
                 webhook.py 签名（签发与校验只此一份，回放脚本 import 它）
                 github_payload.py 载荷 → BootstrapMessage（纯函数）
                 sse.py 收流条件；routes/ 四组路由；deps.py 依赖注入点
  orchestrator/  graph.py 装配图；nodes/ 七个节点各一个文件
                 runner.py 消费 bootstrap；coordinator.py 屏障 + 唤醒
                 sweeper.py 超时扫描；checkpointer.py **自己一个连接池**
  workers/       一套代码三种部署：--spec {security|performance|style}
                 pool.py 是消费循环本体 —— 一个容器一条 lane，或者一个进程
                 三条协程（精简模式），**同一份代码**
  lite/          单事件循环，一个进程跑完整个系统（Render 用）
packages/
  shared/        领域契约、配置、ID、日志、异常、console（编码）
                 diff.py unified diff 解析 —— **网关、编排器、Worker 三处共用**，
                 所以它在这里而不在 agent-core（否则网关要拖进整个 LLM 栈）
  bus/           TaskQueue / RunStore / Lock 协议 + 两种实现
                 postgres.py 里是池子 + 仓储；migrations/*.sql 是全部表结构
                 （001 六张业务表、002 webhook 投递账本；
                 **迁移 SQL 是数据文件**，靠 Dockerfile 的 COPY packages 进镜像）
  agent-core/    LLM 抽象与结构化输出（含 pricing.py 的价格表）、RAG、
                 state.py 图状态、risk.py 文件风险排序、
                 aggregate/ 主 Agent 的聚合（fingerprint / confidence /
                 decision / pipeline / render，**全确定性、零 LLM 调用**）
web/             Vue 3 SPA（Vue 3 + TS + Vite + Element Plus + Pinia + vue-router）
                 src/api/    client.ts 取数与类型、sse.ts 事件流封装
                 src/lib/    纯函数：格式化、发现的分组/排序、事件摘要与节点进度
                             （**纯函数是有意的** —— 前端真正值得测的是这些）
                 src/testing/ factories（假数据）+ mount.ts（挂载助手，
                             **让任何 Vue 警告判失败**，它自己也有测试）
                 src/components/ views/ stores/ router/
infra/           postgres init（只有扩展，表由应用建）、redis conf、nginx conf
fixtures/        diff 样例、录制的 webhook 载荷（含 /pulls/{n}/files 的响应）、大 PR
scripts/        运维与演示脚本（replay_webhook.py 是 M6 的验收工具；
                 record_pr_fixture.py 录真实 PR、replay_bootstrap.py 重投验防重复闸）
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

- **没有配 `GITHUB_TOKEN` 时，`publish` 是 dry-run。** 一行 HTTP 都不发，
  `publish.done` 里明写 `posted: false`、`form: dry_run`，`github_comment_id`
  保持为空 —— **没有这个 id 的 run 就是没发过评论**，UI 不该显示「已评论」。
  这**不是**失败：状态仍然是 `published`、报告是完整的（正文在
  `review_reports.comment_body` 里），本地和 CI 因此不需要任何密钥就能跑通全链路。
- **`GitHubClient.pull_files` 还没有任何运行时调用者。** GitHub 的 `pull_request`
  事件**不带任何代码**，生产路径上必须在收到 webhook 之后立刻调一次
  `GET /repos/{owner}/{repo}/pulls/{n}/files`，把响应塞进载荷的 `files` 字段 ——
  **这一步现在由录制/回放脚本代劳，API 路由里还没有它**（要接它需要一个真实
  webhook 入口，也就是 M10）。所以今天 `fixtures/webhook_pr.json` 里的 `files`
  是一份录制结果，而不是每次投递现取的。
  客户端本身是被真实调用过的（`record-fixture` 用它取的 `/pulls/1/files`，
  同一套分页与重试），而两条路径最终喂给**同一个** `patches_from_files` ——
  「回放能审、真上线审不了」这种分叉不会发生。
- **SSE 是轮询（1 秒）而不是推送。** 换来的好处是实时与补齐共用同一条
  `seq > after_seq` 查询，两者不可能不一致。真要更低延迟，`LISTEN/NOTIFY`
  只该用来**提前唤醒**这个循环，而不是取代它。
- **`INFLIGHT_WINDOW_S`（60 秒）是猜的。** 它的取值区间很宽（远大于处理耗时、
  远小于人的反应时间），但真实负载下「一次投递处理多久」没有量过。
  窗口内崩溃 + 立刻重投的组合下，那次投递会被当成「正在处理」，
  需要等窗口过去再投一次 —— 这个代价是明确的，只是没有实测过。
- **投递账本会无限增长。** `webhook_deliveries` 没有清理策略（
  `purge_older_than` 现在只管 checkpoint / run_events / llm_calls）。
  每次 push 都是一行，真实仓库上它比 run 表增长得快得多。
- **聚合只做指纹精确合并，还没有相似度聚类。** 两个 Worker 用不同措辞说同一处
  问题、或者模型这次报第 10 行下次报第 12 行时，指纹不同 —— 而它们其实是同一件事。
  那一步（并查集 + rapidfuzz，同 Worker 0.75 / 跨 Worker 0.55）是 M9。
  指纹那一步**永远不会被替换掉**（它是并查集的快速路径），M9 加的是它后面的兜底。
- **`conflicts` 恒为空，而且前端必须说清楚这件事。** 冲突消解（同路径 + 邻近行号 +
  不同 Worker + 严重度差 ≥ 2）和聚类一起排在 M9。界面上「一个空表格」和
  「算过了、没有冲突」长得一模一样，含义正好相反 —— 所以冲突面板在空的时候
  **主动解释**自己的空是因为还没算（`ConflictsPanel.vue` 里的第一段）。
  同样受影响的还有每条发现右下角那组 Worker 圆点：跨 Worker 印证也来自聚类，
  所以现在每条发现的来源都只有一个 Worker，界面上不做任何「印证数」的统计。
- **前端没有浏览器端的端到端测试。** 组件测试跑在 jsdom 里（66 条），SSE 那一段
  用 Node 内置的真 `EventSource` 验过协议行为，但「真浏览器里点一遍」没有自动化 ——
  jsdom 不实现 `EventSource`、布局与滚动也是假的。这一层目前靠人工冒烟。
- **Element Plus 是全量引入，构建产物 940 KB（gzip 302 KB）。** 首屏因此偏重，
  而精简模式跑在 Render 免费版上、冷启动本来就慢。改成按需引入（两个构建期插件）
  预计能砍到 100 KB 上下，排在 M10 部署那一步一起做。
- **`localhost:5173` 与 `localhost:5273` 是两份不同的东西**（nginx 的构建产物 vs
  Vite 的热更新），差别和踩过的坑见 M8 那一节。
- **`WAIT_STRATEGY=poll` 一次只能推进一个 run。** 轮询期间整条消费协程被占住，
  而 `interrupt` 之下 `ainvoke` 几十毫秒就返回了。这是逃生开关的已知代价，
  不是缺陷 —— 但它意味着那个开关只适合兜底，不适合长期开着。
- **窗口期内的 `worker.result` 事件可能排在 `aggregate.done` 之后。** Worker 写结果行
  和写事件之间有微秒级的窗口，而图判断屏障读的是**数据库**。这个顺序不该被断言
  （写了就是偶尔会红的测试），客户端也不该依赖它。
- **数据量没有验证过。** 七张业务表的索引是按查询形状设计的（部分索引给超时扫描、
  复合主键给屏障查询），但整个 M4 阶段的数据都是个位数行。
  真实规模下的表现只有 M9 的评测集能回答。
- **GitHub 真实二级限流只能用桩模拟。** `tests/github_stub.py` 模拟
  429/403 → 退避 → 201 的序列。桩是模型，不是真相。
- **`python tasks.py review`（从 diff 直接跑）恒为 dry-run。** 那条路上的
  `repo_id` 是为了让幂等键成立而编的（`--diff` 模式不接队列、不连数据库，
  也没有 PR 号），拿它去调 GitHub 只会稳定 404。要看真实的发布，
  走 `record-fixture` + `demo` 那条路（见 M7 那一节）。
- **webhook 载荷是录的，不是 GitHub 现场推来的。** `record-fixture` 取的是
  `/repos/{o}/{r}`、`/pulls/{n}`、`/pulls/{n}/files` 三个**拉取式**接口的响应，
  再拼成 webhook 的形状。所以我们读的那些字段路径是被验证过的（拼错了会立刻
  体现在报告里），但**「GitHub 真正推过来的整包长什么样」仍然没有端到端验证过**
  —— 那需要一个公网可达的地址（M10 的 Render）和一个真实的 webhook secret。
  这是部署上线后才能回答的第一个问题。
- **两道防重复闸的触发窗口是「制造」出来的，不是撞出来的。**
  它们的真实触发条件是「评论发出去了、写状态那一步崩了」，那是个毫秒级窗口。
  `replay-bootstrap --simulate-crash` 把状态摆回那个窗口留下的样子再重投 ——
  验的是**同一段代码路径**（图唤醒 → `publish` → 闸），但不是真的在那一刻
  杀进程。真正那个竞态（POST 已返回、状态还没落库时断电）没有被测过。
- **靶场用的细粒度 token 有到期日（2026-12-24）。** 到期后 `publish` 会 401，
  而 401 被归类成 `retryable=False` —— 点「重新发布」不会好，得换 token。
  分类是刻意的（换 token 才是处置方式），代价是这份演示三个月后要重配一次。
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
