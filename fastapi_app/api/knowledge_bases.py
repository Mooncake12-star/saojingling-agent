"""知识库与文档接口。

路由层只做三件事：解析入参 → 调 service → 把领域异常翻译成状态码。
所有归属判断都在 service 里，路由不接受任何「user_id」参数——
请求体的 user_id 可以被伪造，token 里的不行。

为什么上传接口是 `async def`，其它接口是 `def`：
    上传必须 `await file.read()` 才能拿到内容，只能异步；
    其余接口只碰 SQLite（同步驱动），写 `async def` 会把阻塞调用
    塞进事件循环，反而拖慢并发。两种写法混用在这里是有意的，
    代价是上传接口里的 SQL 会短暂占用事件循环——文件已在内存/磁盘，
    DB 操作只有两条 insert/update，影响可接受。

为什么上传返回 202 而不是 200/201：
    202 Accepted 的语义是「已受理，尚未完成」。用户看到 202 就知道
    该去轮询 status，而不是以为文档马上能搜到。
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.session import get_db
from fastapi_app.api.dependencies import CurrentUser
from fastapi_app.services import knowledge as kb_service
from fastapi_app.services.ingest import IngestService
from schemas import (
    DeleteResponse,
    DocumentRead,
    KnowledgeBaseCreate,
    KnowledgeBaseRead,
)
from utils.logger_handler import logger

router = APIRouter(tags=["知识库"])

# 模块级单例：构造时不加载任何模型，真正入库时才惰性初始化向量库
_ingest_service = IngestService()


def run_ingest(document_id: int) -> None:
    """后台任务入口。

    单独包一层函数是为了让测试能替换它——测 API 的人不该被
    「解析 PDF + 调 embedding 接口」拖进慢测试。
    """
    _ingest_service.ingest(document_id)


# ---------- 知识库 ----------

@router.post(
    "/knowledge-bases",
    response_model=KnowledgeBaseRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建知识库",
)
def create_knowledge_base(
    body: KnowledgeBaseCreate,
    user: CurrentUser,
    db: Session = Depends(get_db),
) -> KnowledgeBaseRead:
    try:
        kb = kb_service.create_knowledge_base(db, user.id, body.name, body.description)
        db.commit()
    except kb_service.KnowledgeBaseNameTaken as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except IntegrityError as exc:
        # 并发建同名库：预检都通过了，靠唯一约束兜住
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="已存在同名知识库"
        ) from exc

    db.refresh(kb)
    # 提交之后再失效缓存：反序会让并发读把旧列表写回去（见 invalidate_list_cache 注释）
    kb_service.invalidate_list_cache(user.id)
    return KnowledgeBaseRead.from_model(kb, 0)


@router.get("/knowledge-bases", response_model=list[KnowledgeBaseRead], summary="我的知识库列表")
def list_knowledge_bases(user: CurrentUser, db: Session = Depends(get_db)) -> list[KnowledgeBaseRead]:
    """带缓存的列表。

    这是本项目唯一做缓存的地方，选它的理由：调用频率最高（前端每次进首页都拉），
    数据变化最少（用户偶尔建/删库），而且天然按用户分片——缓存键带 user_id 就隔离了。
    """
    payload = kb_service.list_knowledge_bases_view(db, user.id)
    return [KnowledgeBaseRead.model_validate(item) for item in payload]


@router.get("/knowledge-bases/{kb_id}", response_model=KnowledgeBaseRead, summary="知识库详情")
def get_knowledge_base(
    kb_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> KnowledgeBaseRead:
    try:
        kb = kb_service.get_knowledge_base(db, user.id, kb_id)
    except kb_service.KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    count = len(kb_service.list_documents(db, user.id, kb_id))
    return KnowledgeBaseRead.from_model(kb, count)


@router.delete(
    "/knowledge-bases/{kb_id}",
    response_model=DeleteResponse,
    summary="删除知识库（含文档与向量）",
)
def delete_knowledge_base(
    kb_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> DeleteResponse:
    try:
        kb_service.delete_knowledge_base(db, user.id, kb_id)
        db.commit()
    except kb_service.KnowledgeBaseNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - 向量库故障也要回滚数据库，避免半删
        db.rollback()
        logger.error(f"[KB] 删除知识库 {kb_id} 失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="删除知识库失败，请稍后重试",
        ) from exc
    kb_service.invalidate_list_cache(user.id)
    return DeleteResponse(message="知识库已删除", deleted_id=kb_id)


# ---------- 文档 ----------

@router.post(
    "/knowledge-bases/{kb_id}/documents",
    response_model=DocumentRead,
    status_code=status.HTTP_202_ACCEPTED,
    summary="上传文档（异步入库）",
)
async def upload_document(
    kb_id: int,
    background: BackgroundTasks,
    user: CurrentUser,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> DocumentRead:
    try:
        doc, need_ingest = await kb_service.create_document(db, user.id, kb_id, file)
        db.commit()
    except kb_service.KnowledgeBaseNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except kb_service.UnsupportedFileType as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=str(exc)
        ) from exc
    except kb_service.DocumentTooLarge as exc:
        db.rollback()
        raise HTTPException(
            status_code=413,  # Starlette 1.0 已把常量改名，写数字最稳
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        # safe_filename 抛的「文件名不合法」
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except IntegrityError as exc:
        # 并发上传同一份文件：唯一约束挡住第二次插入
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该文档正在处理中，请勿重复上传"
        ) from exc

    # 新文档会让列表里的 document_count 变化，缓存同样要清
    kb_service.invalidate_list_cache(user.id)

    if need_ingest:
        # 提交之后再入队：后台任务在另一个 Session 里读不到未提交的行
        background.add_task(run_ingest, doc.id)

    db.refresh(doc)
    return DocumentRead.model_validate(doc)


@router.get(
    "/knowledge-bases/{kb_id}/documents",
    response_model=list[DocumentRead],
    summary="知识库的文档列表",
)
def list_documents(
    kb_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> list[DocumentRead]:
    try:
        docs = kb_service.list_documents(db, user.id, kb_id)
    except kb_service.KnowledgeBaseNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return [DocumentRead.model_validate(d) for d in docs]


@router.get("/documents/{doc_id}", response_model=DocumentRead, summary="查询文档入库状态")
def get_document(
    doc_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> DocumentRead:
    try:
        doc = kb_service.get_document(db, user.id, doc_id)
    except kb_service.DocumentNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return DocumentRead.model_validate(doc)


@router.delete(
    "/documents/{doc_id}",
    response_model=DeleteResponse,
    summary="删除文档（含向量）",
)
def delete_document(
    doc_id: int, user: CurrentUser, db: Session = Depends(get_db)
) -> DeleteResponse:
    try:
        kb_service.delete_document(db, user.id, doc_id)
        db.commit()
    except kb_service.DocumentNotFound as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.error(f"[KB] 删除文档 {doc_id} 失败: {exc}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="删除文档失败，请稍后重试",
        ) from exc
    # document_count 变了，列表缓存必须失效——这是最容易漏的一处：
    # 只想着「建库/删库才改列表」，忘了列表里还有文档数
    kb_service.invalidate_list_cache(user.id)
    return DeleteResponse(message="文档已删除", deleted_id=doc_id)
