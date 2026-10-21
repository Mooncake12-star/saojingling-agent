#!/usr/bin/env bash
# 容器启动脚本：迁移 → 交接给 CMD
#
# 为什么迁移放在容器里而不是 CI 里跑一次：
#   单机部署下「迁移」和「新代码」必须同时发生。放在镜像启动流程里，
#   `docker compose up` 一条命令就能把库升到最新版本，不用记第二条命令。
#
# 多副本下的问题（本次补上的）：
#   2 个副本同时启动 = 2 个 alembic 同时 upgrade。轻则报
#   "relation already exists"，重则迁移到一半一个进程退出，留下半成品 schema。
#   这里用 flock 在**共享目录**上加锁，让它们排队。
#
#   为什么用 flock 而不是 PostgreSQL advisory lock：
#   本项目的默认数据库是 SQLite，拿不到 PG 的锁；而 flock 只依赖文件系统，
#   两种库都能用。前提是锁文件放在所有副本都能看到的目录里
#   （compose 里挂载 ./deploy/locks，同一台宿主机上的容器共享）。
set -euo pipefail

echo "[entrypoint] 数据库地址: ${ZST_DB_URL:-<默认 data/app.db>}"

# 跳过迁移的开关：运维想手动控制升级时机时用（例如先备份、再单独跑一次）
if [ "${ZST_SKIP_MIGRATIONS:-0}" = "1" ]; then
    echo "[entrypoint] 已设置 ZST_SKIP_MIGRATIONS=1，跳过迁移"
    exec "$@"
fi

LOCK_DIR="${ZST_MIGRATION_LOCK_DIR:-/app/locks}"
LOCK_WAIT="${ZST_MIGRATION_LOCK_WAIT:-120}"

if mkdir -p "$LOCK_DIR" 2>/dev/null && [ -d "$LOCK_DIR" ]; then
    exec 9>"$LOCK_DIR/migrate.lock"
    if flock -w "$LOCK_WAIT" 9; then
        echo "[entrypoint] 已取得迁移锁，开始 upgrade"
        # 迁移失败就退出，不要用 || true 吞掉：
        # 表结构不对时宁可起不来，也不要让服务带着旧 schema 接收请求
        alembic upgrade head
        echo "[entrypoint] 迁移完成"
        flock -u 9
    else
        # 等不到锁说明另一个副本卡在迁移上。带着不确定的 schema 启动更危险
        echo "[entrypoint] 等待迁移锁超时（${LOCK_WAIT}s），退出以避免并发迁移" >&2
        exit 1
    fi
else
    # 锁目录不可写（例如只读根文件系统且没挂载）时退化为「直接跑」：
    # 单副本下这与旧行为一致，多副本下会并发迁移——日志里说明白，别让它静默发生
    echo "[entrypoint] 警告：锁目录 $LOCK_DIR 不可用，跳过迁移互斥（仅单副本安全）" >&2
    alembic upgrade head
fi

echo "[entrypoint] 启动服务: $*"
# exec：让 uvicorn 接管 PID 1，SIGTERM 才能直达
exec "$@"
