# Dify 复刻版（方案 B：只借检索，不借生成）

同一个「扫净灵」问答能力，用 Dify 的可视化编排重新搭一遍。

**复刻的目的不是替代手写版，而是补上一个能力证明**：

> 这条链路我既能从零手写（重排怎么调、降级怎么做、并发怎么扛），
> 也能在一个可视化平台上用一天拼出来——我知道平台省掉的是模板代码和部署，
> 省不掉的是检索质量。

为了做到这一点，Dify 这一版**不自己接向量库**，而是用 HTTP 节点调用手写版暴露的
`POST /tools/retrieve`：检索、RRF 融合、BGE 重排仍然跑在原来的代码里，
Dify 只负责流程编排和最终生成。

```
Dify Chatflow
  开始(query)
    → HTTP 请求  ──→  POST /tools/retrieve  （手写版：改写 + 混合检索 + BGE 重排）
    → 代码节点       把 hits 拼成上下文
    → LLM 节点       基于上下文回答
    → 直接回复
```

链路两端**不要求同一台机器**：Dify 跑在本地 Docker 或云端 SaaS 都行，
只要 HTTP 节点能访问到 `/tools/retrieve`。本机内存吃紧时走云端 + 隧道（见第二节路线 B）。

---

## 一、先把手写版的服务跑起来

```bash
# 1. 开启外部调用密钥（不设这个变量，接口只接受 JWT）
export ZST_TOOL_API_KEY=sjling-demo-key      # Windows: set / $env:

# 2. 启动（8000 端口，与 README 主文档一致）
uvicorn fastapi_app.main:app --host 0.0.0.0 --port 8000
```

**验证接口通不通**（先别开 Dify，这一步单独测）：

```bash
curl -X POST http://127.0.0.1:8000/tools/retrieve \
  -H "Content-Type: application/json" \
  -H "X-Tool-Key: sjling-demo-key" \
  -d '{"query":"大户型适合什么扫地机器人","top_k":3}'
```

期望返回：

```json
{
  "query": "大户型适合什么扫地机器人",
  "rewritten_query": "大户型 扫地机器人 推荐 型号",
  "hits": [
    {"rank": 1, "content": "...", "source": "data/product.md", "knowledge_base_id": null, "score": null},
    {"rank": 2, "content": "...", "source": "data/faq.md", "knowledge_base_id": null, "score": null}
  ]
}
```

`rewritten_query` 是 LLM 改写后的检索词——它本身就是个演示点：
同一个问题，向量检索用原句和用改写句的命中率差 30pp。

---

## 二、起 Dify

有两条路，按机器内存选。**Dify 那套容器稳定跑要 4~6 GB**，如果本机可用内存低于 4 GB，直接走路线 B。

### 路线 A：本地 Docker（功能完整，吃内存）

```bash
git clone https://github.com/langgenius/dify.git D:/dify
cd D:/dify/docker
cp .env.example .env
```

改 `.env` 里的端口，避开本项目占用的 8000，以及本机可能已有的 80 / 5432 / 6379：

```ini
EXPOSE_NGINX_PORT=3000
EXPOSE_NGINX_SSL_PORT=3443
```

```bash
docker compose up -d
# 浏览器打开 http://localhost:3000，首次进入要设置管理员账号
```

> 不要把这套容器合并进项目的 `docker-compose.yml`。一旦合并，
> 网络、卷、启动顺序全搅在一起，排查问题会变成两个人的责任交界地。

### 路线 B：云端 Dify + 内网穿透（省内存，推荐低配机）

> **状态：已实测打通（2026-10-28）。** 配置：cloud.dify.ai Chatflow（5 节点）+
> cloudflared 免注册隧道 + 通义 qwen3.7-max。全流程运行：
> HTTP 19.96s（检索+重排）/ 代码 83ms / LLM 2.2s，5 节点全部成功。
> 注意：trycloudflare 域名每次重启隧道都会变，HTTP 节点 URL 需同步更新。

链路：

