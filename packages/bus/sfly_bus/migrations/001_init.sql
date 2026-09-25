-- 001_init.sql —— 初始表结构。
--
-- **这个文件一旦被应用过就不能再改。** 迁移器把它的 sha256 记在 schema_version
-- 里，改了而库没重建 = 启动时直接报错。要改结构就加 002_*.sql。
-- 理由见 migrations/__init__.py —— 那条规则的代价在这个文件里是「不能再顺手改一下」，
-- 收益是「线上库和仓库里的结构不可能悄悄分叉」。
--
-- 七张表：六张业务表 + 一张记账表。schema_version 由迁移器自己建，不在这里。
--
--   review_runs     一次审查 = 一行。**deadline_at 是所有恢复逻辑的主干**
--   worker_results  Worker 的一次上报 = 一行。主键 (task_id, worker_type) 是幂等性的真正保证
--   findings        发现逐条展开（Worker 原始输出），供评测按类目/文件/严重度统计
--   review_reports  主 Agent 的最终产物（jsonb）。publish 失败时报告仍然在这里
--   run_events      SSE 事件的权威来源（SSE 只是快路径，断线靠这张表补齐）
--   llm_calls       每次 LLM 调用的 token 与成本。评测的每个数字都是这张表的 SUM
--
-- SQL 里的列名与 contracts.py 的字段逐字对应，不另起名字：多一层名字映射，
-- 排查时就多一步「这个字段在库里叫什么」的心算，而它没有任何收益。

-- --------------------------------------------------------------------------- #
-- 一次审查
-- --------------------------------------------------------------------------- #

CREATE TABLE review_runs (
    task_id           text PRIMARY KEY,
    -- 幂等键 = repo_id:pr_number:head_sha。**UNIQUE 才是重复投递的唯一保证** ——
    -- Redis 的 SETNX 会过期、会随重启丢失，这里不会。
    idempotency_key   text NOT NULL UNIQUE,
    repo_id           text NOT NULL,
    repo_node_id      text NOT NULL DEFAULT '',
    pr_number         integer NOT NULL,
    head_sha          text NOT NULL,
    base_sha          text NOT NULL DEFAULT '',

    -- 状态机。取值集合必须与 contracts.RunStatus 一致 ——
    -- 有一份单测把这个 CHECK 里的字面量和枚举逐个比对（tests/unit/bus/test_migrations.py）。
    -- 写错一个 state 名的后果是没有任何地方报错：due_runs 捞不到它、UI 显示成未知状态，
    -- 而 run 就那么停在那里。
    status            text NOT NULL DEFAULT 'queued'
                      CHECK (status IN ('queued', 'dispatched', 'waiting', 'aggregating',
                                        'published', 'publish_failed', 'failed', 'skipped')),
    attempt           integer NOT NULL DEFAULT 1,

    files_total       integer NOT NULL DEFAULT 0,
    files_reviewed    integer NOT NULL DEFAULT 0,
    diff_truncated    boolean NOT NULL DEFAULT false,

    -- 为什么是 text[] 而不是 jsonb：这两个列表的访问方式只有 = ANY / @>，
    -- 数组的原生操作符比 jsonb 的路径查询短得多，而且 psycopg 能直接映射 list[str]。
    planned_workers   text[]  NOT NULL DEFAULT '{}',
    missing_workers   text[]  NOT NULL DEFAULT '{}',

    -- NOT NULL 且没有默认值：**任何可能卡住的状态都必须是一行带 deadline 的记录**。
    -- 建 run 时就填（= 现在 + RUN_DEADLINE_S），plan 节点可以覆盖它（比如大 PR 放宽）。
    -- 不允许为空是因为「没有 deadline」正好等于「永远不会被扫描器捞起来」——
    -- 那正是这条约定要防的东西。
    deadline_at       timestamptz NOT NULL,

    dispatched_at     timestamptz,
    published_at      timestamptz,
    -- publish 节点发帖**之前**先查这个字段 —— 第一道防重复评论的闸
    -- （第二道是评论正文里的隐藏标记 <!-- sfly:run:{task_id} -->）
    github_comment_id bigint,
    -- 可空 = 「还没做决定」，与 false（决定了不阻断）是两件不同的事
    block_merge       boolean,
    degraded          boolean NOT NULL DEFAULT false,

    -- jsonb：RunTotals 的形状会随评测演进（per_worker_ms / cache_hit_rate），
    -- 而它只被整体读写、从不按字段筛选，所以不值得展开成列。
    totals            jsonb,

    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- 超时扫描器的查询就是这个索引的形状：
--   SELECT * FROM review_runs WHERE status IN ('dispatched','waiting') AND deadline_at <= now()
-- 做成**部分索引**（只索引会超时的那两个状态）：已完成的 run 占了表里的绝大多数，
-- 而它们永远不会被这条查询扫到。扫描是每 15 秒一次的常驻查询，值得单独优化。
CREATE INDEX review_runs_due_idx ON review_runs (deadline_at)
    WHERE status IN ('dispatched', 'waiting');

-- 列表接口按 task_id 倒序（ULID 前 48 位是毫秒时间戳，见 sfly_shared/ids.py），
-- 所以不需要额外的 created_at 索引 —— 主键索引就够了。

-- --------------------------------------------------------------------------- #
-- Worker 上报
-- --------------------------------------------------------------------------- #

CREATE TABLE worker_results (
    task_id          text NOT NULL REFERENCES review_runs (task_id) ON DELETE CASCADE,
    worker_type      text NOT NULL
                     CHECK (worker_type IN ('security', 'performance', 'style')),

    -- ok / partial / failed。**failed 也是一条正常的结果** ——
    -- Worker 放弃前必须先写它，否则 wait 节点的屏障永远闭合不了
    -- （CLAUDE.md 约定 #2）。所以这张表里没有「失败就不写」的余地。
    status           text NOT NULL
                     CHECK (status IN ('ok', 'partial', 'failed')),
    error            text,
    error_class      text
                     CHECK (error_class IN ('transient', 'llm_timeout', 'llm_http_error',
                                            'db_unavailable', 'schema_unrecoverable',
                                            'diff_too_large', 'repo_not_found', 'auth_revoked')),

    tokens_in        integer NOT NULL DEFAULT 0,
    tokens_out       integer NOT NULL DEFAULT 0,
    cached_tokens    integer NOT NULL DEFAULT 0,
    latency_ms       integer NOT NULL DEFAULT 0,
    model            text,
    -- 解析彻底失败时保留的原文（上游已截断到 8KB）。没有它就无法改进提示词。
    raw_response     text,
    dropped_findings integer NOT NULL DEFAULT 0,
    attempt          integer NOT NULL DEFAULT 1,
    finished_at      timestamptz NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),

    -- **这个主键就是幂等性本身。** 与 INSERT ... ON CONFLICT DO NOTHING 配合：
    -- 同一条任务被回收后重跑，第二次写库静默无效，结果不会被覆盖成两次。
    -- Redis 的 SETNX 只是省 token 的快路径，重启就没了。
    PRIMARY KEY (task_id, worker_type)
);

