# 部署到公网（国内轻量云 2C4G）

本文按「腾讯云/阿里云轻量应用服务器 2 核 4G，Ubuntu 22.04」写，都是可以照抄的命令。
其它云、其它配置也能用，只是买机器的入口不同。

---

## 0. 先搞清楚要部署的是什么

```
浏览器 → nginx:80（入口，负责 request_id、SSE 不缓冲、上传大小限制）
            ↓
         app:8000（FastAPI + ReAct Agent + Chroma 向量库 + SQLite）
            ↓
         redis:6379（只做知识库列表缓存，挂了服务照常用）
```

三个关键事实，决定后面所有操作：

| 事实 | 影响 |
|---|---|
| LLM 和 embedding 都走 DashScope API（`qwen3.8-flash` / `text-embedding-v4`） | **不需要 GPU**，也不需要大内存来跑模型 |
| 唯一本地模型是 reranker（`bge-reranker-v2-m3`），惰性加载 | 空载 620MB，加载后约 3GB → 2C4G 够用，但要加 swap |
| 镜像 6.81GB（torch 占大头） | **在服务器上构建**，别想着本地 build 再上传 |

需要搬过去的数据只有 11MB：`data/app.db`（184K）+ `chroma_db/`（9.9M）+ `uploads/`（300K）。

---

## 1. 买机器 + 放行端口

**配置**：2 核 4G，系统盘 **60G**（镜像 6.8G + 构建缓存 + 模型权重，40G 会很紧张），
镜像选 **Ubuntu 22.04 LTS**。

**控制台防火墙**（这一层脚本管不到，必须手动点）：
- `TCP:22` — SSH
- `TCP:80` — 应用入口

> 想先用 8080 试，就再放行 `TCP:8080`，并把下面 `.env` 里的 `ZST_HTTP_PORT` 改成 8080。
> 国内服务器绑域名必须备案（2–3 周），**只用 IP 访问不需要备案**。

---

## 2. 初始化服务器

```bash
ssh root@<公网IP>

# 把脚本传上去（在你自己电脑上执行）
scp deploy/server-setup.sh root@<公网IP>:/root/

# 回到服务器执行
bash /root/server-setup.sh
```

脚本做四件事，都有原因：

1. **装 Docker**（`--mirror Aliyun`，官方源在国内经常超时）
2. **配镜像加速器** —— 不配的话 `docker pull nginx` 会卡死在 `docker.io`。阿里云用户去控制台「容器镜像服务 → 镜像加速器」拿专属地址，加进 `/etc/docker/daemon.json` 最快
3. **配日志轮转** —— 应用是 JSON 日志，不限制的话几个月后能把磁盘写满
4. **加 2GB swap** —— 这是 2C4G 的关键。reranker 加载要 2.2GB，加上 app 本身 620MB 和系统开销，4G 内存会摸到上限，没 swap 就是 OOM Kill

---

## 3. 拉代码

部署前**务必先把本地改动推上去**（现在本地还有 46 个文件没提交，W5–W7 的全部工作都在里面）：

```bash
# 本地执行
git add -A
git commit -m "feat: 知识库/文档异步入库 + 检索隔离 + 可观测 + 容器化"
git push origin master
```

然后服务器上，两条路选一条：

**A. git clone（推荐，方便以后 `git pull` 更新）**

```bash
mkdir -p /opt && cd /opt
# 国内直连 github 经常几 KB/s，走 ghproxy 镜像
git clone https://ghproxy.net/https://github.com/Mooncake12-star/saojingling-agent.git
cd saojingling-agent
```

**B. 本地打包上传（GitHub 实在拉不动时）**

```bash
# 本地执行：排除数据、日志、密钥、git 历史
tar czf saojingling-src.tar.gz \
    --exclude='.git' --exclude='data' --exclude='chroma_db' \
    --exclude='uploads' --exclude='logs' --exclude='.env' \
    --exclude='__pycache__' --exclude='*.db'
scp saojingling-src.tar.gz root@<公网IP>:/opt/

# 服务器执行
cd /opt && mkdir -p saojingling-agent && tar xzf saojingling-src.tar.gz -C saojingling-agent
```

---

## 4. 传数据（强烈建议做）

