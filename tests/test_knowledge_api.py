"""知识库与异步入库的测试。

三条主线：
1. 隔离：别人的知识库/文档一律 404，连列表都不给看
2. 上传契约：202 + pending、后缀白名单、大小上限、内容去重
3. 入库流程：成功 → ready + chunk 带 kb/doc 标识；失败 → failed + 原因落库

关键取舍：**不真跑 embedding**。
    真模型要连 DashScope、要十几秒加载，测一次就能把 CI 拖到无法忍受。
    IngestService 的 indexer 是构造参数，注入假实现即可完整验证
    「解析 → 切片 → 打标 → 状态流转」这条逻辑链；
    真正需要验证的是这条链，而不是第三方向量库能否连通。
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import db.session as db_session
import fastapi_app.api.knowledge_bases as kb_api
import fastapi_app.services.knowledge as kb_service
from db.base import Base
from db.session import build_engine, session_scope
from fastapi_app.api import api_router
from fastapi_app.services.ingest import IngestService
from models.document import DOC_FAILED, DOC_READY, Document
from tests_helpers import register_and_login


class FakeIndexer:
    """假向量库：只记录被写了什么、删了什么。"""

    def __init__(self):
        self.chunks = []
        self.deleted_documents = []
        self.deleted_kbs = []

    def add_chunks(self, chunks):
        self.chunks.extend(chunks)
        return len(chunks)

    def delete_document(self, document_id):
        self.deleted_documents.append(document_id)

    def delete_knowledge_base(self, knowledge_base_id):
        self.deleted_kbs.append(knowledge_base_id)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时库 + 临时上传目录 + 不跑后台入库。

    三处都要换掉，否则测试会往真实的 data/app.db 和 uploads/ 里写东西：
        SessionLocal → 临时 sqlite
        knowledge.upload_dir → tmp_path/uploads
        kb_api.run_ingest → 空函数（后台任务不执行真实解析）
    """
    engine = build_engine(f"sqlite:///{(tmp_path / 'kb.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", TestingSession)

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    monkeypatch.setattr(kb_service, "upload_dir", lambda: upload_root)

    monkeypatch.setattr(kb_api, "run_ingest", lambda document_id: None)

    app = FastAPI()
    app.include_router(api_router)
    with TestClient(app) as client:
        yield {"client": client, "session": TestingSession, "upload_root": upload_root}
    engine.dispose()