```
浏览器 → cloud.dify.ai（云端 Dify）
              ↓ HTTP 节点
        https://xxx.trycloudflare.com  ← cloudflared 隧道
              ↓
        本机 127.0.0.1:8000/tools/retrieve  ← 你的 FastAPI + BGE 重排
```

本机只需要跑 FastAPI（约 1.5 GB），不用起 9 个容器。

**1. 下载 cloudflared（免注册，比 ngrok 省一步）**

```bash
curl -L -o D:/tools/cloudflared.exe \
  https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe
```

**2. 建隧道**（必须先让 FastAPI 跑起来）

```bash
D:/tools/cloudflared.exe tunnel --url http://127.0.0.1:8000 \
  --protocol http2 --edge-ip-version 4 --no-autoupdate
```

日志里出现这一行才算成功，地址就在上面几行：

```
INF Registered tunnel connection connIndex=0 ... protocol=http2
https://xxx-yyy-zzz.trycloudflare.com
```

**3. 本地先验一遍再填进 Dify**

```bash
curl -X POST https://xxx-yyy-zzz.trycloudflare.com/tools/retrieve \
  -H "Content-Type: application/json" \
  -H "X-Tool-Key: sjling-demo-key" \
  -d '{"query":"大户型适合什么扫地机器人","top_k":2}'
```

**4. Dify 里 HTTP 节点填隧道地址**，其余配置与路线 A 完全一致。

#### 路线 B 的四个坑（实测踩过）

| 坑 | 现象 | 解法 |
| --- | --- | --- |
| `--protocol auto` 建隧道失败 | `failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": EOF` | 加 `--protocol http2 --edge-ip-version 4` |
| 进程被父终端回收 | 日志有 `Registered tunnel connection`，但公网访问返回 **530**（错误码 1033），进程已消失 | 用独立终端/受管后台任务启动，别用一次性子 shell |
| 域名每次重启都变 | 重新建隧道后旧地址立刻 530 | 每次换地址后同步改 Dify HTTP 节点里的 URL |
| 免费隧道无 SLA | 长时间空闲可能断连 | 演示前重新探一次 `/healthz`，200 才继续 |

> 无账号隧道官方明确说"不保证可用性"，仅适合本地演示与面试投屏，
> 不要写进任何对外服务的生产配置。


---

## 三、在 Dify 里搭 Chatflow

新建应用 → **Chatflow**（不是 Workflow，Chatflow 才有对话变量和直接回复）。

### 节点 1：开始

加一个输入变量 `top_k`（数字，默认 3），或者干脆不要，用默认值。

### 节点 2：HTTP 请求

| 字段 | 值 |
| --- | --- |
| 方法 | `POST` |
| URL | `http://host.docker.internal:8000/tools/retrieve`（本地 Dify）<br>或隧道地址（云端 Dify，见第二节 B 路线） |
| 超时 | `60` 秒（首次检索要加载 BGE 模型，40~60s，别用默认 10s） |
| 重试 | 开启，最多 2 次（服务端返回 503 表示知识库正在初始化） |

Headers：

```
Content-Type: application/json
X-Tool-Key: sjling-demo-key
```

Body（JSON）：

```json
{
  "query": "{{#start.query#}}",
  "top_k": 3
}
```

> URL 必须是 `host.docker.internal` 而不是 `127.0.0.1`：
> Dify 跑在容器里，容器内的 127.0.0.1 是它自己，不是你的 Windows 主机。
> Linux 上用 `--add-host=host.docker.internal:host-gateway` 或在 compose 里补 host 映射。

### 节点 3：代码节点（把 hits 拼成上下文）

输入变量：`hits` ← `{{#http_request.body#}}` 里的 `hits`（数组）。

```python
def main(hits: list) -> dict:
    if not hits:
        return {"context": "（知识库未检索到相关内容）"}
    lines = []
    for h in hits:
        lines.append(f"[{h['rank']}] {h['content']}\n来源：{h['source']}")
    return {"context": "\n\n".join(lines)}
```

> 这一步手写版里是 `_build_context()`，Dify 里换成一个 10 行的代码节点。
> 面试可以指出来：**这 10 行就是「平台帮你省掉的部分」和「平台不帮你省的部分」的分界线**。