不传的话，服务器上是一个空库：没有商品数据、没有公共知识库，Agent 问什么都是瞎编，
演示效果直接废掉。

```bash
# 本地执行，11MB，几秒钟
cd "D:/大模型开发/langchain1.2/大模型入门/Agent扫净灵项目"
scp -r data chroma_db uploads root@<公网IP>:/opt/saojingling-agent/
```

`data/` 里跟部署无关的两个文件可以不带：`zst.db`（旧库备份）、`_app_backup_before_kb.db`（迁移前备份）。

---

## 5. 写 .env

`docker compose` 会自动读工作目录下的 `.env`，**这个文件不进仓库**（已被 `.gitignore` 覆盖）。

```bash
cd /opt/saojingling-agent

# 生成一个真的随机密钥，不要用默认值：
#   secret 泄露 = 任何人都能自己签一个 token 冒充任意用户
SECRET=$(openssl rand -hex 32)

cat > .env <<EOF
DASHSCOPE_API_KEY=<你的百炼 API Key>
ZST_JWT_SECRET=$SECRET
ZST_INVITE_CODE=<自己想一个邀请码，比如 zst-2026-x7k>
ZST_HTTP_PORT=80
PRELOAD_MODEL=0
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
EOF

chmod 600 .env          # 只有 root 能读，防止同机其它账号看到密钥
```

几个值的理由：

- **`ZST_INVITE_CODE` 必须设**。不设就是「谁都能注册」，而注册一次就能用你的 DashScope 额度跑对话和 RAG——公网裸奔等于把钱包挂在门口。设了之后注册接口要带邀请码，否则 403
- **`PRELOAD_MODEL=0`**。构建时预热模型会让构建进程吃 2GB+ 内存，2C4G 上大概率 OOM。改成运行期下载，只在第一次用到 reranker 时等一次
- **`PIP_INDEX_URL`** 走清华源，装 6GB 依赖从几十分钟降到几分钟

---

## 6. 构建

```bash
cd /opt/saojingling-agent
docker compose build app
```

首次约 **10–20 分钟**（取决于机器带宽）。构建失败先看两件事：

- `failed to resolve source metadata for docker.io/...` → 镜像加速器没生效，`systemctl restart docker` 再试
- `Killed` / `exit code 137` → 内存不够，确认 `.env` 里 `PRELOAD_MODEL=0`，swap 已启用（`free -h` 能看到 Swap 那行）

---

## 7. 启动与验收

```bash
docker compose up -d
docker compose ps          # 三个都是 Up，app 显示 (healthy)
```

**逐条验收**（这几条过了，就是真的能用了）：

```bash
# 1) 服务活着
curl -s http://localhost/healthz
# 期望：{"status":"alive"}

# 2) 能不能接流量（就绪探针）
curl -s http://localhost/readyz | python3 -m json.tool
# 期望：ready: true，db: ok，mcp: ok
# 同时能看到：
#   reranker.state = ready     重排模型已预热（不是加载中）
#   chat_gate.limit = 16       同时允许 16 个对话
#   两者的意义见第 10 节
#
# 注意：冷启动后 reranker 后台加载需要 30~60 秒，这段时间 readyz 已经是 200
# （重排可降级），但 reranker.state 会是 loading。想看真正就绪，等这一项变 ready

# 3) 邀请码生效
docker compose logs app | grep invite
# 期望：看到 "invite_code": "on"。
# 如果是 "off"，说明 .env 里 ZST_INVITE_CODE 没传进去，注册口子是敞开的

# 4) 缓存接上了
curl -s http://localhost/health
# 期望："cache":"redis"

# 5) request_id 跨服务串联
curl -s -o /dev/null -H "X-Request-ID: deploy-check" http://localhost/health
docker compose logs app   | grep deploy-check   # 有 http.access 日志
docker compose logs nginx | grep deploy-check   # 同一个 rid
```

然后浏览器打开 `http://<公网IP>/docs`，用 Swagger 走一遍：

