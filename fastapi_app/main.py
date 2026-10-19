import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, UploadFile, File, HTTPException
#RedirectResponse：FastAPI 提供的一个响应类，返回 HTTP 重定向（状态码 307/302），
# 告诉浏览器"你要找的东西在另一个地址，自动跳过去吧"。
#StreamingResponse — FastAPI 提供的特殊响应类，不一次性返回完整数据，而是保持 TCP 连接打开，持续发送字节流
from fastapi.responses import StreamingResponse, FileResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

from utils.path_tool import ensure_project_root_in_syspath, get_project_root
ensure_project_root_in_syspath()

from fastapi_app.schemas import ChatRequest, ChatResponse, StatusResponse
from fastapi_app.deps import manager
from utils.logger_handler import logger
#uvicorn fastapi_app.main:app --host 0.0.0.0 --port 8000
#http://10.63.209.44:8000/docs 本地ip端口号


@asynccontextmanager
async def lifespan(app: FastAPI):
    await manager.startup()
    yield
    await manager.shutdown()

#FastAPI 启动时先进入 lifespan，执行 yield 前的代码
app = FastAPI(
    title="扫净灵 AI Agent",
    description="基于 ReAct Agent + RAG + MCP 的智能助手后端服务",
    version="2.0.0",
    lifespan=lifespan,
)
#实现前后端分离后此部分代码有用
app.add_middleware(
    CORSMiddleware,           # 安装跨域中间件（处理浏览器跨域拦截）
    allow_origins=["*"],      # 允许哪些域名访问，["*"]=全部放行（生产应写具体域名）
    allow_credentials=True,   # 是否允许前端带 Cookie/Token 等凭证
    allow_methods=["*"],      # 允许哪些 HTTP 方法，["*"]=全部（GET/POST/PUT/DELETE…）
    allow_headers=["*"],      # 允许哪些自定义请求头，["*"]=全部
)


@app.get("/")
async def root():
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=StatusResponse)
async def health_check():
    return StatusResponse(
        status="ok" if manager.is_ready else "degraded",
        mcp_connected=manager._mcp_started,
        rag_ready=manager._rag_ready,
    )

#async def → 这是一个协程函数，调用后返回一个协程对象，交由事件循环调度
#_stream_answer → 下划线开头表示"内部私有函数"，不对外暴露
#生成器函数（含 yield）：每次执行到 yield 就暂停，把值丢给调用方，自己原地等待下一次被唤醒。
#sse格式：data: 消息内容\n\n
async def _stream_answer(query: str, image_path: str | None = None) -> AsyncGenerator[bytes, None]:
    query_text = query
    if image_path and os.path.exists(image_path):
        query_text = f"{query}\n\n[用户已上传图片，文件路径：{image_path}，如需分析图片请使用 image_analyze 工具]"
    async for chunk in manager.agent.execute_stream(query_text, mcp_manager=manager.mcp_mgr):
        yield f"data: {chunk}\n\n".encode("utf-8")
    yield "data: [DONE]\n\n".encode("utf-8")


@app.post("/chat/stream")
async def chat_stream(body: ChatRequest):
    if not manager.is_ready:
        raise HTTPException(status_code=503, detail="Agent 服务未就绪")
    return StreamingResponse(
        _stream_answer(body.query, body.image_path),
        #— 设置 HTTP 头 Content-Type: text/event-stream，告诉浏览器这是 SSE 流，浏览器 EventSource 解析器才会启动
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",# 禁止浏览器和代理缓存，流式数据每次都得拿最新的
            "Connection": "keep-alive",# 保持 TCP 长连接不断开，否则发一个字断一次
            "X-Accel-Buffering": "no",# 禁用 Nginx 缓冲，否则 Nginx 会攒够一整块才转发，失去流式效果
        },
    )


@app.post("/chat", response_model=ChatResponse)
async def chat_sync(body: ChatRequest):
    if not manager.is_ready:
        raise HTTPException(status_code=503, detail="Agent 服务未就绪")
    query_text = body.query
    if body.image_path and os.path.exists(body.image_path):
        query_text = f"{body.query}\n\n[用户已上传图片，文件路径：{body.image_path}，如需分析图片请使用 image_analyze 工具]"
    full = ""
    async for chunk in manager.agent.execute_stream(query_text, mcp_manager=manager.mcp_mgr):
        full += chunk
    return ChatResponse(answer=full)


@app.post("/history/clear")
async def clear_history():
    manager.agent.clear_history()
    return {"message": "对话历史已清空"}


@app.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    upload_dir = os.path.join(get_project_root(), "uploads")
    os.makedirs(upload_dir, exist_ok=True)

    file_path = os.path.join(upload_dir, file.filename)
    content = await file.read()
    '''
    合起来 "wb" 就是二进制写入模式：
    - 文件存在 → 清空重写
    - 文件不存在 → 创建再写
    - 写的是字节（bytes），不是文本字符串（str）
    '''
    with open(file_path, "wb") as f:
        f.write(content)

    abs_path = os.path.abspath(file_path)
    logger.info(f"[FastAPI] 图片已上传: {abs_path}")
    return {"filename": file.filename, "path": abs_path}


@app.get("/report/{filename}")
async def download_report(filename: str):
    reports_dir = os.path.join(get_project_root(), "logs", "reports")
    file_path = os.path.join(reports_dir, filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="报告文件不存在")
    #在 Starlette（FastAPI 底层）里，*FileResponse 只要传了 filename 参数，
    # 就默认加 Content-Disposition: attachment*，
    # media_type注解
    '''
    HTTP 的 Content-Type 响应头，告诉浏览器返回的是什么类型的数据：
    - application/pdf → 这是 PDF 文件
    - image/jpeg → 这是 JPEG 图片
    - text/event-stream → 这是 SSE 流
      浏览器根据 media_type 决定怎么处理：是下载、展示、还是解析。
    '''
    return FileResponse(file_path, media_type="application/pdf", filename=filename)