-- 主键已经覆盖了 (task_id, worker_type) 开头的查询，所以
-- completed_workers / get_results / exists_result 都不需要额外索引。

-- --------------------------------------------------------------------------- #
-- 发现（逐条展开）
-- --------------------------------------------------------------------------- #

CREATE TABLE findings (
    id           bigserial PRIMARY KEY,
    task_id      text NOT NULL,
    worker_type  text NOT NULL
                 CHECK (worker_type IN ('security', 'performance', 'style')),

    file         text NOT NULL,
    line         integer NOT NULL,
    end_line     integer,
    severity     text NOT NULL
                 CHECK (severity IN ('critical', 'high', 'medium', 'low', 'info')),
    category     text NOT NULL,
    message      text NOT NULL,
    evidence     text,
    confidence   double precision NOT NULL DEFAULT 0.5,
    suggestion   text,
    rule_id      text,
    -- line 是否落在 diff 的变更行上：为 false 时发布阶段降级成文件级评论
    -- （GitHub 会 422 拒绝锚定在未变更行上的 inline 评论）
    source_line_verified boolean NOT NULL DEFAULT false,
    fingerprint  text,
    created_at   timestamptz NOT NULL DEFAULT now(),

    -- 复合外键指向上面的主键：一个结果被删时它的发现自动跟着走，
    -- 而「结果不存在却有发现」这种半写状态在数据库层就不可能存在。
    FOREIGN KEY (task_id, worker_type)
        REFERENCES worker_results (task_id, worker_type) ON DELETE CASCADE
);

-- Postgres **不会**为外键的引用列自动建索引，而级联删除需要它：
-- 没有这个索引，删一行 worker_results 都要对 findings 做一次全表扫描。
-- 顺带覆盖了「按 run 取全部发现」这个唯一的高频读法。
CREATE INDEX findings_task_idx ON findings (task_id, worker_type);

-- 刻意不建 category / severity 的单列索引：评测数据量在几千行量级，
-- 现在加是提前优化。真需要时（M9 有了真实数据）再加，那时才知道该建什么。

