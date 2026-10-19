import sqlite3
import json

from utils.logger_handler import logger
import os

DB_PATH = None
_INITIALIZED = False


def get_db_path():
    global DB_PATH
    if DB_PATH is None:
        from utils.path_tool import get_abs_path
        DB_PATH = get_abs_path("data/zst.db")
    return DB_PATH


def get_connection():
    global _INITIALIZED
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    # 让查询结果能按列名访问：row["role"] 而不是 row[1]
    conn.row_factory = sqlite3.Row
    #让SQLite从"读写互斥"变成"读写并发
    conn.execute("PRAGMA journal_mode=WAL")
    if not _INITIALIZED:
        # 建表
        _init_db(conn)
        #灌入初始/产品数据
        _seed_data(conn)
        _INITIALIZED = True
    return conn


def _init_db(conn):
    c = conn.cursor()
    #real：小数类型
    c.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id          TEXT PRIMARY KEY,
            brand       TEXT,
            name        TEXT NOT NULL,
            type        TEXT,
            price       REAL,
            specs       TEXT,
            features    TEXT,
            suitable_for TEXT,
            rating      REAL
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            role        TEXT NOT NULL,
            content     TEXT,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()


def _seed_data(conn):
    c = conn.cursor()

    # 从 JSON 导入产品（仅在首次）
    count = c.execute("SELECT COUNT(*) FROM products").fetchone()[0]
    if count == 0:
        from utils.path_tool import get_abs_path
        json_path = get_abs_path("data/products.json")
        if os.path.exists(json_path):
            with open(json_path, "r", encoding="utf-8") as f:
                products = json.load(f)
            for p in products:
                c.execute("""
                    INSERT INTO products (id, brand, name, type, price, specs, features, suitable_for, rating)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    p["id"], p["brand"], p["name"], p["type"], p["price"],
                    json.dumps(p.get("specs", {}), ensure_ascii=False),
                    json.dumps(p.get("features", []), ensure_ascii=False),
                    json.dumps(p.get("suitable_for", []), ensure_ascii=False),
                    p.get("rating", 0)
                ))
            logger.info(f"[DB] 已导入 {len(products)} 条产品数据")
