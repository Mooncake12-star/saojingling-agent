"""一次性元数据迁移：给历史 chunk 补 `is_public` 标记。

背景：
    多用户检索隔离（rag/scope.py）依赖 chunk metadata 做过滤。
    内置资料（data/ 下那几份文档）必须是「所有人可见」，靠 is_public=True 表达。
    但这批 chunk 是 P3 之前写进向量库的，没有这个字段——不补的话，
    加了范围过滤之后它们对所有人都不可见，表现是「知识库突然什么都答不出来」。

为什么不能靠重新跑 load_document()：
    它按 md5 文件去重，看到 md5.text 里已有记录就直接跳过，根本不会碰到老 chunk。
    清理 md5.text 再重跑会重复写入一份（同样的内容在库里存两遍），
    检索时同一段文本占据多个 Top-K 名额。

幂等：只处理「没有 user_id 且没有 is_public」的 chunk，重复执行不会重复写。
"""
from __future__ import annotations

import sys

from utils.logger_handler import logger

BATCH = 500


def migrate() -> tuple[int, int]:
    """返回 (扫描总数, 实际更新数)。"""
    from rag.vector_store import VectorStoreService

    store = VectorStoreService().vector_store
    data = store.get(include=["metadatas"])
    ids = data.get("ids") or []
    metadatas = data.get("metadatas") or []

    pending_ids: list[str] = []
    pending_metas: list[dict] = []
    for chunk_id, meta in zip(ids, metadatas):
        meta = meta or {}
        # 已有归属（用户上传的）或已标记的，一律不动
        if "user_id" in meta or meta.get("is_public"):
            continue
        pending_ids.append(chunk_id)
        pending_metas.append({**meta, "is_public": True})

    for start in range(0, len(pending_ids), BATCH):
        # langchain_chroma 的 Chroma 包装类没有暴露 update，只能落到它内部持有的
        # chromadb Collection 上。这里用私有属性是有意的：一次性迁移脚本，
        # 与其为了它去给业务代码加一层「只改元数据」的封装，不如直接调底层 API。
        store._collection.update(
            ids=pending_ids[start:start + BATCH],
            metadatas=pending_metas[start:start + BATCH],
        )

    return len(ids), len(pending_ids)


if __name__ == "__main__":
    total, updated = migrate()
    logger.info(f"[MigrateMeta] 共 {total} 个 chunk，补充 is_public 标记 {updated} 个")
    print(f"chunks={total} updated={updated}")
    sys.exit(0)
