import os, sys
import pytest


class TestMCPServer:
    def test_mcp_fastmcp_import(self):
        from mcp.server.fastmcp import FastMCP
        assert FastMCP is not None

    def test_mcp_stdlib_import(self):
        import mcp.types as types
        assert types is not None


class TestModelFactory:
    def test_factory_config(self):
        from utils.config_handler import rag_conf
        assert rag_conf["chat_model_name"] != ""
        assert rag_conf["embedding_model_name"] != ""
