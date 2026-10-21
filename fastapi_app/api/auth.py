"""鉴权接口：注册、登录、刷新、当前用户、全设备登出。

设计取舍写在这里，面试被问到可以直接讲：

登录用 OAuth2PasswordRequestForm 而不是 JSON body
    好处是 /docs 的 Authorize 按钮能直接用，测试和联调都省事；
    代价是字段名固定为 username / password（这里 username 就是邮箱）。

刷新时不做「一次性 refresh」（refresh token rotation + 旧 token 作废）
    无状态 JWT 没有地方标记「这个 refresh 用过了」，要做得引入存储（Redis 或表）。
    缩减版接受这个取舍：refresh 有效期 7 天，配合 token_version 可以整体作废。

登出实现为「全设备登出」
    把 users.token_version + 1，所有已签发 token 立刻失效。
    代价是不能只踢某一个设备——那需要按设备记录 jti，留到完整版。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from db.session import get_db
from fastapi_app.api.dependencies import CurrentUser
from fastapi_app.services import auth as auth_service
from schemas.auth import (
    LogoutResponse,
    RefreshRequest,
    RegisterRequest,
    TokenPairResponse,
    UserRead,
)

router = APIRouter(prefix="/auth", tags=["鉴权"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="注册",
)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> UserRead:
    try:
        user = auth_service.register(db, body.email, body.password)
        # get_db 只负责回滚和关闭，不自动提交：提交时机由业务决定，
        # 这样一个请求里的多次写可以一起成功或一起失败。
        db.commit()
    except auth_service.EmailTaken as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except auth_service.WeakPassword as exc:
        db.rollback()
        raise HTTPException(
            status_code=422,  # Starlette 1.0 起常量改名为 UNPROCESSABLE_CONTENT，写数字最稳
            detail=str(exc)
        ) from exc
    except IntegrityError as exc:
        # 并发注册：两个请求同时通过「邮箱不存在」检查，只有一个能插入成功
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="该邮箱已注册") from exc

    db.refresh(user)
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPairResponse, summary="登录（表单）")
def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: Session = Depends(get_db),
) -> TokenPairResponse:
    try:
        tokens = auth_service.login(db, form.username, form.password)
    except auth_service.InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    except auth_service.InactiveUser as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    return TokenPairResponse(**tokens.as_dict())


@router.post("/refresh", response_model=TokenPairResponse, summary="用 refresh 换新 token")
def refresh(body: RefreshRequest, db: Session = Depends(get_db)) -> TokenPairResponse:
    try:
        tokens = auth_service.refresh(db, body.refresh_token)
    except auth_service.InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)
        ) from exc
    return TokenPairResponse(**tokens.as_dict())


@router.get("/me", response_model=UserRead, summary="当前用户")
def me(user: CurrentUser) -> UserRead:
    return UserRead.model_validate(user)


@router.post("/logout", response_model=LogoutResponse, summary="全设备登出")
def logout(user: CurrentUser, db: Session = Depends(get_db)) -> LogoutResponse:
    updated = auth_service.logout_all(db, user)
    db.commit()
    return LogoutResponse(message="已退出登录，全部 token 已失效", token_version=updated.token_version)
