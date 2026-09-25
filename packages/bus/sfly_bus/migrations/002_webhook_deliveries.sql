-- 002_webhook_deliveries.sql —— webhook 投递去重表。
--
-- 001_init.sql 里的六张表都是「一次审查的产物」，而这张表是**入口的账本**：
-- 每收到一个 GitHub webhook 就写一行，答一个「这次投递后来怎么样了」。
--
-- 它解决两件不同的事，混在一起会让去重看起来像一件事而实际上是两件：
--
--   1. **同一个 delivery id 重复到达** —— GitHub 超时后会重投（也可以在
--      Settings → Webhooks → Recent Deliveries 里点 Redeliver），重投时的
--      X-GitHub-Delivery **和第一次相同**。主键去重拦的就是它。
--   2. **不同的 delivery、同一个 PR 的同一个 head_sha** —— 推送事件和
--      pull_request 事件可能同时到，或者同一个提交被反复推送。这时 delivery
--      是新的，但幂等键（repo:pr:head_sha）指向同一个 run。这一层由
--      review_runs.idempotency_key 的唯一约束兜住，不归这张表管。
--
-- 两层各管各的，缺一层就会在某些路径上重复审查 —— 或者重复花钱。

CREATE TABLE webhook_deliveries (
    -- X-GitHub-Delivery，GitHub 生成的 GUID。**主键就是去重本身**：
    -- 并发投递同一个 delivery 时，赢的那个 INSERT 成功、输的那个撞主键，
    -- 不需要锁，也不需要先查后写（先查后写在并发下两边都会读到"不存在"）。
    delivery_id  text PRIMARY KEY,

    -- X-GitHub-Event。载荷类型（pull_request / ping / push...）。
    -- 存下来是因为「收到了一条我们不认识的事件」是排查时的第一个问题。
    event        text NOT NULL DEFAULT '',

    repo_id      text NOT NULL DEFAULT '',
    pr_number    integer,

    -- received  = 已受理、处理中。**这个状态是可以被下一次重投接管的** ——
    --             进程在写完这一行之后、投递 bootstrap 之前崩掉的话，
    --             这一行会永远停在 received，而那次投递其实什么也没做成。
    -- accepted  = 已投递（产生了新 run，或复用了一个已存在的 run）
    -- duplicate = 重复投递（同一个 delivery id 之前已经受理过）
    -- ignored   = 事件类型或动作不关心（比如 pull_request closed）
    -- rejected  = 载荷本身不合法（验签通过了，但缺字段）
    --
    -- 取值集合必须与 contracts.DeliveryStatus 一致，有单测逐个比对
    -- （tests/unit/bus/test_migrations.py 里的同一套检查）。
    status       text NOT NULL DEFAULT 'received'
                 CHECK (status IN ('received', 'accepted', 'duplicate', 'ignored', 'rejected')),

    -- 关联到的 run。**故意不加外键** —— 建 run 的是编排层的 ingest 节点，
    -- 而这边只是记一个「我把它转给了谁」。加外键的话，一个指向还没建出来的
    -- run 的投递会直接违反约束，而那是完全正常的时间差（API 先返回、
    -- 编排器随后建）。
    task_id      text,

    -- 为什么被忽略/拒绝。没有它，运维只能看到「投递收到了但什么也没发生」。
    reason       text,

    received_at  timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz
);

-- 运维查询的形状：「最近收到过哪些 webhook」。与 review_runs 不同，
-- 这张表的主键是 GitHub 给的 GUID（没有时间前缀），所以列表要自己的索引。
CREATE INDEX webhook_deliveries_received_idx ON webhook_deliveries (received_at DESC);
