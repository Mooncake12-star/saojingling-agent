"""对外暴露「工具能力」的接口层（给进程外的编排器用）。

为什么要有这个模块
──────────────────
本项目的能力原本只对内提供：Agent 通过 MCP 调工具，MCP 工具在主进程里
直接拿 `rag/local_tool.py` 的单例。也就是说，「混合检索 + BGE 重排」
这套调优成果**只有这一个前端能吃到**。

Dify / Coze 这类可视化编排平台的价值在「快速拼装业务流 + 一键发布」，
它们不缺生成能力，缺的恰恰是中文检索这一段（默认的向量检索在中文短问句、
口语化 query 上掉分明显——这正是本项目评测集里 65% → 95% 那段差距）。

所以这里开一个**只检索、不生成**的 HTTP 入口：
    外部编排器负责流程与生成，本项目的检索链路负责「找得准」。
两边各用长项，也不用把 1.5GB 的重排模型搬到对方容器里。

安全模型（为什么有两套凭证）
──────────────────────────
    普通前端      → Bearer JWT，走 `get_current_user`，能立刻作废
    外部编排器    → `X-Tool-Key` 静态密钥，映射到一个服务账号

为什么外部编排器不能也用 JWT：access token 是短期的（本项目双 Token，
过期后要靠 refresh 静默续期），而 Dify 的 HTTP 节点只会在答错 401 时
原样重试，不会去刷新 token——真用 JWT，演示到一半就会因为过期而断。
静态密钥换成「服务账号」在语义上也更准确：这是机器调用，不是某个人的会话。

代价必须写清楚：静态密钥不会过期，一旦泄漏就是长期有效。
所以它只映射到一个**专用的服务账号**（默认 legacy 账号，只能看到公共资料
和自己名下的知识库），不是管理员；生产环境应当配合 IP 白名单和轮换。
"""
from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from db.session import get_db
from fastapi_app.api.dependencies import get_current_user
from rag.local_tool import RagBuildBusy, get_rag_service
from rag.scope import use_scope
from utils.logger_handler import logger

router = APIRouter(prefix="/tools", tags=["工具能力"])

# 注意：这里重新声明一个 auto_error=False 的 scheme，
# 而不是复用 dependencies.oauth2_scheme，是为了让本模块在没有 token 时
# 也能走到 X-Tool-Key 分支——用 dependencies 里那个也行，
# 但引一个仅用于「不报错」的对象会让依赖关系更难读。
_bearer = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

# 外部编排器的静态密钥。为空表示「没有开启外部调用」，此时只有 JWT 能进来
_TOOL_API_KEY = os.getenv("ZST_TOOL_API_KEY", "").strip()
# 密钥对应的服务账号。默认 1 = legacy 账号（只能看到公共资料 + 其名下知识库）
_TOOL_USER_ID = int(os.getenv("ZST_TOOL_USER_ID", "1"))


class RetrieveRequest(BaseModel):
    """检索请求。

    query 限长 500：这条链路会先过一次 LLM 改写，
    超长输入既不会被改写成功，也会白花一次调用。
    """

    query: str = Field(min_length=1, max_length=500, description="用户问题原文")
    knowledge_base_id: int | None = Field(
        default=None,
        description="限定检索某个知识库；不传则检索「服务账号自己的 chunk + 公共资料」",
    )
    top_k: int | None = Field(default=None, ge=1, le=20, description="最终返回条数，默认沿用服务端配置")


class RetrieveHit(BaseModel):
    rank: int
    content: str
    source: str
    knowledge_base_id: int | None = None
    score: float | None = None


class RetrieveResponse(BaseModel):
    query: str
    rewritten_query: str | None = None
    hits: list[RetrieveHit]


def resolve_caller(
    db: Session = Depends(get_db),
    token: str | None = Depends(_bearer),
    x_tool_key: str | None = Header(default=None, alias="X-Tool-Key"),
) -> int:
    """解析调用方身份，返回 user_id。

    顺序很重要：先看 X-Tool-Key。外部编排器不会同时带 JWT，
    但如果有人两个都带，应该以静态密钥为准，避免出现
    「密钥错了却因为顺手抄了个 token 而通过」这种隐蔽的误配置。
    """
    if x_tool_key is not None:
        if not _TOOL_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="外部调用未开启：服务端未配置 ZST_TOOL_API_KEY",
            )
        # compare_digest 防时序侧信道：普通 == 会按前缀逐字节短路比较，
        # 理论上可被用来一位一位地猜出密钥
        if not hmac.compare_digest(x_tool_key, _TOOL_API_KEY):
            logger.warning("[工具接口] X-Tool-Key 校验失败")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="X-Tool-Key 无效")
        return _TOOL_USER_ID

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="未提供凭证：请带 Bearer token 或 X-Tool-Key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # 直接复用现有校验逻辑（查库、禁用、token_version 全部一模一样），
    # 不复制一份——复制出来的第二份迟早和第一份不一致
    return get_current_user(token=token, db=db).id


@router.post(
    "/retrieve",
    response_model=RetrieveResponse,
    summary="只检索不生成（供 Dify / Coze 等外部编排器调用）",
)
def retrieve(payload: RetrieveRequest, user_id: int = Depends(resolve_caller)) -> RetrieveResponse:
    """返回重排后的检索片段，不调用生成模型。

    用同步 def：整条链路（向量检索、BGE 推理）都是阻塞的，
    交给 FastAPI 的线程池跑；写成 async 会占住事件循环，
    一次 BGE 推理就能拖住所有并发请求。
    """
    # 范围在入口处设定，而不是让调用方传 user_id：
    # use_scope 里的 ContextVar 会随 to_thread 一起复制到线程池，
    # 检索实现（跑在别的线程里）读到的仍是这个范围
    with use_scope(user_id, payload.knowledge_base_id):
        try:
            service = get_rag_service()
        except RagBuildBusy as exc:
            # 503 而不是 500：语义是「正在初始化，稍后重试」，
            # 编排器会重试，500 会被当成代码 bug
            logger.warning(f"[工具接口] 检索服务繁忙: {exc}")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"知识库正在初始化，请稍后重试（{exc}）",
            ) from exc

        result = service.retrieve(payload.query, top_k=payload.top_k)

    hits = [RetrieveHit(**hit) for hit in result["hits"]]
    logger.info(
        f"[工具接口] 检索完成 uid={user_id} kb={payload.knowledge_base_id} "
        f"hits={len(hits)} query={payload.query!r}"
    )
    return RetrieveResponse(
        query=payload.query,
        rewritten_query=result["rewritten_query"],
        hits=hits,
    )