1. `POST /auth/register` 带 `invite_code` 注册 → 201
2. 右上角 **Authorize**，username 填邮箱、password 填密码 → 登录
3. `POST /knowledge-bases` 建库 → `POST /knowledge-bases/{id}/documents` 传一个 PDF → 返回 202
4. `GET /documents/{id}` 轮询，几秒后 `status` 变 `ready`
5. `POST /chat` 问一个文档里的问题 → 能答对，说明「检索隔离 + RAG」在公网环境也通了

---

## 8. 日常运维

```bash
docker compose ps                       # 状态
docker compose logs -f app              # 跟日志（JSON 一行一条）
docker compose logs app | grep <rid>    # 按 request_id 捞一次请求的完整链路
docker compose restart app              # 重启应用（不动数据）

# 更新代码：镜像里是 COPY 进去的，改代码必须重新 build
git pull
docker compose build app && docker compose up -d

# 备份（SQLite 是单文件，直接复制即可；建议每天定时跑）
mkdir -p /opt/backup
cp data/app.db /opt/backup/app-$(date +%F).db
tar czf /opt/backup/chroma-$(date +%F).tar.gz chroma_db
```

`data/`、`chroma_db/`、`uploads/`、`logs/` 都是 bind mount 到宿主机的，容器重建不会丢。

---

## 9. 这套部署还剩的边界（面试被问到要能说清）

1. **没有 HTTPS**。只有 IP + http，JWT 在公网是明文传输。演示够用，别拿它当真账号用。
   要上 HTTPS：得有域名（国内服务器还要备案），然后 nginx 加 443 + 证书（Let's Encrypt 或云厂商免费证书）。
2. **没有按用户限流**。对话闸门是「全实例总量」保护（第 10 节），
   挡得住雪崩，但挡不住「一个账号疯狂刷」。要按用户限，得有 Redis 计数器 + 每用户配额，
   当前只有邀请码 + 百炼控制台的额度上限兜底。
3. **仍然是单机**。机器挂了就没了，`restart: unless-stopped` 只能救「进程崩」，
   救不了「机器宕」。要真高可用，得两台机器 + 一个负载均衡 + 外部数据库，
   这是另一个量级的成本，演示项目不做。
4. **重排模型每副本一份**。这是内存的主要开销（~1.1GB/副本），也是副本数的硬约束。
   想在一台机器上开更多副本，唯一的路是把重排拆成独立推理服务（`rag/reranker.py`
   换成 HTTP 调用），本项目没做。

---

## 10. 并发与高可用：改了什么、能撑多少

### 10.1 压测暴露的四个串行点

100 并发对话压测（改前）的结果：300 个请求 280 个 500，随后 OOM。日志里能定位到四个串行点，
每一个都不是「代码写错」，而是「来多少接多少」的必然结果：

| # | 位置 | 现象 | 根因 |
|---|---|---|---|
| 1 | `rag/local_tool.py` 的全局锁 | 3 个用户 58.5s / 66.8s / 71.6s，严格串行 | 锁包住了**整个构造过程**（含加载模型 40~60s），别的 scope 全在排队 |
| 2 | `rag_service._get_bge_reranker()` | 内存 700MB → 1.53GB，之后卡死不报错 | 无互斥的 double-check：几十个线程同时看到 None，各加载一份 1.5GB 模型 |
| 3 | 重排推理本身 | 线程池被占满，连不带模型的接口也超时 | CPU 密集 + 无限并发 = 互相抢 GIL，总吞吐不升反降 |
| 4 | 没有任何背压 | 504 / OOM，而不是「忙」 | 请求全收，慢请求吃光线程和连接 |

### 10.2 四处改造

| 改造 | 文件 | 做法 |
|---|---|---|
| 构造去全局锁 | `rag/local_tool.py` | per-key `Event` 去重（同 scope 只建一次）+ 不同 scope 并行 + 全局构造闸门（默认 2）+ 等待超时抛 `RagBuildBusy` |
| 模型单例化 | `rag/reranker.py`（新） | 一把加载锁 + 状态机（idle/loading/ready/failed）+ 加载中**不阻塞请求**，直接降级 |
| 推理限并发 | `rag/reranker.py` | `BoundedSemaphore` 限制同时推理数（默认 2），排队超 2 秒就跳过精排，保留 RRF 顺序 |
| 背压 | `fastapi_app/concurrency.py`（新） | 对话全局信号量（默认 16），排队 3 秒拿不到位子就 503 + `Retry-After` |

