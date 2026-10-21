"""文档异步入库：解析 → 切片 → 打标 → 向量化。

为什么整条链路拆成三个纯函数 + 一个编排类：
    parse_document / split_documents / attach_metadata 都是「输入什么、输出什么」
    的纯函数，不碰数据库、不碰向量库，可以单独测、单独替换格式。
    IngestService 只做三件事：改状态、串流程、把异常翻译成 failed。

为什么后台任务要自己开 session，不能复用请求的 Session：
    BackgroundTasks 在响应返回之后才执行，那时请求的 Session 已经 close 了。
    复用会拿到一个已关闭的会话（或更糟：一个被别处共用的会话，
    在另一个线程里并发读写）。所以这里一律 session_scope()。

为什么失败也要落库：
    只写日志的话，用户看到的是文档永远停在 parsing。失败原因必须能被前端读到，
    否则「为什么我的文档搜不到」这个问题只能靠运维翻日志回答。

已知取舍（写在这里，面试被问到不慌）：
    向量写入与状态更新不在同一个事务里——Chroma 不是关系库，没法参与 SQL 事务。
    如果 add_documents 成功但随后写 status 失败，文档会停在 parsing，
    向量却已经进了库。修复办法是「启动时扫一遍 parsing 超时的文档重跑」，
    属于完整版要做的事，缩减版先记录在案。
"""
from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from db.session import session_scope
from models.document import DOC_FAILED, DOC_PARSING, DOC_READY
from rag.index_service import DocumentIndexer, META_DOC_ID, META_KB_ID, META_USER_ID
from repositories.document import DocumentRepository
from utils.config_handler import chroma_conf
from utils.file_handler import pdf_loader, txt_loader
from utils.logger_handler import logger

# 只支持这两种：和 chroma_conf['allow_knowledge_file_type'] 保持一致，
# 但用字典表达「后缀 → loader」，省掉一长串 if/elif，新增格式只加一行
LOADERS = {
    ".txt": txt_loader,
    ".pdf": pdf_loader,
}

SUPPORTED_SUFFIXES = tuple(LOADERS)

# 错误信息落库前截断：异常里可能带整段 HTML 或超长路径
MAX_ERROR_LEN = 500


class IngestError(RuntimeError):
    """入库过程中的可预期失败（格式不支持、文件没文本）。"""


def is_supported(filename: str) -> bool:
    return Path(filename).suffix.lower() in LOADERS


def parse_document(path: str | Path) -> list[Document]:
    """按后缀选 loader，读出原始 Document 列表（PDF 是一页一个 Document）。"""
    file_path = Path(path)
    loader = LOADERS.get(file_path.suffix.lower())
    if loader is None:
        raise IngestError(f"不支持的文件类型：{file_path.suffix or '无后缀'}")

    if not file_path.is_file():
        # 落盘成功但文件消失（被人手工删了 / 容器重启丢了卷）
        raise IngestError(f"文件不存在：{file_path.name}")

    try:
        return loader(str(file_path))
    except Exception as exc:  # noqa: BLE001 - 解析失败原因太多，统一翻译
        raise IngestError(f"解析失败：{exc}") from exc


def split_documents(docs: list[Document]) -> list[Document]:
    """按 chroma.yml 里的参数切片，保持与全量重建索引时一致的分片口径。"""
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chroma_conf["chunk_size"],
        chunk_overlap=chroma_conf["chunk_overlap"],
        separators=chroma_conf["separators"],
        length_function=len,
    )
    return splitter.split_documents(docs)


def attach_metadata(
    chunks: list[Document],
    *,
    user_id: int,
    knowledge_base_id: int,
    document_id: int,
    filename: str,
) -> list[Document]:
    """给每个 chunk 打上归属标识。

    user_id 必须打上，而不是只打 knowledge_base_id：
        检索时「不指定知识库」的默认范围是 {user_id: 我} OR {is_public: true}，
        没有 user_id 的 chunk 对任何人都不匹配——用户会发现自己刚上传的文档
        搜不到，而且日志里没有任何报错。这类「写了但查不到」是最难查的故障。

    **必须新建 metadata dict，不能原地改**：
    split_documents 产出的 chunk 会共享（或部分复制）原文档的 metadata 对象，
    原地改一个 chunk 可能连带改掉其它 chunk，甚至污染调用方持有的对象。
    """
    stamped: list[Document] = []
    for index, chunk in enumerate(chunks):
        metadata = dict(chunk.metadata or {})
        metadata[META_USER_ID] = user_id
        metadata[META_KB_ID] = knowledge_base_id
        metadata[META_DOC_ID] = document_id
        # 保留原始来源，同时补一个稳定的展示名（loader 给的 source 是绝对路径）
        metadata["source"] = filename
        metadata["chunk_index"] = index
        stamped.append(Document(page_content=chunk.page_content, metadata=metadata))
    return stamped


class IngestService:
    """把「一个文档」从 pending 推到 ready（或 failed）。"""

    def __init__(self, indexer: DocumentIndexer | None = None) -> None:
        self._indexer = indexer

    @property
    def indexer(self) -> DocumentIndexer:
        if self._indexer is None:
            from rag.index_service import get_indexer

            self._indexer = get_indexer()
        return self._indexer

    def ingest(self, document_id: int) -> None:
        """后台任务入口。**不抛异常**：异常在这里就转成 failed 状态。

        不抛的原因是没人接：BackgroundTasks 里的异常只会打到服务器日志，
        对用户等于「什么都没发生」。既然一定要降级，就降得让用户看得见。
        """
        with session_scope() as session:
            repo = DocumentRepository(session)
            doc = repo.get_by_id(document_id)
            if doc is None:
                logger.warning(f"[Ingest] 文档 {document_id} 不存在，跳过")
                return
            # 先落 parsing，再开始干活：这样「卡住」和「排队」在状态上可区分
            repo.set_status(document_id, DOC_PARSING)
            knowledge_base_id = doc.knowledge_base_id
            owner_id = doc.user_id
            stored_path = doc.stored_path
            filename = doc.filename

        try:
            chunks = attach_metadata(
                split_documents(parse_document(stored_path)),
                user_id=owner_id,
                knowledge_base_id=knowledge_base_id,
                document_id=document_id,
                filename=filename,
            )
            if not chunks:
                # 空 PDF（扫描件没有文字层）会走到这里，提前失败比写 0 条更好
                raise IngestError("文档中没有可索引的文本内容")
            count = self.indexer.add_chunks(chunks)
        except Exception as exc:  # noqa: BLE001 - 一切失败都要落成 failed
            message = str(exc)[:MAX_ERROR_LEN] or exc.__class__.__name__
            logger.error(f"[Ingest] 文档 {document_id} 入库失败: {message}")
            with session_scope() as session:
                DocumentRepository(session).set_status(
                    document_id, DOC_FAILED, error_message=message
                )
            return

        with session_scope() as session:
            DocumentRepository(session).set_status(
                document_id, DOC_READY, chunk_count=count, error_message=None
            )
        logger.info(
            f"[Ingest] 文档 {document_id} 入库完成：{len(chunks)} 个 chunk（kb={knowledge_base_id}）"
        )