-- 这里**只放 Worker 的原始输出**（stage=raw 那一层）。聚合之后的发现
-- （去重簇、被置信度闸砍掉的 suppressed）整体存在 review_reports.report 里 ——
-- 它们的形状是「一个簇 + 多个来源 Worker」，硬塞进这张按 (task_id, worker_type)
-- 组织的表会让一半的列对一半的行是空的。

-- --------------------------------------------------------------------------- #
-- 最终报告
-- --------------------------------------------------------------------------- #

CREATE TABLE review_reports (
    task_id          text PRIMARY KEY REFERENCES review_runs (task_id) ON DELETE CASCADE,
    -- 完整的 ReviewReport（含 findings / suppressed / conflicts / totals）。
    -- 一份 jsonb 而不是拆成四张表：评测和新版 UI 要的永远是「整个报告」，
    -- 而它只在 finalize 写一次、被读到几次。
    report           jsonb NOT NULL,

    -- 下面这几列是冗余的，**故意冗余**。两类查询不该为了拿一个数字去解 jsonb：
    --   * 运行列表要显示成本与条数
    --   * 评测要按类目/严重度做全库聚合
    --   * publish 失败后要能直接把 comment_body 重新发出去（报告不能丢）
    findings_count   integer NOT NULL DEFAULT 0,
    suppressed_count integer NOT NULL DEFAULT 0,
    conflicts_count  integer NOT NULL DEFAULT 0,
    block_merge      boolean NOT NULL DEFAULT false,
    degraded         boolean NOT NULL DEFAULT false,
    comment_body     text    NOT NULL DEFAULT '',
    tokens_in        bigint  NOT NULL DEFAULT 0,
    tokens_out       bigint  NOT NULL DEFAULT 0,
    cached_tokens    bigint  NOT NULL DEFAULT 0,
    -- numeric 而非 float：这是钱。聚合时在 SQL 里 ::float8，账目本身不丢精度。
    cost_usd         numeric(12, 6) NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------------- #
-- SSE 事件
-- --------------------------------------------------------------------------- #

CREATE TABLE run_events (
    -- **全局自增，不是每个 task 各自编号。**
    -- 同一个 task 的事件有两个写入方（Worker 报结果、编排器报节点状态），
    -- 按 task 编号就得先 SELECT max(seq)+1 —— 那是一次竞态：两个写入方会拿到同一个号，
    -- 而症状是「时间线上两条事件序号相同」，前端去重逻辑直接失效。
    -- 全局序列没有这个问题，且对单个 task 仍然单调（后写的一定拿到更大的号）。
    seq        bigserial PRIMARY KEY,
    task_id    text NOT NULL REFERENCES review_runs (task_id) ON DELETE CASCADE,
    -- 取值是 contracts.RunEvent 上的一个 Literal（不是 Enum），所以这里没有 CHECK：
    -- 写错只影响时间线的展示，不影响任何判定逻辑，而每加一个事件类型都要改
    -- 一次约束的代价更高。enum 列（status/worker_type/severity/error_class）才加。
    kind       text NOT NULL,
    payload    jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Last-Event-ID 补齐：WHERE task_id = $1 AND seq > $2 ORDER BY seq
CREATE INDEX run_events_task_idx ON run_events (task_id, seq);
-- 保留策略按时间删（Neon 免费版只有 0.5GB）
CREATE INDEX run_events_created_idx ON run_events (created_at);

-- --------------------------------------------------------------------------- #
-- LLM 成本
-- --------------------------------------------------------------------------- #

CREATE TABLE llm_calls (
    id            bigserial PRIMARY KEY,
    -- **刻意不建外键。** 成本记录要能在 run 被清理之后存活 ——
    -- 「今天花了多少钱」不该因为清理了 14 天前的 run 而变小。
    -- 这也意味着 task_id 可以是 NULL：独立 CLI 跑一次审查没有 run。
    task_id       text,
    agent         text NOT NULL,
    model         text NOT NULL,
    tokens_in     integer NOT NULL DEFAULT 0,
    tokens_out    integer NOT NULL DEFAULT 0,
    cached_tokens integer NOT NULL DEFAULT 0,
    cost_usd      numeric(12, 6) NOT NULL DEFAULT 0,
    latency_ms    integer NOT NULL DEFAULT 0,
    ok            boolean NOT NULL DEFAULT true,
    error_class   text,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- sum_costs(task_id)：单次 run 的成本
CREATE INDEX llm_calls_task_idx ON llm_calls (task_id);
-- 每日预算（M6 公网限流）：WHERE created_at >= date_trunc('day', now())
CREATE INDEX llm_calls_created_idx ON llm_calls (created_at);
