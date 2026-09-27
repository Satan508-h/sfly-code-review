# 简历上的项目介绍

**怎么用**：下面「完整版」的正文可以直接贴进简历的项目栏。贴之前把时间改成你的
实际周期。「每一句的证据在哪」那张表是**给你自己看的** —— 面试官问到哪一句，
你得能说出它从哪来。**说不出来出处的句子，就删掉它。**

## 完整版

**sfly — 多 Agent 分布式代码审查系统**
`Python · LangGraph · Redis Streams · PostgreSQL · Vue 3 · Docker`　2026.09

GitHub PR 一提交，安全 / 性能 / 风格三个 Agent 并行审查，主 Agent 汇总去重、
消解冲突、重算置信度，结论作为评论回写 PR。不依赖 K8s，不依赖 Kafka。

- **一套代码两种部署拓扑**：队列与锁抽象为 Protocol，Redis Streams 与进程内两套
  实现共用同一份契约测试，全代码库仅工厂一处按环境变量分支。
  `docker compose up --scale worker-security=3` 即可水平扩展 —— 三个副本竞争消费
  同一消费者组，不需要任何应用层协调代码。
- **自建 30 用例评测集量化多 Agent 收益**（15 手写注入 / 10 从真实 CVE 修复回退 /
  5 干净代码）：真实模型下三个 Worker 较单 Agent 召回 **84.8% vs 72.7%**，
  代价 **2.77 倍成本**（$0.1483 vs $0.0536）；回退的真实 CVE **10 条全部命中**。
  评测分离线层（可复现、进 CI）与真实层（不可复现），置信度阈值由扫描得出。
- **断点恢复与容错**：编排状态机在等待 Worker 时挂起并持久化，恢复只依赖一条带
  `deadline` 的 SQL，不依赖内存状态 —— 编排器崩溃重启后自动续跑；Worker 猝死由
  消费者组回收重投，重试耗尽则补发失败结果闭合屏障。实测杀掉一个 Worker，
  run 仍跑完并在报告中标出降级。

## 精简版（项目栏空间不够时）

**sfly — 多 Agent 分布式代码审查系统**
`Python · LangGraph · Redis Streams · PostgreSQL · Vue 3`

- **一套代码两种拓扑**：队列与锁抽象为协议，Redis 与进程内实现共用同一份契约测试，
  仅工厂一处分支；`--scale` 即可水平扩展，三个副本竞争消费同一消费者组。
- **量化多 Agent 收益**：自建 30 用例评测集（含 10 条从真实 CVE 回退的用例），
  真实模型下三个 Worker 较单 Agent 多召回 **12.1 个百分点**，代价 2.77 倍成本。
- **断点恢复与容错**：编排器崩溃后靠一条 SQL 恢复续跑；实测杀掉一个 Worker，
  run 仍跑完并标注降级。

---

## 每一句的证据在哪

| 简历上的话 | 出处 |
|---|---|
| 三个 Agent 并行、主 Agent 汇总去重定级 | `apps/orchestrator/` 七个节点；`docs/DEMO.md` 第 ② 段 |
| 队列与锁抽象成 Protocol | `packages/bus/sfly_bus/base.py` |
| 同一份契约测试跑两个实现 | `tests/contracts/`（`queue_contract.py` + `lock_contract.py`）—— 加一条测试，四个测试文件同时受益 |
| 全库仅工厂一处分支 | `tests/unit/bus/test_protocols.py` 用 AST 扫出来的，**这条是可执行的检查**，不是声明 |
| `--scale` 三个副本竞争消费 | `python tasks.py scale 3`，然后看日志里的 consumer group |
| 30 用例、三组人群 | `tests/eval/cases/`（30 个 yaml + 对应 diff） |
| 召回 84.8% vs 72.7%、2.77 倍成本 | `reports/eval-e9aafd0-ablation-real-deepseek.md`（2.77 = $0.1483 ÷ $0.0536） |
| 真实 CVE 10 条全中 | `reports/eval-a010ba9-real-deepseek.md` 的 `rebuilt` 组：精确率 76.9% / 召回 100% |
| 「从真实 CVE 修复回退」是什么意思 | 取那些**已修复**的公开仓库代码，把修复回退掉，于是漏洞重新出现 —— ground truth 不是我标的，是那个 CVE 本身。见 `scripts/rebuild_case.py` |
| 阈值由扫描得出 | 两份报告里的「置信度闸的阈值扫描」表（**两层的结论是相反的**，这正是必须跑两层的原因） |
| 挂起 + 一条 SQL 恢复 | `apps/orchestrator/nodes/wait.py`（`interrupt()`）与 `sweeper.py` 的 `due_runs` |
| 杀掉 Worker 仍跑完 | `python tasks.py kill-worker` 然后 `python tasks.py demo` |
| 失败也补发结果闭合屏障 | `apps/workers/sfly_workers/pool.py`；CLAUDE.md 的「失败也是结果」 |
| 真的在 PR 上发过评论 | 靶场仓库 `sfly-playground` 的 PR #1 |

## 不要写上去的话

这几句听起来很诱人，但你**答不上追问**，或者和仓库里的事实冲突：

- ❌ **「已部署上线」/ 附一个线上地址** —— 项目没部署，原因写在
  [`DEPLOY.md`](DEPLOY.md) 开头（三个平台的账号门槛，都是政策问题，不是代码问题）。
  **被问到时主动说出来，比被拆穿强得多** —— 而且你手上有那三个平台的原文报错。
- ❌ **「支持 N 种语言」** —— 没这回事，规则库是手写的 YAML。
- ❌ **「高并发」/「QPS 」** —— 没测过。你只有一个规模：「三个 Worker 竞争消费」。
- ❌ **「审查效率提升 XX%」** —— 没有这个指标。你有的是**召回率**和**成本**，用那两个。
- ❌ **「基于大模型的智能体平台」这类形容词** —— 空话占位置。你有数字，用数字。

## 一段话讲清这个项目（自我介绍 / 面试开场用）

> GitHub 上一个 PR 提上来，三个专业 Agent 并行审，主 Agent 汇总去重定级，最后把
> 结论作为评论写回那个 PR。它的核心是**一套代码两种拓扑**：队列和锁都是协议，
> Redis 和进程内两套实现共用同一份契约测试，所以本地七个容器和线上单容器跑的是
> 同一批对象。我还建了 30 个用例的评测集来量「多 Agent 到底值不值」—— 真实模型上
> 三个 Worker 比单 Agent 多召回 12.1 个百分点，代价是 2.77 倍成本。

更长的讲法（分镜 + 七条追问的答案）见 [`DEMO.md`](DEMO.md)。
