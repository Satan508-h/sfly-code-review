-- 只在容器**首次**初始化数据目录时执行（docker-entrypoint-initdb.d 的语义）。
-- 改了这个文件后要 `docker compose down -v` 才会重新生效。
--
-- 这里**故意只放扩展，不放表结构**。
--
-- 原因：表结构由应用启动时的幂等 migrate() 创建，本地容器和 Neon 云上
-- 走的是同一条代码路径。如果在这里建表，就会出现「本地能跑、线上缺表」
-- 这种只在部署时才暴露的偏差 —— 而 Neon 上根本执行不到这个文件。
--
-- 同理，Neon 上需要手工执行的也就只有下面这几行扩展。

CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- pg_trgm 给模糊字符串匹配加索引支持。聚合阶段的 rapidfuzz 去重在 Python
-- 侧做，但排查问题时经常需要直接在 SQL 里 LIKE 相似消息，有它快得多。

-- M11 可选升级：向量检索。启用后需要把 fastembed 加进 sfly-agent 依赖。
-- 本地和 Neon 都要手动执行这一行，所以默认注释掉。
-- CREATE EXTENSION IF NOT EXISTS vector;

-- 让 psql 里的时间戳好读一些（仅影响当前会话的默认显示）
ALTER DATABASE sfly SET timezone TO 'UTC';
