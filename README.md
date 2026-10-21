# 扫净灵 AI Agent

[![CI](https://github.com/Mooncake12-star/saojingling-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Mooncake12-star/saojingling-agent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/)
[![Tests](https://img.shields.io/badge/tests-173%20passed-brightgreen)](tests/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

面向扫地机器人 / 扫拖一体机器人的智能客服 Agent：**ReAct Agent + RAG + MCP + FastAPI + Redis + SQL**。

不只是「调通一个模型」：它带 JWT 多租户鉴权、知识库异步入库、四层检索隔离、request_id 全链路日志、
过载背压与多副本部署方案，并且有 173 个测试兜住这些行为。

- 业务代码约 7.7k 行（Python），测试代码约 3.7k 行
- 25 条路由（24 个接口 + 根路径重定向）、6 张表、4 个 Alembic 迁移
- 7 个 MCP 工具，向量库 328 个 chunk（内置知识库）

## 项目演示

<!-- TODO(截图)：跑起项目后截图放到 docs/images/ 目录，然后取消下方注释
![Streamlit 对话界面](docs/images/streamlit-chat.png)
![Agent 工具调用链路](docs/images/agent-trace.png)
![PDF 报告导出](docs/images/pdf-report.png)
-->

## 项目亮点

| 能力 | 说明 |
|---|---|
| 多租户隔离 | 四层防线：SQL 强制 `user_id`、仓储层签名、接口身份只来自 token、向量检索 metadata 过滤 |
| 异步入库 | 上传接口 202 立即返回，后台走 `pending → parsing → ready/failed`，失败原因写回文档记录 |
| 可观测 | 中间件生成 `request_id`，跨 nginx / FastAPI / 日志串联；支持 `text` 与单行 JSON 两种格式 |
| 缓存可降级 | Redis 只做加速不做依赖：5s 熔断 + 30s 冷却重试，Redis 挂掉服务照常工作 |
| 并发有背压 | 对话信号量 + 排队超时，过载时 1 秒内返回 `503 + Retry-After`，而不是挂 80 秒后 OOM |
| 可水平扩展 | `docker-compose.ha.yml` 叠加层：PostgreSQL + 2 副本 + 迁移互斥 + 无状态 Agent |

## 架构

```
浏览器 / Streamlit
        │  HTTP + JWT
        ▼
   nginx（容器部署，注入 / 透传 X-Request-ID）
        │
        ▼
FastAPI ── 纯 ASGI 中间件：request_id → ContextVar → 日志 / 响应头
   ├── api/           鉴权、对话、知识库、文件、产品、运维
   ├── services/      业务编排（对话、入库、缓存、鉴权、安全）
   ├── repositories/  SQL 访问层，每个查询都必须带 user_id
   ├── AgentManager → ReactAgent → MCP stdio 子进程（7 个工具）
   └── RagScope(ContextVar) → Ensemble(向量 + BM25) → BGE 重排

存储：
   SQLite / PostgreSQL（6 张表，Alembic 管理）
   ChromaDB（328 chunk，带 user_id / knowledge_base_id / is_public 元数据）
   Redis（可选：知识库列表缓存，写后失效）
```

请求进入后先鉴权拿 `user_id`，再把它写进 `ContextVar`；仓储层和向量检索都从这个上下文取范围，
不从请求参数取——**检索范围由服务端决定，模型和前端都改不了**。详见[安全与多租户隔离](#安全与多租户隔离)。

## 技术栈

| 模块 | 技术 |
|---|---|
| 前端界面 | Streamlit（本地调试用，详见[已知边界](#已知边界)） |
| API 服务 | FastAPI + Uvicorn |
| AI Agent | LangChain ReAct Agent |
| 工具协议 | MCP (Model Context Protocol)，stdio 方式 |
| 向量库 | ChromaDB |
| RAG 检索 | Ensemble Retrieval（向量 + BM25）+ Query Rewrite + BGE Reranker |
| 多模态 | Qwen-VL-Max 图片分析 |
| 大模型 | Qwen3.8-flash（DashScope 兼容模式） |
| 数据库 | SQLAlchemy 2.0 + Alembic，SQLite（默认）/ PostgreSQL（多副本） |
| 缓存 | Redis（可选，故障自动降级） |
| PDF 报告 | fpdf2 |
| 部署 | Docker Compose（nginx + app + redis），可叠加 PG 多副本 |

## 快速开始

### 1. 克隆并配置密钥

```bash
git clone https://github.com/Mooncake12-star/saojingling-agent.git
cd saojingling-agent
cp .env.example .env
```

编辑 `.env`，至少填这两项：

```
DASHSCOPE_API_KEY=sk-xxxxx
ZST_JWT_SECRET=<至少 32 字节的随机串>
```

生成密钥（Git Bash / Linux）：

```bash
openssl rand -hex 32
```

> `ZST_JWT_SECRET` 不设时服务仍能启动，但会打 `[Auth] 未设置 ZST_JWT_SECRET` 警告并使用开发默认值——
> 那等于任何人都能自己签一个 token 冒充任意用户。**部署到公网必须设**。

`.env` 已被 `.gitignore` 忽略，由 `utils/env_utils.py` 在启动时加载。
也可以用系统环境变量（服务器 / CI 常用），两者同时存在时**系统环境变量优先**，`.env` 只补齐缺失项。

### 2. 初始化数据库与知识库

```bash
pip install -r requirements.txt

# 建表 / 升级到最新版本（表结构由 Alembic 管理，服务不会自动建表）
alembic upgrade head

# 导入产品种子数据
python db/seed_products.py

# 首次把 data/ 下的文档灌进 ChromaDB（约 20~30 秒）
python -c "from rag.vector_store import VectorStoreService; VectorStoreService().load_document()"
```

> BGE Reranker 首次加载要下载 1.1GB 模型。国内网络建议先设 `export HF_ENDPOINT=https://hf-mirror.com`。

### 3. 运行

**后端（推荐）：**

```bash
uvicorn fastapi_app.main:app --host 0.0.0.0 --port 8000
```

- 交互式文档：`http://localhost:8000/docs`
- 就绪探针：`http://localhost:8000/readyz`（冷启动 30~35 秒后 `ready: true`）
- 重排模型在后台加载，`reranker.state` 从 `loading` 变 `ready` 才算真正预热完

**Streamlit 前端（本地看效果）：**

```bash
streamlit run app.py
```

### 4. 跑起来后怎么验证

```bash
# 注册（没开邀请码时不需要 invite_code）
curl -X POST http://localhost:8000/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"me@test.local","password":"Passw0rd!123"}'

# 登录拿 token
curl -X POST http://localhost:8000/auth/login \
  -d "username=me@test.local&password=Passw0rd!123"

# 带 token 对话
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <access_token>" \
  -d '{"query":"大户型适合哪些扫地机器人？只说三点","session_id":"demo"}'
```

### 5. Docker 部署

```bash
docker compose up -d --build        # nginx:8080 → app:8000 → redis
curl -s localhost:8080/readyz
```

完整步骤（买机器、传数据、写 `.env`、验收、日常运维、成本）见 **[deploy/DEPLOY.md](deploy/DEPLOY.md)**。

多副本（真高可用，需 4C8G 以上）：

```bash
docker compose -f docker-compose.yml -f docker-compose.ha.yml up -d --build
```

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | 无（必填） | 百炼平台 API Key |
| `DASHSCOPE_BASE_URL` | 阿里云兼容模式地址 | 可指向自建网关 |
| `ZST_JWT_SECRET` | 开发默认值 | JWT 签名密钥，生产必填；换掉即所有 token 失效 |
| `ZST_INVITE_CODE` | 空（注册不设防） | 设置后注册必须带 `invite_code`，否则 403。启动日志会打 `invite_code: on/off` |
| `ZST_DB_URL` | `sqlite:///data/app.db` | 换成 `postgresql+psycopg://...` 即切 PostgreSQL，代码无需改动 |
| `ZST_REDIS_URL` | 空（缓存关闭） | 也支持 `REDIS_HOST` / `REDIS_PORT` |
| `ZST_LOG_FORMAT` | `text` | `json` = 一行一个 JSON，给 ELK / Loki 收 |
| `ZST_LOG_LEVEL` | `DEBUG` | 日志级别 |
| `ZST_LOG_TO_FILE` | `1` | `0` = 只打 stdout（容器里推荐） |
| `ZST_CHAT_CONCURRENCY` | `16` | 同时进行的对话数上限 |
| `ZST_CHAT_QUEUE_TIMEOUT` | `3` | 排队等位秒数，超时返回 503 |
| `ZST_CHAT_RETRY_AFTER` | `5` | 503 响应里的 `Retry-After` 秒数 |
| `ZST_CHAT_GATE_ENABLED` | `1` | 关闭则不限流（压测对照用） |
| `ZST_AGENT_STATELESS` | `1` | 每请求新建 Agent、历史从库读；多副本必须为 1 |
| `ZST_RERANK_CONCURRENCY` | `2` | 重排推理并发上限 |
| `ZST_RERANK_GATE_TIMEOUT` | `2` | 抢不到重排位子就跳过精排，保留 RRF 顺序 |
| `ZST_RERANK_READY_TIMEOUT` | `0.05` | 等待模型加载的秒数，超时直接降级 |
| `ZST_RAG_MAX_SCOPES` | `8` | 进程内缓存的检索范围数（LRU 淘汰） |
| `ZST_RAG_BUILD_CONCURRENCY` | `2` | 同时构造检索服务的上限 |
| `ZST_RAG_BUILD_WAIT_TIMEOUT` | `30` | 等别人构造同一范围的秒数，超时返回“稍后重试” |

Docker Compose 专用（只在 `.env` 里生效，进程读不到）：`ZST_HTTP_PORT`（nginx 对外端口，默认 8080）、
`TZ`（默认 `Asia/Shanghai`）、`PRELOAD_MODEL`（构建期是否把重排模型打进镜像）。
多副本叠加层另有 `ZST_PG_PASSWORD`、`ZST_APP_REPLICAS`。

## 项目结构

```
├── app.py                      # Streamlit 前端入口（直连函数，不走 HTTP）
├── agent/
│   ├── react_agent.py          # ReAct Agent + MCP 上下文管理 + 会话历史
│   └── tools/
│       ├── mcp_server.py       # MCP 工具服务端（7 个工具）
│       └── middleware.py       # Agent 中间件（日志 / 动态提示词）
├── fastapi_app/
│   ├── main.py                 # 应用装配、中间件、/healthz /readyz
│   ├── middleware.py           # 纯 ASGI 中间件：request_id、访问日志
│   ├── concurrency.py          # 对话信号量（背压 + 503）
│   ├── deps.py                 # AgentManager：会话、启动预热、就绪状态
│   ├── api/                    # 路由层：auth / chat / knowledge_bases / files / products / health
│   └── services/               # 业务层：auth / security / chat / knowledge / ingest / files / cache
├── repositories/               # 数据访问层，每个方法都要求 user_id
├── models/                     # ORM：users / conversations / conversation_summary / products / knowledge_bases / documents
├── schemas/                    # Pydantic 模型（对外契约，不暴露内部字段）
├── migrations/                 # Alembic 迁移（4 个版本）
├── rag/
│   ├── rag_service.py          # RAG 主流程（改写 → 混合召回 → 重排 → 压缩）
│   ├── reranker.py             # BGE 重排：单例 + 加载锁 + 限并发 + 降级
│   ├── local_tool.py           # 按检索范围缓存 RAG 服务（per-key 锁 + LRU）
│   ├── scope.py                # RagScope + ContextVar：检索范围传递
│   ├── vector_store.py         # ChromaDB 封装（支持 where 过滤）
│   ├── index_service.py        # 文档入库 / 删除向量
│   └── migrate_metadata.py     # 历史数据补 metadata（幂等）
├── cache/redis_client.py       # Redis 客户端（熔断 + 冷却重试）
├── db/                         # 引擎 / PRAGMA / 会话、健康检查、种子数据、旧数据迁移
├── deploy/                     # entrypoint.sh、nginx.conf、server-setup.sh、DEPLOY.md
├── config/                     # YAML 配置
├── prompts/                    # 提示词模板
├── data/                       # 知识库原始数据
├── docker-compose.yml          # 基础编排：nginx + app + redis
├── docker-compose.ha.yml       # 叠加层：PostgreSQL + 多副本
└── tests/                      # 173 个用例
```

## API 端点

**鉴权**（`/auth`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/register` | 注册，201；邀请码开启时不带 `invite_code` 返回 403 |
| POST | `/auth/login` | 表单登录，返回 access + refresh |
| POST | `/auth/refresh` | 用 refresh token 换新 token |
| GET | `/auth/me` | 当前用户 |
| POST | `/auth/logout` | 全设备登出（`token_version` 自增，旧 token 立即失效） |

**对话**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat/stream` | SSE 流式对话 |
| POST | `/chat` | 一次性返回完整回答 |
| GET | `/history/sessions` | 当前用户的会话列表 |
| POST | `/history/clear` | 清空某个会话历史 |

请求体四个字段：`query`、`image_path`、`session_id`、`knowledge_base_id`。
`knowledge_base_id` 不传则检索「自己的文档 + 内置公共资料」；传别人的库返回 404（不是 403，避免探测库是否存在）。

**知识库**

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/knowledge-bases` | 我的知识库列表 |
| POST | `/knowledge-bases` | 新建，同一用户下名称唯一 |
| GET | `/knowledge-bases/{kb_id}` | 详情 |
| DELETE | `/knowledge-bases/{kb_id}` | 删除，级联清理文档与向量 |
| POST | `/knowledge-bases/{kb_id}/documents` | 上传文档，202 异步入库（.txt / .pdf，≤20MB） |
| GET | `/knowledge-bases/{kb_id}/documents` | 文档列表 |
| GET | `/documents/{doc_id}` | 查询入库状态（`pending/parsing/ready/failed`） |
| DELETE | `/documents/{doc_id}` | 删除文档与向量 |

同一知识库内按文件 MD5 去重：重复上传返回已有记录，不会二次切分和写入。

**文件 / 产品 / 运维**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/upload` | 上传图片，返回绝对路径（供 `image_path` 使用） |
| GET | `/report/{filename}` | 下载 PDF 报告 |
| GET | `/products` | 产品列表（公开，无需登录） |
| GET | `/products/{product_id}` | 产品详情（公开） |
| GET | `/health` | 健康检查（含 MCP / RAG / 缓存状态） |
| GET | `/healthz` | 存活探针：不查库、不碰 MCP，只证明进程还在 |
| GET | `/readyz` | 就绪探针：db + mcp + 重排状态 + 对话闸门；未就绪返回 503 |

存活探针与就绪探针的分工必须分清，混用会出两类事故：拿 `/healthz` 做就绪，数据库断了流量照样打进来；
拿 `/readyz` 做存活，数据库抖一下所有副本一起被判死重启。`/health` 返回结构化状态（MCP / RAG / 缓存），
是给人和监控看的，不适合直接当探针——它不返回 503。重排模型**不计入**就绪判定：
它不可用时检索降级为 RRF 顺序，功能仍可用。

## 可用工具

| 工具 | 说明 |
|---|---|
| `rag_summarize` | 本地知识库 RAG 检索（受检索范围约束） |
| `sougou_search` | 联网实时搜索 |
| `get_current_weather` | 天气查询 |
| `product_recommend` | 产品推荐（户型 / 预算 / 地面 / 宠物） |
| `price_query` | 产品价格查询 |
| `image_analyze` | 多模态图片分析 |
| `export_report` | PDF 报告导出 |

MCP 返回 JSON Schema，LangChain 需要 Pydantic 模型的 Tool 对象。客户端用 `pydantic.create_model`
按 `inputSchema` 动态构建 `args_schema`，**新增工具不需要改客户端代码**。

## 安全与多租户隔离

越权是这类项目最容易漏的地方：SQL 一条没写错，照样能把别人的数据查出来。四层防线：

1. **SQL 层**：仓储层每个查询方法都要求传 `user_id`，没有「不传就查全部」的重载。
2. **仓储层**：`get(user_id, id)` 双条件；查不到就抛 NotFound，不区分「不存在」和「不是你的」。
3. **接口层**：身份只来自 token（`CurrentUser` 依赖），不读请求体里的 `user_id`。
4. **向量层**：检索必带 metadata 过滤（`$and` 精确匹配库、`$or` 带出公共资料），
   向量和 BM25 两路召回都要过滤——只过滤一路等于没过滤。

几个具体决策：

- **检索范围由服务端设定**。`RagScope` 放进 `ContextVar`，由接口层在调用 Agent 前设置；
  不把 `kb_id` 作为工具参数交给模型填——模型会被提示词诱导去查别的库。
- **越权返回 404 而非 403**：403 等于告诉对方「这个 ID 存在，只是不给你看」。
- **邀请码在查邮箱之前校验**：先查邮箱会把「这个邮箱是否注册过」变成可枚举的信息。
- **`hmac.compare_digest` 比较邀请码**，且转成 bytes——直接比中文 str 会抛 `TypeError`。
- **`token_version` 全设备登出**：改密码或登出时自增，旧 token 即使没过期也立刻失效。

## 并发与容量

100 并发压测暴露了四个串行点，全部改掉了（详细数据见 [DEPLOY.md 第 10 节](deploy/DEPLOY.md)）：

| 位置 | 改前 | 改后 |
|---|---|---|
| RAG 服务构造 | 一把全局锁包住整个构造（含 40~60s 加载模型），所有范围排队 | per-key `Event` 去重 + 不同范围并行 + 构造闸门（默认 2）+ 超时抛 `RagBuildBusy` |
| 重排模型加载 | 无互斥 double-check，并发首次各加载一份模型（1.1GB 权重） | `rag/reranker.py`：加载锁 + 状态机，全进程只加载一次 |
| 重排推理 | 无限并发，抢 GIL，总吞吐反而下降 | `BoundedSemaphore` 限 2；排队超 2 秒跳过精排，保留 RRF 顺序 |
| 背压 | 请求全收，慢请求吃光线程和连接 | 对话信号量（默认 16），排队 3 秒拿不到位子返回 503 |

重排的三档降级链路：

```
模型 ready   → BGE 精排（100~300ms）
模型加载中   → 直接用 RRF 顺序（不调 LLM：那 40 秒里 LLM 配额会被重排请求打满）
依赖缺失/失败 → LLM 重排（3~5s，功能不变，只是慢）
```

**实测（2C4G 单副本，单轮对话 40~80 秒）：**

| 场景 | 改前 | 改后 |
|---|---|---|
| 3 个用户并发对话 | 58.5s / 66.8s / 71.6s，严格接力 | 各自 40~50s，同一秒开始 |
| 100 并发对话 | 300 个请求 280 个 500，随后 OOM | 16 个在跑，84 个 3 秒内收到 503 |
| 过载响应 | 挂 80 秒后 504 | 1.14s 返回 503 + `Retry-After: 5` |
| 吞吐 | ≈0.15 QPS（被锁串行化） | ≈**0.23 QPS**，且不雪崩 |

**0.23 QPS 不是「解决了高并发」，是「过载时不再死」。** 要更高吞吐只有两条路：加副本，或换更快的重排 / 生成实现。
多副本必须 4C8G 以上——每副本约 1.8GB（基础 0.7G + 重排模型 1.1G），2C4G 开两个必然 OOM。

## 测试

```bash
pytest -q                          # 173 passed
pytest tests/test_isolation.py -v  # 隔离专项
```

| 文件 | 覆盖内容 |
|---|---|
| `test_auth.py` | 注册 / 登录 / 刷新 / 登出、邀请码、密码校验、token_version |
| `test_isolation.py` | 用户间数据隔离、Agent 缓存 key 含 user_id、跨用户越权 404 |
| `test_rag_scope.py` | `RagScope` 生效范围、向量与 BM25 双路过滤、无 scope 行为 |
| `test_knowledge_api.py` | 知识库 CRUD、异步入库状态机、重复上传去重、级联删除 |
| `test_concurrency.py` | 模型只加载一次、加载中降级、不同范围并行、闸门 503 |
| `test_observability.py` | request_id 注入 / 透传、JSON 日志字段、异常也带 rid |
| `test_api.py` / `test_api_layers.py` | 接口契约与分层边界 |
| `test_conversation_*.py` / `test_product_repository.py` / `test_orm_smoke.py` | ORM 与仓储层 |

## 效果评测

检索质量有专门的评测脚本，四档对照，统计 Hit@1 / Hit@3 / Hit@5：

```bash
python tests/eval_quick.py 40 all        # A 纯向量 / B 向量+BM25(RRF) / C 混合+BGE 重排
python tests/eval_quick.py 40 hard       # 口语化难例
python tests/eval_quick.py 20 all --e2e  # 追加 D 档：真调 LLM 看答案命中
```

脚本会先强制 `reranker.warmup()`，加载失败直接中止——否则 C 档会静默降级成 LLM 重排，数字不可比。
这个「先卡住再测」的检查是踩过坑之后加的。

## 设计取舍

记录几个「为什么这么做」的决策，均有实测依据。

### 1. BM25 中文检索默认是失效的，用字符 bigram 修复

`BM25Retriever` 默认用 `text.split()` 预处理——按空格切词。中文句子没有空格，整句会被当成一个 token，BM25 完全失效。
**实测这一默认行为会使混合检索的 Hit@1 比纯向量检索还低 12.5pp。**

修复方案：字符 bigram（相邻两字成词）作为 `preprocess_func`，零额外依赖且对中文有效；若已安装 jieba 可换成 `jieba.lcut`，效果相当。

### 2. 重排序走三级降级，而不是单点依赖

```
BGE Reranker（CrossEncoder） → 加载失败则 LLM 重排序 → 再失败保持原序
```

- BGE 比 LLM 重排更稳、更快、零 token 成本；但依赖 sentence-transformers 与模型下载
- 每一级都有 try/except 兜底，**任何一级失败都不阻断主流程**
- 加载模型的那 40 秒不阻塞请求：直接用 RRF 顺序，而不是让所有请求排队等模型
- 重排后接上下文压缩：相关度 < 0.3 的文档直接过滤，单文档截断 800 字符，降低生成阶段 token 消耗

### 3. 模型客户端用惰性单例，import 无副作用

早期版本在 `model/factory.py` 模块顶层直接实例化 `chat_model`——任何模块只要 `from model.factory import ...`
就会立刻创建 HTTP 客户端并校验 API Key。后果：

- 单元测试只想 import 类结构，却被强制初始化真实客户端
- 环境里没有 API Key 时，连「纯导入」都会失败，CI 无法验证代码结构

现在改为 `get_chat_model()` / `get_embedding_model()`：首次调用才创建，之后复用同一实例。

### 4. MCP 子进程显式注入环境，脚本转绝对路径

stdio 方式拉起 MCP Server 时，子进程默认继承父进程 CWD，相对路径会相对错误的目录解析。处理方式：

- 启动前把 `args` 里的脚本名强制转为绝对路径，并校验文件存在
- 显式注入 `PYTHONPATH`（项目根 + 工具目录）与 `PYTHONUTF8=1`，保证子进程模块解析和编码与父进程无关

### 5. MCP 工具 schema 动态翻译成 LangChain Tool

MCP 返回的是 JSON Schema 字典，LangChain 需要带 Pydantic 模型的 Tool 对象，两者互不相识。
用 `pydantic.create_model` 按 `inputSchema.properties` 动态构建 `args_schema`
（含 required→必填、description→Field 描述的映射），每个 MCP 工具在运行时生成对应的 LangChain Tool，**新增工具无需改客户端代码**。

### 6. 配置优先级：系统环境变量 > .env 文件

`load_dotenv(override=False)`：系统环境变量优先，`.env` 只补齐缺失项。本地开发用 `.env`，服务器 / CI 用系统变量，
两种方式可并存且行为可预期。所有环境变量统一 `strip()` 后使用，防止粘贴时混入的不可见字符穿透到 URL 解析
（这个问题在 CI 上真实踩过）。

### 7. request_id 用「可变 ContextVar」而不是重新 set

`ContextVar.set()` 在 `run_in_threadpool` 里设的值不会回传到外层上下文——同步依赖里设的 `request_id`，
到了异步中间件里读出来是 `None`。改成「ContextVar 里存一个可变对象，只改它的属性」，跨线程池才拿得到。
中间件本身用纯 ASGI 实现，不用 `BaseHTTPMiddleware`——后者会包一层任务组，SSE 流式响应会被缓冲住。

### 8. 内容入库必须异步，且状态落库

解析 + 切分 + 向量化一个 20MB 的 PDF 要几十秒，同步做就是让用户干等一个可能超时的请求。
上传接口只做「存文件 + 建记录 + 提交」，返回 202，真正的入库交给 `BackgroundTasks`。
所以后台任务必须自己开 `session_scope`——请求的 Session 在响应返回时就关了，复用它必然报错。
失败不吞：`error_message` 写回文档记录，前端能查到为什么失败。

### 9. 缓存失败要能自己恢复，不能只判断一次

早期实现只在启动时连一次 Redis，连不上就把客户端永久置空；`backend()` 还会撒谎说「用着 redis」。
现在的做法：懒获取 + 5 秒熔断 + 30 秒冷却重试 + `backend()` 真探测（`off / redis / redis(circuit-open) / redis(unavailable)`）。
脏数据（不是合法 JSON）当 miss 处理，不让一条坏缓存把接口打挂。

## 已知边界

诚实列清楚，比假装全做了更有用：

1. **Streamlit 前端不走后端**。`app.py` 直接 `import ReactAgent` 调函数，不过 HTTP、不登录、不带检索范围。
   后端的鉴权、隔离、限流它一个都没用上——它是本地调试界面，不是生产前端。要对外开放请接 REST API。
2. **单机部署**。单副本时 SQLite 是单写锁，重启即不可用。多副本方案（PG + 2 副本）解决了「进程崩」，
   解决不了「机器挂」；PG 和 Redis 都是单实例，本身是新的单点。
3. **没有 HTTPS**。公网演示只有 IP + http，JWT 在链路上是明文。要上 HTTPS 需要域名（国内还要备案）加证书。
4. **没有接口级限流**。邀请码挡住了陌生人注册，挡不住「拿着邀请码的人」高频刷接口。
   成本兜底靠百炼控制台给 API Key 设额度上限。
5. **容量上限受单轮时长约束**。2C4G 单副本约 16 个并发对话、0.23 QPS。这不是代码问题，是 CPU 推理和模型生成的物理成本。
6. **向量库与代码同机**。ChromaDB 是嵌入式库，容器与本机版本不一致会写坏索引（本项目真实踩过：
   `Error loading hnsw index`）。跨环境部署时要么固定版本，要么重建索引。

## 文档

| 文档 | 内容 |
|---|---|
| [deploy/DEPLOY.md](deploy/DEPLOY.md) | 公网部署全流程、验收清单、日常运维、并发改造细节、成本 |
| [项目技术总结.md](项目技术总结.md) | 技术选型与实现要点汇总 |
| `问题记录/` | 开发过程中踩过的坑与定位过程 |

## License

[MIT](LICENSE)