三个档位的降级链路（写进代码注释了，这里是结论）：

```
模型 ready   → BGE 精排（100~300ms）
模型加载中   → 直接用 RRF 顺序（不调 LLM，因为那 40 秒里 LLM 配额会被重排请求打满）
依赖缺失/失败 → LLM 重排（3~5s，功能不变，只是慢）
```

### 10.3 现在能撑多少

以 2C4G + 单副本、单轮对话 40~80 秒为基准：

| 场景 | 改前 | 改后 |
|---|---|---|
| 浏览类接口（列表/详情） | 300/300 通过 | 不变（本来就没问题） |
| 对话并发 3 | 串行，58~72 秒 | 并行，各自 40~50 秒 |
| 对话并发 100 | 雪崩（500 / OOM） | 16 个在跑，84 个 3 秒内收到 503 |
| 吞吐 | ≈0.15 QPS（被锁串行化） | ≈16/70 ≈ **0.23 QPS**，且不雪崩 |
| 过载表现 | 全员超时，无法恢复 | 快速失败，压力退去自动恢复 |

**0.23 QPS 不是「解决了高并发」，是「过载时不再死」。** 要更高吞吐，只有两条路：
加副本（下一节），或把重排/生成换成更快的实现。

### 10.4 开多副本（真高可用）

前提：**4C8G 以上**。每副本 ~1.8GB（基础 0.7G + 重排模型 1.1G），2 副本 + PG + 系统 ≈ 4.5GB，
2C4G 上开 2 个必然 OOM。

```bash
# .env 里加
ZST_PG_PASSWORD=<随机密码>
ZST_APP_REPLICAS=2

# 启动（叠加层：PostgreSQL + 2 副本 + 内存上限）
docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d --build

# 验证
docker compose -f docker-compose.yml -f docker-compose.ha.yml ps   # app 显示 2 个
curl -s localhost:8080/readyz   # 多刷几次，mcp/reranker 计数会变化
```

这套叠加层动了四处，每一处都是多副本的必要条件：

1. **SQLite → PostgreSQL**。SQLite 是单文件 + 单写锁，跨容器共享不可靠。
   代码不用改——`ZST_DB_URL` 换掉即可（`db/session.py` 已按方言分别配连接池）。
2. **nginx upstream + `resolve`**。原来的写法只取 DNS 的第一个 IP，多副本时一半请求永远打不到。
   现在是 `upstream { server app:8000 resolve; zone ...; }`，按 Docker DNS 轮询全部副本。
   `nginx:1.27.5-alpine` 是**必须 pin** 的版本（`resolve` 参数 1.27.3 才开源）。
3. **迁移互斥**。2 个副本同时 `alembic upgrade head` 会打架。
   `entrypoint.sh` 用 `flock` 在共享目录 `deploy/locks` 上加锁，排队执行。
4. **Agent 无状态**。`ZST_AGENT_STATELESS=1`（默认值），每请求新建 Agent、历史从库读。
   否则同一会话落到不同副本，用户会发现「刚才说的话模型不记得了」。

### 10.5 为什么「高可用」仍然只有 80 分

- 单机 2 副本救得了「进程崩」，救不了「机器挂」；
- PostgreSQL 和 redis 都是单实例，本身是新的单点；
- 没有跨可用区、没有备份自动化、没有告警。

真要做到 99.9%，得上「多机 + 托管数据库 + 对象存储 + 监控告警」，
那是生产环境的预算（每月几百到几千），不是这个项目该做的事。
**能说清「哪一层做到了、哪一层是成本取舍」，比假装全做了更有说服力。**

---

## 附：成本

| 项 | 费用 |
|---|---|
| 腾讯云轻量 2C4G（学生机/新用户） | ¥10–30/月 |
| 腾讯云轻量 2C4G（正常价） | ¥60–90/月 |
| DashScope API（`qwen3.8-flash` + `text-embedding-v4`） | 按量，演示量级每月几元 |
| 域名（可选） | ¥30–60/年，国内需备案 |

不演示的时候 `docker compose stop`，CPU 和内存不计费（轻量是包月，停不停都收钱；
按量付费的 ECS 停机才省钱）。
