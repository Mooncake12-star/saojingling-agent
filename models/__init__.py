"""模型统一导出。

Alembic 的 autogenerate 只会看到「被 import 过」的模型，
少一行导出就会生成空迁移，而且不报错。新增模型务必在此登记。
"""
from models.conversation import Conversation, ConversationSummary
from models.product import Product
from models.user import LEGACY_USER_EMAIL, LEGACY_USER_ID, ROLE_ADMIN, ROLE_USER, User

__all__ = [
    "Conversation",
    "ConversationSummary",
    "Product",
    "User",
    "LEGACY_USER_ID",
    "LEGACY_USER_EMAIL",
    "ROLE_USER",
    "ROLE_ADMIN",
]
