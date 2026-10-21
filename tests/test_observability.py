"""可观测性与缓存（W7）的测试。

三件事分开测，因为它们失败时的现象完全不同：
    1. request_id 断链 → 日志里一堆请求分不清谁是谁（不报错，最容易被忽略）
    2. 缓存不失效   → 用户建了库刷新看不到（看起来像前端 bug）
    3. Redis 挂掉   → 如果没做降级，整个服务跟着 500

缓存测试全部用内存假实现，**不连真实 Redis**：
    需要验证的是「键设计、失效时机、降级行为」这套逻辑，
    而不是 redis-py 能否连上 localhost。连真 Redis 的测试会在 CI 上随机红。
"""
from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

import db.session as db_session
import fastapi_app.api.knowledge_bases as kb_api
import fastapi_app.services.cache as cache_service
import fastapi_app.services.knowledge as kb_service
from db.base import Base
from db.session import build_engine
from fastapi_app.api import api_router
from fastapi_app.middleware import RequestContextMiddleware
from repositories import knowledge_base as kb_repo_module
from tests_helpers import register_and_login


class MemoryCache:
    """内存版缓存，记录读写次数，用来断言「到底有没有回源」。"""

    def __init__(self) -> None:
        self.data: dict[str, object] = {}
        self.ttls: dict[str, int] = {}
        self.hits = 0
        self.misses = 0

    def get_json(self, key: str):
        if key in self.data:
            self.hits += 1
            return self.data[key]
        self.misses += 1
        return None

    def set_json(self, key: str, value, ttl: int) -> None:
        self.data[key] = value
        self.ttls[key] = ttl

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.data.pop(key, None)

    def backend(self) -> str:
        return "memory"


class BrokenClient:
    """模拟「Redis 进程挂了」：连接层异常。"""

    def get(self, key):
        raise ConnectionError("Connection refused")

    def setex(self, key, ttl, value):
        raise ConnectionError("Connection refused")

    def delete(self, *keys):
        raise ConnectionError("Connection refused")