def _create_kb(client, headers, name="产品手册", description="测试用"):
    resp = client.post("/knowledge-bases", json={"name": name, "description": description}, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _upload(client, headers, kb_id, filename="a.txt", content="扫地机器人每周清理一次尘盒。\n\n滤网三个月换一次。"):
    return client.post(
        f"/knowledge-bases/{kb_id}/documents",
        headers=headers,
        files={"file": (filename, content.encode("utf-8"), "text/plain")},
    )


# ---------- 知识库 CRUD ----------

def test_create_and_list_knowledge_base(env):
    client = env["client"]
    user = register_and_login(client, "kb1@test.local")

    kb = _create_kb(client, user["headers"])
    assert kb["name"] == "产品手册"
    assert kb["document_count"] == 0
    assert "id" in kb and "created_at" in kb

    listed = client.get("/knowledge-bases", headers=user["headers"]).json()
    assert [item["id"] for item in listed] == [kb["id"]]


def test_duplicate_kb_name_conflicts(env):
    client = env["client"]
    user = register_and_login(client, "kb2@test.local")
    _create_kb(client, user["headers"])

    resp = client.post("/knowledge-bases", json={"name": "产品手册"}, headers=user["headers"])
    assert resp.status_code == 409


def test_kb_name_is_trimmed_and_blank_rejected(env):
    client = env["client"]
    user = register_and_login(client, "kb3@test.local")

    blank = client.post("/knowledge-bases", json={"name": "   "}, headers=user["headers"])
    assert blank.status_code == 422

    trimmed = client.post("/knowledge-bases", json={"name": "  售后  "}, headers=user["headers"])
    assert trimmed.status_code == 201
    assert trimmed.json()["name"] == "售后"


def test_knowledge_base_requires_login(env):
    client = env["client"]
    assert client.get("/knowledge-bases").status_code == 401
    assert client.post("/knowledge-bases", json={"name": "x"}).status_code == 401


# ---------- 越权 ----------

def test_other_users_kb_is_404(env):
    client = env["client"]
    alice = register_and_login(client, "alice@test.local")
    bob = register_and_login(client, "bob@test.local")
    kb = _create_kb(client, alice["headers"], name="alice的库")

    # 不是 403：对 bob 来说这个 id 本来就不存在
    assert client.get(f"/knowledge-bases/{kb['id']}", headers=bob["headers"]).status_code == 404
    assert client.delete(f"/knowledge-bases/{kb['id']}", headers=bob["headers"]).status_code == 404
    assert _upload(client, bob["headers"], kb["id"]).status_code == 404
    assert client.get(f"/knowledge-bases/{kb['id']}/documents", headers=bob["headers"]).status_code == 404
    # bob 的列表里也看不到
    assert client.get("/knowledge-bases", headers=bob["headers"]).json() == []


def test_other_users_document_is_404(env):
    client = env["client"]
    alice = register_and_login(client, "alice2@test.local")
    bob = register_and_login(client, "bob2@test.local")
    kb = _create_kb(client, alice["headers"])
    doc = _upload(client, alice["headers"], kb["id"]).json()

    assert client.get(f"/documents/{doc['id']}", headers=bob["headers"]).status_code == 404
    assert client.delete(f"/documents/{doc['id']}", headers=bob["headers"]).status_code == 404


# ---------- 上传契约 ----------

def test_upload_returns_202_pending_without_path(env):
    client = env["client"]
    user = register_and_login(client, "up1@test.local")
    kb = _create_kb(client, user["headers"])

    resp = _upload(client, user["headers"], kb["id"])
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["filename"] == "a.txt"
    assert body["chunk_count"] == 0
    # 服务器绝对路径不能出现在响应里
    assert "stored_path" not in body

    # 状态查询接口读到的也是 pending（后台任务被桩掉了）
    status_resp = client.get(f"/documents/{body['id']}", headers=user["headers"])
    assert status_resp.json()["status"] == "pending"

    # 文档计数进到列表里
    listed = client.get("/knowledge-bases", headers=user["headers"]).json()
    assert listed[0]["document_count"] == 1


def test_upload_rejects_unsupported_type(env):
    client = env["client"]
    user = register_and_login(client, "up2@test.local")
    kb = _create_kb(client, user["headers"])

    resp = client.post(
        f"/knowledge-bases/{kb['id']}/documents",
        headers=user["headers"],
        files={"file": ("payload.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 415


def test_upload_rejects_oversize_file(env, monkeypatch):
    client = env["client"]
    user = register_and_login(client, "up3@test.local")
    kb = _create_kb(client, user["headers"])
    monkeypatch.setattr(kb_service, "MAX_UPLOAD_BYTES", 32)

    resp = _upload(client, user["headers"], kb["id"], content="x" * 100)
    assert resp.status_code == 413
    # 临时文件不能留在磁盘上
    assert not list(env["upload_root"].rglob("*.part"))


def test_same_content_is_deduplicated(env):
    client = env["client"]
    user = register_and_login(client, "up4@test.local")
    kb = _create_kb(client, user["headers"])

    first = _upload(client, user["headers"], kb["id"]).json()
    second = _upload(client, user["headers"], kb["id"]).json()
    assert first["id"] == second["id"]

    with env["session"]() as s:
        assert s.query(Document).count() == 1


def test_same_name_different_content_keeps_both_files(env):
    """同名不同内容不能在磁盘上互相覆盖——否则旧文档会指向新内容。"""
    client = env["client"]
    user = register_and_login(client, "up5@test.local")
    kb = _create_kb(client, user["headers"])

    _upload(client, user["headers"], kb["id"], content="第一版内容")
    _upload(client, user["headers"], kb["id"], content="第二版内容")

    files = list((env["upload_root"] / "knowledge_bases" / f"kb_{kb['id']}").iterdir())
    assert len(files) == 2
    contents = {f.read_text(encoding="utf-8") for f in files}
    assert contents == {"第一版内容", "第二版内容"}


# ---------- 删除 ----------

def test_delete_document_removes_vectors(env, monkeypatch):
    client = env["client"]
    fake = FakeIndexer()
    monkeypatch.setattr(kb_service, "default_indexer", lambda: fake)

    user = register_and_login(client, "del1@test.local")
    kb = _create_kb(client, user["headers"])
    doc = _upload(client, user["headers"], kb["id"]).json()

    resp = client.delete(f"/documents/{doc['id']}", headers=user["headers"])
    assert resp.status_code == 200
    assert fake.deleted_documents == [doc["id"]]
    assert client.get(f"/documents/{doc['id']}", headers=user["headers"]).status_code == 404


def test_delete_kb_cascades_documents(env, monkeypatch):
    client = env["client"]
    fake = FakeIndexer()
    monkeypatch.setattr(kb_service, "default_indexer", lambda: fake)

    user = register_and_login(client, "del2@test.local")
    kb = _create_kb(client, user["headers"])
    _upload(client, user["headers"], kb["id"])

    resp = client.delete(f"/knowledge-bases/{kb['id']}", headers=user["headers"])
    assert resp.status_code == 200
    assert fake.deleted_kbs == [kb["id"]]

    with env["session"]() as s:
        assert s.query(Document).filter(Document.knowledge_base_id == kb["id"]).count() == 0


# ---------- 入库流程（直接测 service） ----------

def test_ingest_marks_ready_and_stamps_metadata(env, tmp_path):
    fake = FakeIndexer()
    service = IngestService(indexer=fake)

    source = tmp_path / "manual.txt"
    source.write_text("滤网建议三个月更换一次。\n\n尘盒每周清理。", encoding="utf-8")

    with env["session"]() as s:
        user_id = _first_user_id(s)
        from repositories.document import DocumentRepository

        repo = DocumentRepository(s)
        doc = repo.create(
            user_id=user_id,
            knowledge_base_id=_make_kb(s, user_id),
            filename="manual.txt",
            stored_path=str(source),
            md5="0" * 32,
            size_bytes=source.stat().st_size,
        )
        s.commit()
        doc_id = doc.id

    service.ingest(doc_id)

    with env["session"]() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == DOC_READY
        assert doc.chunk_count == len(fake.chunks) > 0
        assert doc.error_message is None

    # 每个 chunk 都带 kb/doc 标识，否则无法按库隔离检索
    assert all(c.metadata["knowledge_base_id"] == doc.knowledge_base_id for c in fake.chunks)
    assert all(c.metadata["document_id"] == doc_id for c in fake.chunks)
    assert all(c.metadata["source"] == "manual.txt" for c in fake.chunks)


def test_ingest_failure_is_recorded(env, tmp_path):
    service = IngestService(indexer=FakeIndexer())

    with env["session"]() as s:
        user_id = _first_user_id(s)
        from repositories.document import DocumentRepository

        doc = DocumentRepository(s).create(
            user_id=user_id,
            knowledge_base_id=_make_kb(s, user_id),
            filename="gone.txt",
            stored_path=str(tmp_path / "not-exists.txt"),
            md5="1" * 32,
            size_bytes=0,
        )
        s.commit()
        doc_id = doc.id

    service.ingest(doc_id)

    with env["session"]() as s:
        doc = s.get(Document, doc_id)
        assert doc.status == DOC_FAILED
        assert doc.error_message and "不存在" in doc.error_message


def test_ingest_missing_document_is_noop(env):
    IngestService(indexer=FakeIndexer()).ingest(999999)  # 不抛异常即可


# ---------- 辅助 ----------

def _first_user_id(session) -> int:
    from models.user import User

    user = session.query(User).first()
    if user is None:
        user = User(email="owner@test.local", password_hash="!disabled")
        session.add(user)
        session.flush()
    return user.id


def _make_kb(session, user_id: int) -> int:
    from models.knowledge_base import KnowledgeBase

    kb = KnowledgeBase(user_id=user_id, name=f"kb-{user_id}-{session.query(KnowledgeBase).count()}")
    session.add(kb)
    session.flush()
    return kb.id
