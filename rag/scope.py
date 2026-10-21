"""请求级的检索范围（哪个用户、哪个知识库）。

为什么用 ContextVar 而不是给工具加参数：
    `rag_summarize` 是给大模型调用的工具，参数由模型生成。
    如果让模型自己填 knowledge_base_id，就等于把「查哪个租户的数据」
    交给一个可能被提示注入影响的组件决定——这不是隔离，是碰运气。
    用户身份只能来自 token，范围只能由服务端在请求入口处设定。

ContextVar 而不是全局变量：
    全局变量在并发请求下会被覆盖（A 的请求设成 kb=3，B 的请求进来设成 kb=5，
    A 的工具调用就会查到 B 的库）。ContextVar 是「每个执行上下文一份」，
    协程之间天然隔离。

它能穿过 asyncio.to_thread：
    工具实现是同步阻塞的，会被丢进线程池执行；asyncio.to_thread 会把
    当前 context 一起复制过去，所以线程里读到的仍是发起请求时的范围。
    这一点是本方案能成立的前提，改动工具调用方式时要留意。

范围表达式（Chroma where）有两条规则：
    1. 指定了知识库 → 必须同时匹配 user_id 和 knowledge_base_id（双保险）
    2. 没指定 → 只能看「自己的 chunk」加上「公共种子文档」（data/ 下内置资料）
       绝不能因为「没指定」就变成全库检索——那是一个静默的越权默认值
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator

# 内置资料（data/ 下那几份 txt/pdf）在入库时打的标记。
# 它们不属于任何用户，但所有人都可以检索到——这是产品决策，不是实现细节。
PUBLIC_META_KEY = "is_public"


@dataclass(frozen=True)
class RagScope:
    """一次检索允许看到的数据范围。"""

    user_id: int
    knowledge_base_id: int | None = None

    def to_where(self) -> dict:
        """翻译成 Chroma 的 where 表达式。"""
        if self.knowledge_base_id is not None:
            return {
                "$and": [
                    {"user_id": self.user_id},
                    {"knowledge_base_id": self.knowledge_base_id},
                ]
            }
        return {"$or": [{"user_id": self.user_id}, {PUBLIC_META_KEY: True}]}

    @property
    def cache_key(self) -> tuple[int, int | None]:
        """给「每个范围一份 RAG 服务」的缓存用。"""
        return (self.user_id, self.knowledge_base_id)


_scope: ContextVar[RagScope | None] = ContextVar("rag_scope", default=None)


def current_scope() -> RagScope | None:
    """当前请求的范围。None 表示不在请求上下文里（脚本、Streamlit 旧路径）。"""
    return _scope.get()


@contextmanager
def use_scope(user_id: int, knowledge_base_id: int | None = None) -> Iterator[RagScope]:
    """在 with 块内把范围限定到某个用户（可选某个知识库）。

    用 token + reset 而不是「记下旧值再赋回去」：
        reset 能正确还原嵌套调用和异常退出时的状态，
        手写赋值在 finally 里漏一次，范围就会泄漏到下一个请求——
        泄漏的后果是 A 的检索看到 B 的数据，而且不会报错。
    """
    scope = RagScope(user_id=user_id, knowledge_base_id=knowledge_base_id)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