class FakeIndexer:
    """假向量库：删库/删文档只记账，不碰真实的 chroma_db。

    为什么必须有它：删知识库会先清向量。不替换的话测试会去开真实的
    `chroma_db/chroma.sqlite3`——本机没跑容器时碰巧能过，**容器起来之后就报
    database is locked**（同一个 SQLite 文件被两个进程打开）。
    这类「看运气过」的测试比失败更危险，所以这里显式隔离。
    """

    def __init__(self) -> None:
        self.deleted_documents: list[int] = []
        self.deleted_kbs: list[int] = []

    def add_chunks(self, chunks):
        return len(chunks)

    def delete_document(self, document_id):
        self.deleted_documents.append(document_id)

    def delete_knowledge_base(self, knowledge_base_id):
        self.deleted_kbs.append(knowledge_base_id)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    engine = build_engine(f"sqlite:///{(tmp_path / 'obs.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_session, "SessionLocal", TestingSession)

    upload_root = tmp_path / "uploads"
    upload_root.mkdir()
    monkeypatch.setattr(kb_service, "upload_dir", lambda: upload_root)
    monkeypatch.setattr(kb_api, "run_ingest", lambda document_id: None)
    # 向量库也隔离掉，见 FakeIndexer 的说明
    monkeypatch.setattr(kb_service, "default_indexer", lambda: FakeIndexer())

    # 默认装上内存缓存；单独测降级的用例会再覆盖它
    mem = MemoryCache()
    monkeypatch.setattr(cache_service, "_cache", mem)

    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    app.include_router(api_router)

    with TestClient(app) as client:
        yield {"client": client, "cache": mem, "session": TestingSession}
    engine.dispose()


@pytest.fixture()
def auth(env):
    return register_and_login(env["client"], "obs@test.local")


# ---------- 1. request_id ----------

def test_request_id_is_generated_and_returned(env):
    resp = env["client"].get("/health")
    assert resp.status_code == 200

    rid = resp.headers.get("X-Request-ID")
    assert rid, "响应头必须带 X-Request-ID，否则用户报障时无法定位日志"
    # 16 位十六进制：短到能念出来，长到不可能在一次排查里撞
    assert re.fullmatch(r"[0-9a-f]{16}", rid)


def test_request_id_from_upstream_is_reused(env):
    resp = env["client"].get("/health", headers={"X-Request-ID": "trace-from-nginx"})
    # 复用上游 id 才能把 nginx access log 和应用日志对上；这里还验证大小写不敏感
    assert resp.headers["X-Request-ID"] == "trace-from-nginx"

    too_long = "x" * 200
    resp = env["client"].get("/health", headers={"X-Request-ID": too_long})
    assert len(resp.headers["X-Request-ID"]) == 64


def test_logs_carry_request_id_and_user_id(env, auth, caplog):
    caplog.set_level(logging.INFO, logger="agent")
    resp = env["client"].get(
        "/knowledge-bases", headers={**auth["headers"], "X-Request-ID": "rid-log-check"}
    )
    assert resp.status_code == 200

    records = [r for r in caplog.records if getattr(r, "request_id", None) == "rid-log-check"]
    assert records, "这个请求产生的日志必须都带同一个 request_id"

    # 访问日志：方法/路径/状态码/耗时，缺一个都不好查慢请求
    access = [r for r in records if getattr(r, "context", {}).get("event") == "http.access"]
    assert access, "每个请求应有一条 http.access 访问日志"
    assert access[0].context["status"] == 200
    assert access[0].context["path"] == "/knowledge-bases"
    assert isinstance(access[0].context["duration_ms"], float)

    # 登录之后 user_id 要自动进日志，业务代码不用手动传
    assert any(getattr(r, "user_id", None) == auth["user_id"] for r in records)


# ---------- 2. 缓存 ----------

def _count_list_calls(monkeypatch) -> dict[str, int]:
    """统计「回源数据库」的次数。缓存生效时这个数字应该停住不动。"""
    calls = {"n": 0}
    original = kb_repo_module.KnowledgeBaseRepository.list_for_user

    def counting(self, user_id):
        calls["n"] += 1
        return original(self, user_id)

    monkeypatch.setattr(kb_repo_module.KnowledgeBaseRepository, "list_for_user", counting)
    return calls


def test_kb_list_cache_hit_avoids_database(env, auth, monkeypatch):
    calls = _count_list_calls(monkeypatch)

    first = env["client"].get("/knowledge-bases", headers=auth["headers"])
    second = env["client"].get("/knowledge-bases", headers=auth["headers"])
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()

    assert calls["n"] == 1, "第二次请求应命中缓存，不该再查库"
    assert env["cache"].hits == 1 and env["cache"].misses == 1


def test_cache_invalidated_after_write(env, auth, monkeypatch):
    calls = _count_list_calls(monkeypatch)

    assert env["client"].get("/knowledge-bases", headers=auth["headers"]).json() == []
    baseline = calls["n"]

    created = env["client"].post(
        "/knowledge-bases", json={"name": "扫拖手册"}, headers=auth["headers"]
    )
    assert created.status_code == 201
    kb_id = created.json()["id"]

    # 建库后立刻可见：如果只在 TTL 到期后才更新，用户会以为没建成功
    listed = env["client"].get("/knowledge-bases", headers=auth["headers"]).json()
    assert [item["id"] for item in listed] == [kb_id]
    # 缓存被清掉了 → 这次必须回源。注意建库本身也会读一次列表（同名预检），
    # 所以只能比对「列表请求带来的增量」，不能数总次数
    assert calls["n"] > baseline, "写操作必须让缓存失效，否则读到的是提交前的快照"

    # 上传文档会改变 document_count，同样要失效
    env["client"].post(
        f"/knowledge-bases/{kb_id}/documents",
        headers=auth["headers"],
        files={"file": ("a.txt", "滤网三个月换一次。".encode("utf-8"), "text/plain")},
    )
    listed = env["client"].get("/knowledge-bases", headers=auth["headers"]).json()
    assert listed[0]["document_count"] == 1

    env["client"].delete(f"/knowledge-bases/{kb_id}", headers=auth["headers"])
    assert env["client"].get("/knowledge-bases", headers=auth["headers"]).json() == []


def test_cache_key_is_per_user(env):
    """缓存也是数据面：键里没有 user_id 就是一条越权通道。"""
    alice = register_and_login(env["client"], "alice@test.local")
    bob = register_and_login(env["client"], "bob@test.local")

    env["client"].post("/knowledge-bases", json={"name": "alice 的库"}, headers=alice["headers"])
    alice_list = env["client"].get("/knowledge-bases", headers=alice["headers"]).json()
    bob_list = env["client"].get("/knowledge-bases", headers=bob["headers"]).json()

    assert len(alice_list) == 1
    assert bob_list == [], "B 不能从缓存里读到 A 的列表"


def test_empty_list_is_cached_with_short_ttl(env, auth):
    """空结果也缓存（防穿透），但 TTL 更短——空列表是最容易变的。"""
    env["client"].get("/knowledge-bases", headers=auth["headers"])
    key = cache_service.kb_list_key(auth["user_id"])
    assert env["cache"].ttls[key] <= 10 + cache_service.KB_LIST_TTL_JITTER


# ---------- 3. Redis 故障降级 ----------

def test_redis_failure_degrades_to_database(env, auth, monkeypatch):
    calls = {"factory": 0}

    def broken_factory():
        calls["factory"] += 1
        return BrokenClient()

    # 换成真的 RedisCache，只是客户端一定报错
    monkeypatch.setattr(cache_service, "_cache", cache_service.RedisCache(client_factory=broken_factory))

    first = env["client"].get("/knowledge-bases", headers=auth["headers"])
    second = env["client"].get("/knowledge-bases", headers=auth["headers"])

    # 缓存挂了，业务必须照常
    assert first.status_code == second.status_code == 200
    assert first.json() == []

    # 熔断：第一次失败后一段时间内不再尝试，避免每个请求都等一次连接超时
    assert calls["factory"] == 1, "熔断期间不该反复建连接"

    health = env["client"].get("/health").json()
    assert health["cache"] == "redis(circuit-open)"


def test_health_reports_cache_backend(env):
    assert env["client"].get("/health").json()["cache"] == "memory"