### 节点 4：LLM

模型自选（Qwen / DeepSeek / GPT 均可），上下文变量填 `{{#code.context#}}`，提示词：

```
你是扫地机器人选购助手。严格依据下面的参考资料回答，资料里没有的内容不要编造，
无法回答时直接说「知识库里没有相关信息」。

【参考资料】
{{#code.context#}}

【用户问题】
{{#start.query#}}

要求：先给结论（推荐哪款 / 哪类），再用 1-2 条理由说明，最后标出引用来源编号。
```

温度 0.3，最大回复长度 800。

### 节点 5：直接回复

输出 `{{#llm.text#}}`。

搭完点右上角「运行」，输入「大户型适合什么扫地机器人」，能看到完整链路。

---

## 四、导出 DSL（这才是仓库里要留的东西）

新版 Dify 的入口是：**工作室 → 应用卡片右上角 `…` → 导出应用**（不叫"导出 DSL"了）。

导出的文件是 `.ifpkg`（本质是 ZIP，内含 `manifest.yaml` + `app.yaml`）。仓库里存两份：

```
dify/
├── README.md                    ← 本文件
├── saojingling.chatflow.yml     ← app.yaml 直接改名（可当 DSL 导入）
├── saojingling.app.ifpkg        ← 原始应用包（新版 Dify「导入应用」用这个）
└── screenshots/
    ├── 01_chatflow_canvas.png   ← 5 节点画布全图
    └── 02_answer_preview.png    ← 预览面板真实回答（含依据资料的分点推荐）
```

`.ifpkg` 解包方式：

```bash
unzip saojingling.app.ifpkg -d /tmp/ifpkg   # 得到 manifest.yaml + app.yaml
```

> **导入后必改一处**：`app.yaml` 里 HTTP 节点的 `url` 是导出时的隧道地址，
> 隧道重建后域名会变，导入前（或导入后立刻）改成当前地址，否则节点直接连不上。

DSL 里**不要**留真实密钥：`X-Tool-Key` 用 `sjling-demo-key` 这类占位，
真实值只放在本机 `.env` 里。当前导出件里的 key 就是演示占位值，可直接开源。

---

## 五、和手写版的差异（面试就讲这张表）

| 维度 | 手写版 | Dify 版 |
| --- | --- | --- |
| 检索 | 向量 + 中文 BM25 + RRF + BGE 重排，自建双评测集（Hit@1 65%→95%） | **直接复用**同一个 HTTP 接口，检索质量不变 |
| 降级 | BGE 忙 → RRF 顺序；模型不可用 → LLM 重排 | 平台无此概念，靠服务端自己的三档降级 |
| 编排改动 | 改代码 + 重启 | 拖节点，秒级生效 |
| 多用户隔离 | JWT + ContextVar 检索范围 | 用服务账号的静态密钥，隔离粒度变粗（真实缺陷，值得主动讲） |
| 部署 | 自己写 compose、Nginx、探针 | 平台自带，一天上线 |
| 适合场景 | 要调检索质量、要压测、要深度定制 | 业务侧快速验证、非技术同事自己改流程 |

**一句话总结**：Dify 解决的是「从想法到能用的应用」这一段，解决不了
「从能用到准」这一段——后者还是要写代码，也就是这个仓库主体在做的事。

---

## 六、已知坑

1. **超时**：HTTP 节点默认 10s，首次请求必超时（BGE 冷启动 40~60s）。设 60s + 重试。
2. **host.docker.internal**：Windows / macOS 的 Docker Desktop 自带；Linux 需要手动加 host。
3. **503 不是 bug**：`RagBuildBusy` 表示范围正在构建检索器，重试即可。
4. **端口**：Dify 默认抢 80，本项目用 8000，改 `EXPOSE_NGINX_PORT=3000` 最省事。
5. **密钥**：`X-Tool-Key` 不过期，只映射到服务账号（默认 legacy 账号 id=1），
   所以它只能看到公共资料 + 该账号名下的知识库。别把它配成管理员账号。
