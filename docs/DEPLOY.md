# 部署上线（精简模式）

把 sfly 部署到公网上，让别人点开链接就能看到审查记录。

**总共约 30 分钟**，其中大部分是等 Render 构建（每次 3–5 分钟）。

部署的是**精简模式**：一个容器跑完整个系统（API + 编排器 + 三个 Worker 在
同一个进程里）。它和本地 `docker compose up` 起的七个容器用的是**同一套代码、
同一个 Dockerfile**，差别只有 `QUEUE_BACKEND` / `LOCK_BACKEND` 两个环境变量 —— 这是这个
项目最主要的那个卖点，部署完之后你可以自己在页面上验证它（顶栏会显示当前形态）。

| 要什么 | 干什么用的 | 花钱吗 |
|---|---|---|
| [Neon](https://neon.tech) 账号 | 线上数据库（Postgres） | 免费档够用 |
| [Render](https://render.com) 账号 | 跑后端那个容器 | 免费档，会休眠 |
| [Vercel](https://vercel.com) 账号 | 托管前端页面 | 免费 |

三个都能用 **GitHub 账号一键登录**，不需要信用卡。

> **免费档的代价**：Render 免费档在 15 分钟没人访问后休眠，下一个访客要等
> 约 30–60 秒才能打开页面。前端为此做了「正在唤醒服务」的提示（而不是白屏），
> 但那个等待本身是躲不掉的。如果这个链接要发给很多面试官，可以考虑升级到
> 付费档（约 $7/月，不休眠）—— 改一个下拉框就行。

---

## 第 1 步 · Neon：建一个数据库

1. 登录 Neon → **Create project** → 名字随便填（比如 `sfly`）→ 区域选**离你近的**
   （新加坡 / 东京，如果列表里有）。
2. Neon 会给你**两条**连接串，长这样：

   ```
   postgresql://用户名:密码@ep-xxx.ap-southeast-1.aws.neon.tech/neondb?sslmode=require          ← Direct
   postgresql://用户名:密码@ep-xxx-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require   ← Pooled
   ```

   **两条都复制**，粘到项目根目录的 `.env` 里这一行（`NEON_DATABASE_URL=` 后面）：

   ```
   NEON_DATABASE_URL=把两条都粘在这里
   ```

   下面的命令会**自己挑出带 `-pooler` 的那一条**（Neon 面板上叫
   "Pooled connection"）—— 直连端点在免费档的并发上限低，而容器里有两个连接池。

3. 验证一下这条连接串真的能用（这一步会让 Neon 提前建好表，容器起来时就不用现建了）：

   ```bash
   python tasks.py db-check --name NEON_DATABASE_URL
   ```

   **你应该看到**最后一行是 `[OK] NEON_DATABASE_URL 可用（连接、权限、迁移都验过了）`，
   上面还会列出 `findings, llm_calls, review_reports, review_runs, run_events,
   schema_version, webhook_deliveries, worker_results` 八张表。

   > 这一步值得做：它走的是**和容器启动时同一条路**，所以连接、权限、SSL、
   > 以及我们的建表 SQL 全都提前验过了。不验的话，这些问题的表现是
   > 「Render 构建五分钟、部署成功、然后打开页面说数据库连不上」——
   > 排查方向会先跑到 Render 那边去。

---

## 第 2 步 · Render：部署后端

> **要选哪个仓库？`Satan508-h/sfly-code-review`** —— 就是装着这份代码的那个。
> （注意别和 `sfly-playground` 搞混：那是**被审查的靶场仓库**，第 5 步配 webhook
> 才用得到它。本地文件夹叫 `sfly-code-review-system`，那只是文件夹名，
> GitHub 上的仓库名不一样 —— 按仓库名找。）

1. 登录 Render → **New** → **Blueprint**。
2. 选 **GitHub** → 授权。授权那一步会让你选「允许 Render 看哪些仓库」——
   **`sfly-code-review` 必须在列表里**（选 All repositories，或者单独勾上它）。
   授权完回到 Render，选中 `Satan508-h/sfly-code-review` → **Connect**。
3. Render 会读出仓库里的 `render.yaml`，列出它要建的服务（`sfly-lite`）。
   点 **Apply**。
4. 接着它会**问你几个值**（`render.yaml` 里标了 `sync: false` 的那几个）。

   下面这张表就是答案 —— **注意哪些要来本项目复制、哪些要手打**：

   | 变量 | 值从哪来 |
   |---|---|
   | `DATABASE_URL` | 在本项目目录跑 `python tasks.py copy-env NEON_DATABASE_URL`，然后在这里 Ctrl+V。**注意变量名对不上是故意的**：`.env` 里的 `DATABASE_URL` 指向你本机的 Docker，粘上去线上连不上数据库 |
   | `LLM_API_KEY` | 同上，`python tasks.py copy-env LLM_API_KEY` |
   | `GITHUB_TOKEN` | 同上，`python tasks.py copy-env GITHUB_TOKEN` |
   | `GITHUB_WEBHOOK_SECRET` | 同上，`python tasks.py copy-env GITHUB_WEBHOOK_SECRET` |
   | `CORS_ORIGINS` | **先随便填**，第 4 步会回来改成真正的网址。现在填 `http://localhost:5173` |

   > `copy-env` 那条命令**不会把值打印到屏幕上**，它只把值放进剪贴板 ——
   > 密钥不该出现在终端历史、日志或者截图里。跑完直接去网页上粘贴。
   > **一次复制一条**，粘完再去复制下一条（剪贴板只有一格）。

5. 点 **Apply / Create**，然后等。第一次构建要 3–5 分钟。
6. 构建完，页面上会给你一个网址，形如：

   ```
   https://sfly-lite-xxxx.onrender.com
   ```

   **记下它**，下一步要用。第一次打开可能要等 60 秒（冷启动）。

**你应该看到**：打开 `https://你的网址/healthz`，返回
`{"ok": true, "service": "sfly-api", ...}`。

再打开 `https://你的网址/api/health`，应该看到 `"mode": "lite"`、
`"queue_backend": "memory"`，以及 `postgres: ok`、`redis: skipped`。
（`redis: skipped` 是**对的**：精简模式不需要 Redis，不是「连不上」。）

**这里出问题的话**：

- 构建失败 → 看 Render 的 Logs。多半是 Docker Hub 拉基础镜像超时，**再点一次
  Deploy** 通常就好了。
- 打开是 502 → 看 Logs 里有没有 `lite.starting`。没有的话是构建就没起来。
- `postgres: down` → `DATABASE_URL` 填错了。回去检查是不是粘成了本机那条，
  以及有没有带 `-pooler`。

---

## 第 3 步 · Vercel：部署前端

1. 登录 Vercel → **Add New** → **Project** → 选同一个仓库 → **Import**。
2. 在配置页上：

   | 设置项 | 填什么 |
   |---|---|
   | **Root Directory** | 点 **Edit** → 选 `web`（**这一项最容易漏**，不改的话 Vercel 会在仓库根目录找不到前端项目） |
   | Framework Preset | 会自动变成 Vite，不用改 |
   | **Environment Variables** | 加一条：名字 `VITE_API_BASE`，值 `https://你的Render网址/api` |

   > `VITE_API_BASE` 的末尾**一定要带 `/api`**。少了它的表现是页面能打开、
   > 但所有数据都是空的、控制台一堆 404。

3. 点 **Deploy**，等 1–2 分钟。
4. 拿到前端网址，形如 `https://sfly-xxxx.vercel.app`。**记下它**。

**你应该看到**：打开那个网址，能看到页面（可能顶部有一条红色提示，
说后端连接失败 —— 那是因为下一步还没做完，CORS 还没放行）。

---

## 第 4 步 · 回到 Render：放行前端的域名

浏览器的安全策略默认不允许一个域名去请求另一个域名，除非后端明确说
「这个来源可以」。所以要把上一步的前端网址告诉后端。

1. Render → 你的服务 → **Environment** → 找到 `CORS_ORIGINS`。
2. 把值改成你的 Vercel 网址（**不要带末尾的斜杠**）：

   ```
   https://sfly-xxxx.vercel.app
   ```

3. 保存 → Render 会自动重新部署（2–5 分钟）。

**你应该看到**：重新部署完成后，刷新前端页面，那条红色提示消失，顶栏右上角
变成绿点，显示 `精简模式 · memory · deepseek`。

---

## 第 5 步 · GitHub：让靶场仓库把 PR 事件发过来

到这一步后端已经就绪，但它还不知道有 PR 发生了。要让 GitHub 主动通知它。

1. 打开靶场仓库（`Satan508-h/sfly-playground`）→ **Settings** → **Webhooks** →
   **Add webhook**。
2. 填三样：

   | 字段 | 填什么 |
   |---|---|
   | **Payload URL** | `https://你的Render网址/api/webhook` |
   | **Content type** | `application/json`（**默认是别的，一定要改成这个**） |
   | **Secret** | 在本项目目录跑 `python tasks.py copy-env GITHUB_WEBHOOK_SECRET`，然后在这里 Ctrl+V（**必须和第 2 步填进 Render 的是同一个值**） |

3. 事件类型选 **Let me select individual events** → 只勾 **Pull requests**。
4. **Add webhook**。

**你应该看到**：加完之后 GitHub 会立刻发一条测试投递，那个 webhook 列表里
会出现一条记录，点开它 → **Recent Deliveries** → 应该是一个绿色的 `200`
（不是红色）。如果是红的，把 Response 那一栏的内容发我。

---

## 第 6 步 · 验收：在线上真的审一个 PR

1. 在靶场仓库开一个 PR（改点东西就行，比如改一行注释）。
2. 打开前端网址，应该**几秒钟内**出现一条新的审查记录，状态从「排队中」
   一路走到「已发布」。
3. 回到那个 PR 页面，应该看到机器人发出的审查评论。

**这一步就是简历上「可部署上线」那句话的证据。** 建议截两张图：
一张前端的时间线页面，一张 PR 上那条评论。

> 如果 PR 开了但前端一直没有新记录：先看 GitHub webhook 的 Recent Deliveries
> 是不是 200，再看 Render 的 Logs 里有没有 `webhook.accepted`。
> 两者能定位到是「没发过来」还是「发过来了但没处理」。

---

## 花钱的闸在哪

公网上放一个真花钱的服务，最该先说清楚的是它最多花多少。

- **每天 45 次真实模型调用**（= 15 次审查，一次审查三个 Worker）。
  超了会自动降级成**确定性扫描器**（不花钱），报告照样生成，但页面上会多一个
  「结果来自规则扫描器，不是模型」的徽章 —— 访客看到的东西是诚实的。
- **每天 $2 的金额上限**，和次数是两道独立的闸。
- 按实测每 PR 约 $0.006 估，满额一天约 $0.1 —— **一个月最坏情况约 $3**。

想临时完全停掉花钱的调用：把 Render 上的 `ENABLE_REAL_LLM` 改成 `false`，
重新部署。所有审查会走扫描器，一分钱不花。

> 想自己看今天花了多少：`llm_calls` 表里就是。Render 没有直接的界面，
> 可以用 Neon 的 SQL Editor 跑：
> `SELECT count(*), sum(cost_usd) FROM llm_calls WHERE created_at >= date_trunc('day', now());`

---

## 出问题时的排查顺序

从上往下走，每一步都能排除掉一层：

1. **后端活着吗** —— 打开 `https://你的Render网址/api/health`。
   - 打不开 / 502 → 冷启动中，等 60 秒。一直这样就是构建或启动有问题，看 Logs。
   - `"ok": false` → 看 `checks` 里哪个是 `down`。
2. **前端连得上后端吗** —— 浏览器按 F12 → Console。
   - 一堆 CORS 错误 → 第 4 步的 `CORS_ORIGINS` 不对（末尾斜杠、拼写、
     或者忘了重新部署）。
   - 404 → `VITE_API_BASE` 少了 `/api`，或者改完之后没在 Vercel 重新部署
     （Vite 的变量是**构建时**写进产物的，改完必须重新构建）。
3. **GitHub 发过来了吗** —— 仓库 Settings → Webhooks → Recent Deliveries。
   - 没有记录 → 事件类型没勾 Pull requests。
   - 401 → Secret 和 Render 上的不一致。
   - 200 但没有审查 → 看 Render Logs 里 `webhook.accepted` 后面那一行。
4. **审查跑完了吗** —— 前端点进那条记录，看时间线停在哪个节点。

---

## 已知限制

- **冷启动 30–60 秒**，躲不掉（免费档的代价，见开头）。
- **不能用 `--scale`**：精简模式是一个进程，没有第二个进程可以加入消费者组。
  水平扩展是完整模式的能力，本地 `docker compose up --scale worker-security=3` 可见。
- **断点恢复演示要在完整模式下做**：精简模式里进程死了就是整个系统死了，
  重启靠 Render 拉起容器，而不是靠 `review_runs.deadline_at` 那条恢复查询。
- **`GITHUB_TOKEN` 90 天到期**。到期后表现为「报告正常生成，但 PR 上一直没有评论」
  （`publish.done` 事件里 `posted=false`）。到时候重新签发一个，
  在 Render 上更新 `GITHUB_TOKEN` 即可。
