# 扫净灵 AI Agent

[![CI](https://github.com/Mooncake12-star/saojingling-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/Mooncake12-star/saojingling-agent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11-blue)](https://www.python.org/)

基于 **ReAct Agent + RAG + MCP + FastAPI + Redis + SQL** 的智能客服助手，专为扫地机器人/扫拖一体机器人场景打造。

## 项目演示

<!-- TODO(截图)：跑起项目后截图放到 docs/images/ 目录，然后取消下方注释
![Streamlit 对话界面](docs/images/streamlit-chat.png)
![Agent 工具调用链路](docs/images/agent-trace.png)
![PDF 报告导出](docs/images/pdf-report.png)
-->

## 技术栈

| 模块 | 技术                                                      |
|------|---------------------------------------------------------|
| 前端界面 | Streamlit                                               |
| API 服务 | FastAPI + Uvicorn                                       |
| AI Agent | LangChain ReAct Agent                                   |
| 工具协议 | MCP (Model Context Protocol)                            |
| 向量库 | ChromaDB                               |
| RAG 检索 | Ensemble Retrieval (向量+BM25) + Query Rewrite + BGE Reranker|
| 多模态 | Qwen-VL-Max 图片分析                                        |
| 大模型 | Qwen3.8-flash (DashScope)                               |
| 数据库 | SQLite                                                  |
| 缓存 | Redis                                                   |
| PDF 报告 | fpdf2                                                   |

## 快速开始

### 1. 克隆并配置 API Key

```bash
git clone https://github.com/Mooncake12-star/saojingling-agent.git
cd saojingling-agent
```

本项目支持**两种**配置方式，任选其一。

**方式 A：`.env` 文件（推荐，本地开发）**

```bash
cp .env.example .env
```

然后编辑 `.env`，填入你的 API Key：

```
DASHSCOPE_API_KEY=sk-xxxxx
DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

`.env` 已被 `.gitignore` 忽略，不会被提交；程序启动时由 `utils/env_utils.py` 自动加载。

**方式 B：系统环境变量（适合服务器 / CI）**

不创建 `.env`，直接把 Key 配到系统环境变量即可：

```bash
# Linux / macOS
export DASHSCOPE_API_KEY=sk-xxxxx
export DASHSCOPE_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# Windows PowerShell
$env:DASHSCOPE_API_KEY="sk-xxxxx"
$env:DASHSCOPE_BASE_URL="https://dashscope.aliyuncs.com/compatible-mode/v1"
```

> 两种方式同时存在时，**系统环境变量优先**（`.env` 只补齐缺失项，不覆盖已有值）。

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

> 如果 BGE Reranker 下载慢，可设置 HuggingFace 镜像：`export HF_ENDPOINT=https://hf-mirror.com`

### 3. 运行

**Streamlit 前端（推荐开发使用）：**

```bash
streamlit run app.py
```

**FastAPI 后端（推荐生产部署）：**

```bash
uvicorn fastapi_app.main:app --host 0.0.0.0 --port 8000
```

访问 `http://localhost:8000/docs` 查看 API 文档。

### 4. Docker 部署

```bash
docker compose up -d
```

## 项目结构

```
├── app.py                    # Streamlit 前端入口
├── agent/
│   ├── react_agent.py        # ReAct Agent + MCP 上下文管理
│   └── tools/
│       ├── mcp_server.py     # MCP 工具服务端 (7 工具)
│       └── middleware.py     # Agent 中间件 (日志/动态提示词)
├── fastapi_app/
│   ├── main.py               # FastAPI API 端点
│   ├── deps.py               # Agent 生命周期管理
│   └── schemas.py            # Pydantic 数据模型
├── rag/
│   ├── rag_service.py        # RAG 服务 (重排序+上下文压缩)
│   └── vector_store.py       # ChromaDB 向量存储
├── config/                   # YAML 配置文件
├── prompts/                  # 提示词模板
├── data/                     # 知识库原始数据
├── db/
│   └── database.py           # SQLite 会话存储
├── cache/
│   └── redis_client.py       # Redis 缓存客户端
├── model/
│   └── factory.py            # 模型工厂 (ChatTongyi / Embedding)
├── utils/                    # 工具模块
├── tests/                    # 单元测试
└── docker-compose.yml        # Docker 编排
```

## API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/health` | 健康检查 (含 MCP/RAG 状态) |
| POST | `/chat/stream` | SSE 流式对话 |
| POST | `/chat` | 同步对话 |
| POST | `/history/clear` | 清空对话历史 |
| POST | `/upload` | 上传图片 |
| GET | `/report/{filename}` | 下载 PDF 报告 |

## 可用工具

| 工具 | 说明 |
|------|------|
| `rag_summarize` | 本地知识库 RAG 检索 |
| `sougou_search` | 联网实时搜索 |
| `get_current_weather` | 天气查询 |
| `product_recommend` | 产品推荐 (户型/预算/地面/宠物) |
| `price_query` | 产品价格查询 |
| `image_analyze` | 多模态图片分析 |
| `export_report` | PDF 报告导出 |

## 测试

```bash
pytest tests/ -v
```

## 设计取舍

记录几个「为什么这么做」的决策，均有实测依据：

### 1. BM25 中文检索默认是失效的，用字符 bigram 修复

`BM25Retriever` 默认用 `text.split()` 预处理——按空格切词。中文句子没有空格，整句会被当成一个 token，BM25 完全失效。**实测这一默认行为会使混合检索的 Hit@1 比纯向量检索还低 12.5pp**（详见 [评测](#效果评测)）。

修复方案：字符 bigram（相邻两字成词）作为 `preprocess_func`，零额外依赖且对中文有效；若已安装 jieba 可换成 `jieba.lcut`，效果相当。

### 2. 重排序走三级降级，而不是单点依赖

```
BGE Reranker（CrossEncoder） → 加载失败则 LLM 重排序 → 再失败保持原序
```

- BGE 比 LLM 重排更稳、更快、零 token 成本；但依赖 sentence-transformers 与模型下载
- 每一级都有 try/except 兜底，**任何一级失败都不阻断主流程**
- 重排后接上下文压缩：相关度 < 0.3 的文档直接过滤，单文档截断 800 字符，降低生成阶段 token 消耗

### 3. 模型客户端用惰性单例，import 无副作用

早期版本在 `model/factory.py` 模块顶层直接实例化 `chat_model`——任何模块只要 `from model.factory import ...` 就会立刻创建 HTTP 客户端并校验 API Key。后果：

- 单元测试只想 import 类结构，却被强制初始化真实客户端
- 环境里没有 API Key 时，连「纯导入」都会失败，CI 无法验证代码结构

现在改为 `get_chat_model()` / `get_embedding_model()`：首次调用才创建，之后复用同一实例。

### 4. MCP 子进程显式注入环境，脚本转绝对路径

stdio 方式拉起 MCP Server 时，子进程默认继承父进程 CWD，相对路径会相对错误的目录解析。处理方式：

- 启动前把 `args` 里的脚本名强制转为绝对路径，并校验文件存在
- 显式注入 `PYTHONPATH`（项目根 + 工具目录）与 `PYTHONUTF8=1`，保证子进程模块解析和编码与父进程无关

### 5. MCP 工具 schema 动态翻译成 LangChain Tool

MCP 返回的是 JSON Schema 字典，LangChain 需要带 Pydantic 模型的 Tool 对象，两者互不相识。用 `pydantic.create_model` 按 `inputSchema.properties` 动态构建 `args_schema`（含 required→必填、description→Field 描述的映射），每个 MCP 工具在运行时生成对应的 LangChain Tool，**新增工具无需改客户端代码**。

### 6. 配置优先级：系统环境变量 > .env 文件

`load_dotenv(override=False)`：系统环境变量优先，`.env` 只补齐缺失项。本地开发用 `.env`，服务器/CI 用系统变量，两种方式可并存且行为可预期。所有环境变量统一 `strip()` 后使用，防止粘贴时混入的不可见字符穿透到 URL 解析（这个问题在 CI 上真实踩过）。
