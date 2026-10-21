"""向量索引的写入与删除。

为什么要单独一层，而不是直接在 ingest 里 new 一个 VectorStoreService：
    `VectorStoreService.__init__` 会加载 embedding 模型（首次约 10-20 秒），
    而现在有三个地方要用它——后台入库、删文档、删知识库。
    如果不收在一起，每处都要自己管「什么时候创建、什么时候复用」，
    最后一定会出现同一个进程里加载三份模型。

隔离靠 metadata，不靠 collection：
    每个 chunk 都带 knowledge_base_id / document_id，检索时用 where 过滤。
    另一个方案是「一个知识库一个 collection」，好处是隔离更硬，
    代价是 collection 随用户数线性增长，备份、迁移、统计都要遍历一遍。
    当前规模（单机 SQLite + 本地 Chroma）用 metadata 过滤足够，
    而且删文档时 `where={"document_id": id}` 一条命令就能清干净。

注意：metadata 的键名必须与检索时的 filter 键名**完全一致**。
    写错不会报错，只会安静地返回空结果——这是这类系统最典型的假故障。
"""
from __future__ import annotations

import threading

from langchain_core.documents import Document
from utils.logger_handler import logger

_indexer = None
_indexer_lock = threading.Lock()

# 这两个键名是写入与检索之间唯一的契约，抽成常量防止两边各写各的
META_USER_ID = "user_id"
META_KB_ID = "knowledge_base_id"
META_DOC_ID = "document_id"


class DocumentIndexer:
    """把 chunk 写进向量库 / 从向量库删掉。"""

    def __init__(self, store=None) -> None:
        # 允许注入（测试用假实现），默认惰性创建真实向量库
        self._store = store

    @property
    def store(self):
        if self._store is None:
            from rag.vector_store import VectorStoreService

            logger.info("[Indexer] 首次写入向量库，正在初始化 embedding 与 Chroma...")
            self._store = VectorStoreService()
        return self._store

    def add_chunks(self, chunks: list[Document]) -> int:
        """写入一批 chunk，返回写入条数。

        调用方保证每个 chunk 的 metadata 已带 kb/doc 标识；
        这里不再补，是因为补的地方越多，漏的地方就越多——统一在 ingest 里打标。
        """
        if not chunks:
            return 0
        self.store.vector_store.add_documents(chunks)
        return len(chunks)

    def delete_document(self, document_id: int) -> None:
        self.store.vector_store.delete(where={META_DOC_ID: document_id})

    def delete_knowledge_base(self, knowledge_base_id: int) -> None:
        self.store.vector_store.delete(where={META_KB_ID: knowledge_base_id})


def get_indexer() -> DocumentIndexer:
    """进程内单例：embedding 模型与 Chroma 连接只建一份。"""
    global _indexer
    if _indexer is not None:
        return _indexer
    with _indexer_lock:
        if _indexer is None:
            _indexer = DocumentIndexer()
    return _indexer
