"""知识库与文档的业务逻辑。

这一层负责「规则」，不负责 HTTP：
    - 归属校验（不是你的库 → 当作不存在）
    - 同名冲突、重复上传去重
    - 文件落盘、大小限制、后缀白名单
    - 删库/删文档时同步清向量

路由只做三件事：解析入参、调用这里、把领域异常翻译成状态码。

关于「上传」和「入库」为什么分开：
    一次 PDF 解析 + 向量化可能要几秒到几十秒，占着请求线程会让网关超时，
    用户看到的是「上传中」转圈直到失败。所以上传接口只做到「落盘 + 建 pending 记录」
    就返回 202，后续由后台任务推进状态，前端轮询。
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session

from fastapi_app.services import cache as cache_service
from fastapi_app.services.files import safe_filename, upload_dir
from fastapi_app.services.ingest import is_supported
from models.document import DOC_PARSING, DOC_PENDING, DOC_READY, Document
from models.knowledge_base import KnowledgeBase
from rag.index_service import DocumentIndexer
from repositories.document import DocumentRepository
from repositories.knowledge_base import KnowledgeBaseRepository
from schemas.knowledge import KnowledgeBaseRead
from utils.logger_handler import logger

# 单文件上限 20MB。PDF 超过这个量级通常是扫描件，
# 解析出来没有文字层，收下来也索引不到东西，不如早拒
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# 流式读的块大小：既不把整个文件读进内存，也不至于一次 4KB 慢得离谱
_READ_CHUNK = 1024 * 1024

# 知识库文档目录：uploads/knowledge_bases/kb_<id>/
KB_UPLOAD_SUBDIR = ("uploads", "knowledge_bases")


# ---------- 领域异常 ----------
# 定义在本层而不是直接用 HTTPException：
# 后台任务、命令行脚本复用这些函数时，不该被迫 import fastapi

class KnowledgeBaseNotFound(Exception):
    pass


class DocumentNotFound(Exception):
    pass


class KnowledgeBaseNameTaken(Exception):
    pass


class UnsupportedFileType(Exception):
    pass


class DocumentTooLarge(Exception):
    pass


@dataclass(frozen=True)
class SavedUpload:
    """落盘结果。分开存 filename / path，是因为两者用途不同：
    前者给用户看，后者给后台任务读。"""

    filename: str
    path: str
    md5: str
    size: int


# ---------- 文件落盘 ----------

def kb_upload_dir(knowledge_base_id: int) -> Path:
    """每个知识库一个目录。

    按库分目录不是为了好看，是为了删库时能物理清干净，
    以及避免两个用户上传同名文件互相覆盖。
    """
    path = upload_dir().joinpath(*KB_UPLOAD_SUBDIR[1:], f"kb_{knowledge_base_id}")
    path.mkdir(parents=True, exist_ok=True)
    return path


async def save_kb_upload(knowledge_base_id: int, upload) -> SavedUpload:
    """把上传文件流式写到磁盘，边写边算 md5、边判大小。

    为什么不用 `await upload.read()` 一次读完：
        Starlette 把它整个读进内存，20MB 的文件乘上并发就是几百 MB，
        而且超限时才发现已经晚了。分块读可以在超限的第一时间停手。

    为什么文件名前面拼 md5 前缀：
        同一个库里可能先后传两次同名不同内容的文件（比如 v1.pdf 改了内容还叫 v1.pdf）。
        只用原名会覆盖旧文件，导致旧文档记录的 stored_path 指向新内容——
        检索出来的东西和文档名对不上，这种 bug 极难发现。
    """
    filename = safe_filename(getattr(upload, "filename", None))
    if not is_supported(filename):
        raise UnsupportedFileType(f"不支持的文件类型，仅支持：.txt / .pdf")

    target_dir = kb_upload_dir(knowledge_base_id)
    # 先写临时文件，全部写完再原子改名：中途失败不会留下半个文件被人当成有效文档
    tmp_path = target_dir / f".upload-{uuid4().hex}.part"

    md5 = hashlib.md5()
    size = 0
    try:
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await upload.read(_READ_CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise DocumentTooLarge(
                        f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)}MB 上限"
                    )
                md5.update(chunk)
                f.write(chunk)
        if size == 0:
            raise UnsupportedFileType("文件内容为空")

        md5_hex = md5.hexdigest()
        target = target_dir / f"{md5_hex[:12]}_{filename}"
        os.replace(tmp_path, target)
    except Exception:
        # 任何失败都清掉临时文件，别在磁盘上留垃圾
        tmp_path.unlink(missing_ok=True)
        raise

    return SavedUpload(filename=filename, path=str(target), md5=md5_hex, size=size)


# ---------- 知识库 ----------

def create_knowledge_base(
    db: Session, user_id: int, name: str, description: str | None
) -> KnowledgeBase:
    repo = KnowledgeBaseRepository(db)
    # 预检一遍只为给出友好提示；真正的唯一性由数据库约束兜底（并发下预检会漏）
    for kb, _count in repo.list_for_user(user_id):
        if kb.name == name:
            raise KnowledgeBaseNameTaken(f"已存在同名知识库：{name}")
    return repo.create(user_id, name, description)


def list_knowledge_bases(db: Session, user_id: int) -> list[tuple[KnowledgeBase, int]]:
    """不走缓存的原版，给「需要 ORM 对象」的内部调用用。

    路由不要直接用这个：ORM 对象带着 Session 生命周期，
    既不能缓存，也不好跨层传。
    """
    return KnowledgeBaseRepository(db).list_for_user(user_id)


def list_knowledge_bases_view(db: Session, user_id: int) -> list[dict]:
    """列表接口的缓存版本：返回可直接进 response_model 的字典。

    为什么把缓存放在**服务层而不是路由层**：
        路由层的职责是「解析入参 → 调服务 → 翻译异常」。
        缓存策略（键、TTL、失效）属于业务规则，换个入口（比如将来的
        后台管理页）也该复用同一套，所以下沉一层。

    为什么缓存的是 dict 而不是 (KnowledgeBase, count)：
        后者一过 Session 就是 detached 对象，Redis 里也放不进去。
        这里用 KnowledgeBaseRead 导出，字段集合和接口契约天然一致——
        「缓存的形状 == 返回的形状」是最省心的设计。
    """
    cache = cache_service.get_cache()
    key = cache_service.kb_list_key(user_id)

    cached = cache.get_json(key)
    if cached is not None:
        logger.debug("知识库列表命中缓存", extra={"context": {"event": "cache.hit", "key": key}})
        return cached

    rows = KnowledgeBaseRepository(db).list_for_user(user_id)
    payload = [
        KnowledgeBaseRead.from_model(kb, count).model_dump(mode="json")
        for kb, count in rows
    ]
    cache.set_json(key, payload, ttl=cache_service.kb_list_ttl(payload))
    logger.debug(
        "知识库列表回源数据库",
        extra={"context": {"event": "cache.miss", "key": key, "rows": len(payload)}},
    )
    return payload


def invalidate_list_cache(user_id: int) -> None:
    """写操作之后清掉列表缓存。

    **必须在 db.commit() 之后调用**，顺序反了会有一个很短但真实的窗口：
        删缓存 → 并发请求回源读到旧数据 → 写回缓存 → 提交新数据
    结果就是缓存里留着旧值，直到 TTL 到期（最长 1 分钟后才「自愈」）。
    先提交再删，最坏情况只是慢半拍，不会错。
    """
    cache_service.get_cache().delete(cache_service.kb_list_key(user_id))


def get_knowledge_base(db: Session, user_id: int, kb_id: int) -> KnowledgeBase:
    kb = KnowledgeBaseRepository(db).get(user_id, kb_id)
    if kb is None:
        raise KnowledgeBaseNotFound("知识库不存在")
    return kb


def default_indexer() -> DocumentIndexer:
    """默认索引器：进程内单例。

    单独抽成函数是为了可替换——测试打桩只需要 monkeypatch 这一个名字，
    不用去动 rag 模块里的全局变量，也不会因为「第一次调用就实例化真模型」
    让单元测试去连 DashScope。
    """
    from rag.index_service import get_indexer

    return get_indexer()


def _resolve_indexer(indexer: DocumentIndexer | None) -> DocumentIndexer:
    return indexer if indexer is not None else default_indexer()


def delete_knowledge_base(
    db: Session, user_id: int, kb_id: int, indexer: DocumentIndexer | None = None
) -> None:
    """先清向量，再删行。

    顺序是刻意的：向量删失败时数据库还在，用户可以重试；
    反过来先删行，向量就成了谁也不知道该删的孤儿——它不会被检索到
    （filter 里那个 kb_id 已经没人引用），但会一直占着磁盘和索引。
    """
    get_knowledge_base(db, user_id, kb_id)  # 归属校验，不存在直接抛
    _resolve_indexer(indexer).delete_knowledge_base(kb_id)
    KnowledgeBaseRepository(db).delete(user_id, kb_id)


# ---------- 文档 ----------

async def create_document(
    db: Session, user_id: int, kb_id: int, upload
) -> tuple[Document, bool]:
    """接收上传，返回 (文档, 是否需要走后台入库)。

    去重规则：同一知识库内 md5 相同视为同一份内容。
        - 已完成（ready）：直接返回旧记录，不重复入库（否则同一段内容会占多个 Top-K 名额）
        - 曾失败（failed）：复用原行、重置状态，重新入库
        - 处理中（pending/parsing）：不重复入队，避免同一文件被并行处理两遍
    """
    get_knowledge_base(db, user_id, kb_id)  # 归属校验先做，避免为别人的库写文件
    saved = await save_kb_upload(kb_id, upload)

    repo = DocumentRepository(db)
    existing = repo.get_by_md5(kb_id, saved.md5)
    if existing is not None:
        if existing.status == DOC_READY:
            logger.info(f"[KB] 文档内容已存在（md5 命中），复用 document={existing.id}")
            return existing, False
        if existing.status not in (DOC_PENDING, DOC_PARSING):
            repo.reset_for_reingest(existing.id, saved.path)
            db.flush()
            db.refresh(existing)
            return existing, True
        return existing, False

    doc = repo.create(
        user_id=user_id,
        knowledge_base_id=kb_id,
        filename=saved.filename,
        stored_path=saved.path,
        md5=saved.md5,
        size_bytes=saved.size,
    )
    return doc, True


def get_document(db: Session, user_id: int, doc_id: int) -> Document:
    doc = DocumentRepository(db).get(user_id, doc_id)
    if doc is None:
        raise DocumentNotFound("文档不存在")
    return doc


def list_documents(db: Session, user_id: int, kb_id: int) -> list[Document]:
    get_knowledge_base(db, user_id, kb_id)  # 别人的库：连列表都不给看
    return DocumentRepository(db).list_for_kb(user_id, kb_id)


def delete_document(
    db: Session, user_id: int, doc_id: int, indexer: DocumentIndexer | None = None
) -> None:
    doc = get_document(db, user_id, doc_id)
    _resolve_indexer(indexer).delete_document(doc.id)
    DocumentRepository(db).delete(user_id, doc.id)
    # 磁盘文件先留着：删错了还能从 uploads 里捞回来。
    # 真正的清理由定时任务按「无引用的文件」批量做（完整版）。
