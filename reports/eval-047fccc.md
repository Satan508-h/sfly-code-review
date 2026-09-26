# sfly 评测报告（离线层 · Mock LLM）· 047fccc

- 代码版本：`047fccc`（**生成时工作区有未提交改动**，这份数字不一定精确对应上面那个 commit）
- 用例：20 个（injected 15，clean 5）
- LLM：**Mock**（确定性正则扫描器，不产生任何模型调用，成本恒为 $0）
- 命令：`python tasks.py eval`

**这份报告量的是聚合层**（聚类、去重、置信度闸、冲突消解），不是模型的审查能力 —— 这一层里根本没有模型。干净组的误报率主要由扫描器一次只看一行的粗糙程度决定，**不能当作系统的精确率引用**。

## 总览

| 指标 | 严格档 | 宽松档 |
|---|---|---|
| 真阳性 | 18 | 18 |
| 假阳性 | 1 | 1 |
| 漏报 | 5 | 5 |
| **精确率** | **94.7%** | 94.7% |
| **召回率** | **78.3%** | 78.3% |
| F1 | 0.857 | 0.857 |
| 严重度判对 | 18 | — |

> 严格档 = 文件 + 类目 + 行号（±3 行内）全对；宽松档 = 文件 + 类目对，不看行号。
> 差距说明的是「定位准不准」，而不是「有没有找到」。

## 误报与召回损失

- 干净组：5 个用例，共发布 **1** 条发现 → 每个干净 PR 平均 0.20 条
- 被置信度闸砍掉、但确实命中 ground truth：**4** 条（这就是那道闸的召回代价；它只入库不发布，所以不在上面几个数里）
- 冲突裁决：0 次

## 置信度闸的阈值扫描

置信度门槛是**唯一一个纯策略数字**（其余都是量出来的），所以它不该靠猜。
下表把同一批发现按不同阈值重切一遍 —— 不需要重跑审查，因为阈值只影响发布、不影响模型。

| 阈值 | 发布条数 | 严格精确率 | 严格召回率 | F1 | 干净组每条 |
|---:|---:|---:|---:|---:|---:|
| 0.20 | 25 | 88.0% | 95.7% | 0.917 | 0.20 |
| 0.25 | 25 | 88.0% | 95.7% | 0.917 | 0.20 |
| 0.30 | 20 | 95.0% | 82.6% | 0.884 | 0.20 |
| 0.35 **←当前** | 19 | 94.7% | 78.3% | 0.857 | 0.20 |
| 0.40 | 14 | 100.0% | 60.9% | 0.757 | 0.00 |
| 0.50 | 7 | 100.0% | 30.4% | 0.467 | 0.00 |
| 0.60 | 1 | 100.0% | 4.3% | 0.083 | 0.00 |
| 0.70 | 0 | 0.0% | 0.0% | 0.000 | 0.00 |

> 读法：阈值调低会同时拉高召回和误报。**没有免费的档位** ——选哪一档取决于「漏掉一个真问题」和「多报一个假问题」哪个更贵，而那是产品决策，不是技术决策。

## 成本与延迟

| 项 | 值 |
|---|---|
| 用例数 | 20 |
| Worker 数 | 3 |
| 总成本 | $0.0000 |
| 每 PR 成本 | $0.0000 |
| 输入 / 输出 token | 52093 / 2622 |
| 缓存命中率 | 0.0% |
| 延迟 p50 / p95 | 1 ms / 1 ms |

## 逐用例

