"""路由汇总。

main.py 只 include 这一个 router，新增接口时不用再改 main.py。
"""
from fastapi import APIRouter

from . import auth, chat, files, health, products

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(chat.router)
api_router.include_router(products.router)
api_router.include_router(files.router)

__all__ = ["api_router"]
