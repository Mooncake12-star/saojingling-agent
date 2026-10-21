"""文件接口：图片上传与报告下载。

P2 起两者都要求登录：
    上传会把文件写进服务器磁盘，匿名可写等于给了别人一个免费存储；
    报告是「某次排查的输出」，虽然还没有按用户分目录（缩减版的取舍），
    但也不该让未登录的人枚举文件名去撞。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import FileResponse

from fastapi_app.api.dependencies import CurrentUser
from fastapi_app.services.files import resolve_report, save_upload
from schemas import UploadResponse
from utils.logger_handler import logger

router = APIRouter(tags=["文件"])


@router.post("/upload", response_model=UploadResponse, summary="上传图片")
async def upload_image(
    user: CurrentUser,
    file: UploadFile = File(...),
) -> UploadResponse:
    try:
        name, abs_path = await save_upload(file)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    logger.info(f"[FastAPI] 图片已上传: {abs_path} (user={user.id})")
    return UploadResponse(filename=name, path=abs_path)


@router.get("/report/{filename}", summary="下载 PDF 报告")
async def download_report(filename: str, user: CurrentUser) -> FileResponse:
    try:
        path = resolve_report(filename)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="报告文件不存在"
        ) from exc

    # FileResponse 传了 filename 会自动加 Content-Disposition: attachment，浏览器变成下载
    return FileResponse(
        str(path), media_type="application/pdf", filename=os.path.basename(path)
    )