| 用例 | 组 | 期望 | 发布 | 严格命中 | 假阳 | 冲突 | 耗时 |
|---|---|---:|---:|---:|---:|---:|---:|
| `clean-client` | clean | 0 | 0 | 0 | 0 | 0 | 1 ms |
| `clean-filepath` | clean | 0 | 0 | 0 | 0 | 0 | 1 ms |
| `clean-pagination` | clean | 0 | 1 | 0 | 1 | 0 | 1 ms |
| `clean-paging-helper` | clean | 0 | 0 | 0 | 0 | 0 | 1 ms |
| `clean-validation` | clean | 0 | 0 | 0 | 0 | 0 | 1 ms |
| `inj-blocking` | injected | 1 | 1 | 1 | 0 | 0 | 1 ms |
| `inj-cmdi` | injected | 1 | 1 | 1 | 0 | 0 | 0 ms |
| `inj-crypto` | injected | 2 | 2 | 2 | 0 | 0 | 0 ms |
| `inj-deser` | injected | 2 | 2 | 2 | 0 | 0 | 1 ms |
| `inj-mutable-default` | injected | 1 | 1 | 1 | 0 | 0 | 0 ms |
| `inj-pathtraversal` | injected | 1 | 1 | 1 | 0 | 0 | 0 ms |
| `inj-quadratic` | injected | 2 | 1 | 1 | 0 | 0 | 1 ms |
| `inj-random` | injected | 1 | 1 | 1 | 0 | 0 | 0 ms |
| `inj-secrets` | injected | 2 | 1 | 1 | 0 | 0 | 1 ms |
| `inj-sql-format` | injected | 1 | 1 | 1 | 0 | 0 | 0 ms |
| `inj-sql-fstring` | injected | 1 | 1 | 1 | 0 | 0 | 1 ms |
| `inj-ssrf` | injected | 1 | 0 | 0 | 0 | 0 | 1 ms |
| `inj-style` | injected | 5 | 3 | 3 | 0 | 0 | 1 ms |
| `inj-unbounded` | injected | 1 | 1 | 1 | 0 | 0 | 0 ms |
| `inj-xss` | injected | 1 | 1 | 1 | 0 | 0 | 0 ms |

## 用例说明

- `clean-client`（clean）：上游客户端：地址来自配置、超时显式给出、状态码显式检查。
- `clean-filepath`（clean）：正常的路径拼接：文件名来自白名单枚举，不含任何外部输入。测的是扫描器会不会把「os.path.join」一律当成路径穿越。
- `clean-pagination`（clean）：正常的分页查询：上限由 Page 决定，链式 .limit().all()。测的是扫描器会不会把「.all()」一律当成无上限查询。
- `clean-paging-helper`（clean）：纯函数：分页参数解析与收敛，全部带类型标注，异常收得干净。
- `clean-validation`（clean）：表单校验：正则预编译、错误用返回值而不是异常传递。
- `inj-blocking`（injected）：异步函数里用 time.sleep 退避，会卡住整个事件循环。
- `inj-cmdi`（injected）：把库名插进 shell 命令，分号与反引号会被解释。
- `inj-crypto`（injected）：用 MD5 做签名摘要；TLS 校验被关掉。
- `inj-deser`（injected）：客户端载荷直接 pickle.loads；yaml.load 未指定 Loader。
- `inj-mutable-default`（injected）：可变对象当默认参数，多次调用之间会共享同一个列表。
- `inj-pathtraversal`（injected）：文件名直接拼进路径，../ 可以逃出附件目录。
- `inj-quadratic`（injected）：循环里做字符串累加；循环条件里每轮重算长度。
- `inj-random`（injected）：用非密码学随机数生成密码重置令牌。
- `inj-secrets`（injected）：生产用的 API key 与数据库密码硬编码在源码里。
- `inj-sql-format`（injected）：用 .format() 把关键词插进 LIKE 子句，等价于字符串拼接。
- `inj-sql-fstring`（injected）：f-string 拼接 SQL，用户输入可直接改写查询语义。
- `inj-ssrf`（injected）：目标 URL 由调用方给定，可指向内网元数据服务。
- `inj-style`（injected）：一次提交里同时留下：待办标记、== 比较 None、裸 except、单字母变量、print 残留。
- `inj-unbounded`（injected）：无上限查询：SELECT * 不加 limit，表一大就 OOM。
- `inj-xss`（injected）：评论正文用 innerHTML 插入，未转义的输入可以执行脚本。
