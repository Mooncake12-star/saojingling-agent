"""文件读写：上传落盘、报告定位。

两个函数都不抛 HTTPException——它们只知道「文件操作」，不知道「HTTP」。
路由负责把 FileNotFoundError 翻译成 404。这个分界很重要：
将来换成命令行工具或定时任务复用这些逻辑时，不用改业务代码。
"""
from __future__ import annotations

import os
from pathlib import Path

from utils.path_tool import get_project_root

UPLOAD_DIRNAME = "uploads"
REPORT_SUBDIR = ("logs", "reports")


def _safe_name(filename: str | None) -> str:
    """只取文件名部分，丢掉任何目录成分。

    改造前直接用了 `file.filename` 拼路径，前端传 `../../config/agent.yml`
    就能覆盖项目里任意文件——这类漏洞叫路径穿越，是上传接口的经典问题。
    这里用 Path(...).name 取最后一段，Windows 下的 `..\\..\\x` 同样会被去掉。
    """
    name = Path(filename or "").name.strip()
    if not name or name in {".", ".."}:
        raise ValueError("文件名不合法")
    return name


# 公开别名：知识库模块要复用同一套「防路径穿越」规则，
# 但跨模块 import 一个下划线开头的函数等于承认它是私有的，不如给个正名
safe_filename = _safe_name


def upload_dir() -> Path:
    path = Path(get_project_root()) / UPLOAD_DIRNAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def report_dir() -> Path:
    path = Path(get_project_root()).joinpath(*REPORT_SUBDIR)
    return path


async def save_upload(upload) -> tuple[str, str]:
    """保存上传文件，返回 (安全文件名, 绝对路径)。"""
    name = _safe_name(getattr(upload, "filename", None))
    target = upload_dir() / name
    content = await upload.read()
    with open(target, "wb") as f:
        f.write(content)
    return name, os.path.abspath(target)


def resolve_report(filename: str) -> Path:
    """把报告文件名解析成本地绝对路径，不存在就抛 FileNotFoundError。"""
    name = _safe_name(filename)
    target = report_dir() / name
    # 再校验一次解析后的真实路径仍在报告目录内（防符号链接绕过）
    base = os.path.abspath(report_dir())
    real = os.path.abspath(target)
    if os.path.commonpath([base, real]) != base or not os.path.isfile(real):
        raise FileNotFoundError(name)
    return Path(real)
