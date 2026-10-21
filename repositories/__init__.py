"""数据访问层。

约定：repository 只负责「把 SQL 写成方法」，不做业务判断，不提交事务。
业务规则（打分、排序、文案）留在 service 或调用方，事务边界由 get_db / session_scope 决定。
"""
from repositories.conversation import ConversationRepository
from repositories.product import ProductRepository

__all__ = ["ConversationRepository", "ProductRepository"]
