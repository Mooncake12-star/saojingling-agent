# 生产用镜像：不带 --reload、不 bind mount 源码，代码只在构建时进来一次
#
# 两个体积陷阱（踩过）：
#   1. sentence-transformers → torch。默认 wheel 带 CUDA 依赖，一个 2GB+
#      下载完了 99% 用不上（CPU 推理足够跑 bge-reranker）。
#      所以先单独装 CPU 版，后面 pip 看到已满足就不会再拉 CUDA 版。
#   2. 镜像里预下载 reranker 模型（下面那行）。取舍：
#      镜像大 1GB 左右，换来容器冷启动不用等下载——线上重启不会卡 1 分钟。
#      如果更在意镜像体积，删掉那行，改成挂载模型缓存卷。
FROM python:3.11-slim

# PYTHONUNBUFFERED：日志立刻出 stdout，不然 docker logs 里会攒着、
#   崩溃时最后几行丢失（排查事故时最需要那几行）
# PYTHONDONTWRITEBYTECODE：不写 .pyc，容器重建时代码层更干净
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# libgomp1：torch 的 OpenMP 运行时，slim 镜像里没有，缺了会
#   "libgomp.so.1: cannot open shared object file"
# curl：给 HEALTHCHECK 用
# tzdata：没有它 TZ=Asia/Shanghai 不生效，日志时间全是 UTC（排查时差 8 小时很难受）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl tzdata \
    && rm -rf /var/lib/apt/lists/*

# 先装 CPU 版 torch（约 200MB），让后面的 sentence-transformers 复用它。
# 索引地址做成参数：国内服务器直连 download.pytorch.org 经常几十 KB/s，
# 6GB 依赖能等到以为卡死。换阿里云/清华的 torch 镜像时不用改 Dockerfile
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir torch --index-url "$TORCH_INDEX_URL"

# pip 源同理做成参数。默认官方源（本地/海外构建正常）；
# 国内服务器部署时传清华源，装依赖从几十分钟降到几分钟
ARG PIP_INDEX_URL=https://pypi.org/simple
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt --index-url "$PIP_INDEX_URL"

# 模型预热开关。
#   1 = 构建时把 reranker 权重打进镜像（镜像 +1GB，冷启动不用等下载）
#   0 = 跳过，运行时第一次用到才下载
# 为什么要有这个开关：构建机内存小的时候（比如 Docker Desktop 只分到 4GB），
# 加载 bge-reranker-v2-m3 会让构建进程 OOM；这种情况跳过预热、靠运行时下载更稳
ARG PRELOAD_MODEL=1
RUN if [ "$PRELOAD_MODEL" = "1" ]; then \
        HF_ENDPOINT=https://hf-mirror.com python -c \
        "from sentence_transformers import CrossEncoder; CrossEncoder('BAAI/bge-reranker-v2-m3')"; \
    else \
        echo "[build] 跳过模型预热"; \
    fi

COPY . .
RUN chmod +x deploy/entrypoint.sh

EXPOSE 8000

# entrypoint 先跑 alembic upgrade head，再 exec uvicorn：
#   - 迁移失败 → 容器起不来（fail fast），不会带着旧表结构对外服务
#   - 用 exec 让 uvicorn 成为 PID 1，docker stop 发的 SIGTERM 能直达进程，
#     否则信号被 shell 吃掉，只能等 10s 后 SIGKILL
ENTRYPOINT ["deploy/entrypoint.sh"]
CMD ["uvicorn", "fastapi_app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
