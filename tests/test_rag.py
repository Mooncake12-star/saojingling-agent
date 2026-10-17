import os, sys
import pytest


class TestVectorStore:
    def test_vector_store_init(self):
        from rag.vector_store import VectorStoreService
        vs = VectorStoreService()
        assert vs.vector_store is not None
        assert vs.splitter is not None

    def test_vector_store_config(self):
        from utils.config_handler import chroma_conf
        assert chroma_conf["k"] >= 1
        assert chroma_conf["chunk_size"] >= 50
        assert "txt" in chroma_conf["allow_knowledge_file_type"]


class TestRagServiceConfig:
    def test_rag_config(self):
        from utils.config_handler import rag_conf
        assert isinstance(rag_conf["chat_model_name"], str)
        assert isinstance(rag_conf["embedding_model_name"], str)

    def test_rag_prompt_exists(self):
        from utils.prompt_loader import load_rag_prompts
        prompt = load_rag_prompts()
        assert "{context}" in prompt or "{question}" in prompt or "{input}" in prompt
if __name__ == '__main__':
    TestRagServiceConfig.test_rag_config()