# app.py
import streamlit as st
import asyncio
import os
import sys
import base64

from agent.react_agent import ReactAgent, ReactMCPContextManager
from rag.vector_store import VectorStoreService

st.set_page_config(page_title="扫净灵 AI Agent", layout="wide")
st.title("扫净灵 AI Agent 智能助手")

# ──────────────────────────────────────────────
#  侧边栏：图片上传 + 报告下载入口
# ──────────────────────────────────────────────
with st.sidebar:
    st.header("📎 上传图片")
    uploaded_img = st.file_uploader("上传故障照片或产品图片", type=["jpg", "jpeg", "png", "webp"])
    if uploaded_img:
        upload_dir = "uploads"
        os.makedirs(upload_dir, exist_ok=True)
        img_path = os.path.join(upload_dir, uploaded_img.name)
        with open(img_path, "wb") as f:
            f.write(uploaded_img.getbuffer())
        st.session_state["uploaded_image_path"] = os.path.abspath(img_path)
        st.success(f"已上传：{uploaded_img.name}")

        if st.button("清除图片"):
            st.session_state.pop("uploaded_image_path", None)
            st.rerun()

    st.divider()
    st.header("📄 导出报告")
    if "last_report_path" in st.session_state:
        report_path = st.session_state["last_report_path"]
        if os.path.exists(report_path):
            with open(report_path, "rb") as f:
                st.download_button("下载最新报告", f, file_name=os.path.basename(report_path))

    st.divider()
    st.caption("扫净灵 v2.0")


def _init_knowledge():
    """首次启动时自动加载知识库（MD5 去重，已有文件跳过）"""
    try:
        vs = VectorStoreService()
        vs.load_document()
    except Exception as e:
        st.warning(f"知识库加载异常，部分功能可能受限: {e}")


def _init_mcp():
    """在独立事件循环中初始化 MCP 会话，返回 (mgr, loop)"""
    SERVER_COMMAND = sys.executable
    root_dir = os.path.dirname(os.path.abspath(__file__))
    server_dir = os.path.join(root_dir, "agent", "tools")
    server_script_name = "mcp_server.py"

    mgr = ReactMCPContextManager(command=SERVER_COMMAND, args=[server_script_name], script_dir=server_dir)

    original_cwd = os.getcwd()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        os.chdir(server_dir)
        loop.run_until_complete(mgr.__aenter__())
    except Exception as e:
        st.error(f"MCP 服务端拉起失败: {e}")
        raise
    finally:
        os.chdir(original_cwd)
    return mgr, loop


# =====================================================================
#  1. 初始化全局单例
# =====================================================================
if "agent" not in st.session_state:
    st.session_state["agent"] = ReactAgent()
    _init_knowledge()

if "mcp_mgr" not in st.session_state:
    mgr, loop = _init_mcp()
    st.session_state["mcp_mgr"] = mgr
    st.session_state["event_loop"] = loop

# =====================================================================
#  2. 聊天历史
# =====================================================================
if "messages" not in st.session_state:
    st.session_state.messages = []

for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

# =====================================================================
#  3. 对话处理
# =====================================================================
if prompt := st.chat_input("请输入您的指令"):
    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    with st.chat_message("assistant"):
        placeholder = st.empty()

        async def get_streaming_response():
            full = ""
            query_text = prompt
            if "uploaded_image_path" in st.session_state:
                img_path = st.session_state["uploaded_image_path"]
                query_text = f"{prompt}\n\n[用户已上传图片，文件路径：{img_path}，如需分析图片请使用 image_analyze 工具]"

            async for chunk in st.session_state["agent"].execute_stream(
                query_text, mcp_manager=st.session_state["mcp_mgr"]
            ):
                full += chunk
                placeholder.markdown(full + "▌")
            placeholder.markdown(full)
            return full

        loop = st.session_state["event_loop"]
        final_answer = loop.run_until_complete(get_streaming_response())
        st.session_state.messages.append({"role": "assistant", "content": final_answer})