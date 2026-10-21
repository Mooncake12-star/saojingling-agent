import os, sys
import pytest

from utils.path_tool import get_project_root, ensure_project_root_in_syspath

ensure_project_root_in_syspath()

# 供本文件各处拼接测试数据路径使用（此前漏定义，导致 3 个用例 NameError）
project_root = get_project_root()


class TestPathTool:
    def test_get_project_root(self):
        from utils.path_tool import get_project_root
        root = get_project_root()
        assert os.path.isdir(root)
        assert os.path.exists(os.path.join(root, "app.py"))

    def test_get_abs_path(self):
        from utils.path_tool import get_abs_path
        path = get_abs_path("config/rag.yml")
        assert os.path.exists(path)


class TestConfigHandler:
    def test_load_rag_config(self):
        from utils.config_handler import rag_conf
        assert "chat_model_name" in rag_conf
        assert "embedding_model_name" in rag_conf

    def test_load_chroma_config(self):
        from utils.config_handler import chroma_conf
        assert "collection_name" in chroma_conf
        assert "persist_directory" in chroma_conf
        assert "k" in chroma_conf

    def test_load_prompts_config(self):
        from utils.config_handler import prompts_conf
        assert "main_prompt_path" in prompts_conf
        assert "rag_summarize_prompt_path" in prompts_conf


class TestPromptLoader:
    def test_load_main_prompts(self):
        from utils.prompt_loader import load_main_prompts
        prompt = load_main_prompts()
        assert isinstance(prompt, str)
        assert len(prompt) > 50

    def test_load_rag_prompts(self):
        from utils.prompt_loader import load_rag_prompts
        prompt = load_rag_prompts()
        assert isinstance(prompt, str)
        assert len(prompt) > 10

class TestFileHandler:
    def test_get_file_md5(self):
        from utils.file_handler import get_file_mds_hex
        test_file = os.path.join(project_root, "md5.text")
        if os.path.exists(test_file):
            md5 = get_file_mds_hex(test_file)
            assert md5 is not None
            assert len(md5) == 32

    def test_listdir_allowed_type(self):
        from utils.file_handler import listdir_with_allowed_type
        data_path = os.path.join(project_root, "data")
        files = listdir_with_allowed_type(data_path, ("txt", "pdf"))
        assert isinstance(files, tuple)
        for f in files:
            assert f.endswith(("txt", "pdf"))

    def test_txt_loader(self):
        from utils.file_handler import txt_loader
        sample = os.path.join(project_root, "data", "选购指南.txt")
        if os.path.exists(sample):
            docs = txt_loader(sample)
            assert len(docs) > 0
            assert hasattr(docs[0], "page_content")


class TestSchemas:
    def test_chat_request(self):
        from schemas import ChatRequest
        req = ChatRequest(query="你好")
        assert req.query == "你好"
        assert req.image_path is None

    def test_chat_request_with_image(self):
        from schemas import ChatRequest
        req = ChatRequest(query="分析图片", image_path="/tmp/test.jpg")
        assert req.image_path == "/tmp/test.jpg"

    def test_chat_response(self):
        from schemas import ChatResponse
        resp = ChatResponse(answer="这是回答")
        assert resp.answer == "这是回答"

    def test_status_response(self):
        from schemas import StatusResponse
        resp = StatusResponse(status="ok", mcp_connected=True, rag_ready=False)
        assert resp.status == "ok"
        assert resp.mcp_connected is True
        assert resp.rag_ready is False
