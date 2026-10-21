"""
RAG 服务
改进点：
  1. BGE Reranker 替代 LLM 重排序（更稳定、更快、更便宜）
  2. 上下文压缩（阈值过滤 + 长度裁切，降低 token 消耗）

安装额外依赖（三选一）：
  pip install sentence-transformers    # BGE Reranker（推荐，最成熟）
  pip install flashrank                # 轻量级 ONNX reranker（无需 PyTorch）
  pip install flagembedding            # FlagEmbedding 官方库

依赖已安装，会自动使用 BGE Reranker；
未安装则降级为 LLM 重排序（原版逻辑），不报错。
"""

import os
import re
#先看环境变量有没有设置HF_ENDPOINT，如果是空就去原官网，如果有内容，就按设置的内容去找是这个意思吗
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
from utils.path_tool import ensure_project_root_in_syspath
ensure_project_root_in_syspath()

from typing import List, AsyncGenerator
from langchain_core.documents import Document
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough, RunnableLambda
from langchain_core.prompts import PromptTemplate
from langchain_classic.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever


from rag.vector_store import VectorStoreService
from utils.prompt_loader import load_rag_prompts
from model.factory import get_chat_model
from utils.logger_handler import logger

QUERY_REWRITE_TEMPLATE = """你是检索专家，请根据用户的原始问题和对话历史，生成一个更清晰、更完整的搜索查询。

原始问题：{input}

要求：
- 补充缺失的上下文，使查询独立完整
- 提取核心关键词
- 直接输出改写后的查询，不要多余解释"""

# ──────────────────────────────────────────────
# 0. BM25 中文分词
# ──────────────────────────────────────────────
# BM25Retriever 默认的 preprocess_func 是 text.split()，按空格切词。
# 中文句子没有空格，整句会被当成一个 token → BM25 检索完全失效（实测
# 会使混合检索的 Hit@1 比纯向量低 12.5pp）。这里改用字符 bigram（相邻
# 两字成词），零依赖且对中文有效；若已安装 jieba，可换成
# preprocess_func=jieba.lcut，效果相当。
def _cn_preprocess(text: str) -> List[str]:
    text = re.sub(r"\s+", "", text)
    if len(text) < 2:
        ##长度不足2切不出bigram；空串返回 []（而非 [""]）避免产生空 token
        return [text] if text else []
    return [text[i:i + 2] for i in range(len(text) - 1)]


# ──────────────────────────────────────────────
# 1. 专业 Reranker — 交给 rag/reranker.py 管生命周期
# ──────────────────────────────────────────────
# 模型只加载一份、推理限并发、忙时降级，这些都在 reranker 模块里做完了。
# 这里不再保留任何模型状态：本文件会被多个 scope 的实例共享，
# 把状态放在模块变量里，等于让「缓存了几个 scope」决定「模型被加载几次」。
from rag.reranker import (  # noqa: E402
    STATE_DISABLED,
    STATE_FAILED,
    STATE_READY,
    reranker,
)


class RagSummarizeService:
    def __init__(
        self,
        keyword_top_k: int = 10,
        final_top_k: int = 6,
        where: dict | None = None,
    ):
        """where 限定「这次检索能看到哪些 chunk」（Chroma 过滤表达式）。

        为什么范围要在构造时定死，而不是每次 invoke 时传：
            混合检索里的 BM25 是在构造阶段用候选集合建索引的，
            索引一旦建好就固定了。如果只在查询时过滤向量那一路，
            BM25 那一路仍会从别人的文档里召回——两路各查各的，
            混起来就是「一半守规矩、一半不守」。
            所以范围必须同时作用于「建索引」和「向量检索」，而这两件事
            都发生在构造阶段。不同范围用不同实例，由 rag/local_tool.py 缓存。
        """
        self.vector_store_svc = VectorStoreService()
        self.model = get_chat_model()
        self.keyword_top_k = keyword_top_k
        self.final_top_k = final_top_k
        self.where = where
        self.prompt_text = load_rag_prompts()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)

        self._init_ensemble_retriever()
        self.chain = self._build_chain()


    #给文档打分，然后从高到低按top_k拿文档
    def _init_ensemble_retriever(self):
        """初始化集成检索器：向量检索 + BM25 关键词检索（限定在 self.where 范围内）"""
        vector_retriever = self.vector_store_svc.get_retriever(self.where)

        docs = self.vector_store_svc.get_documents(self.where)

        if not docs:
            logger.warning(f"[检索器] 范围内无文档（where={self.where}），回退到纯向量检索")
            self.ensemble_retriever = vector_retriever
            return

        #把docs里的所有 Document 建立成一个 BM25 关键词检索器。
        #必须显式传 preprocess_func：默认的 text.split() 切不开中文，会让 BM25 失效。
        bm25_retriever = BM25Retriever.from_documents(docs, preprocess_func=_cn_preprocess)
        bm25_retriever.k = self.keyword_top_k
        #EnsembleRetriever 同时进行向量检索和 BM25 检索，根据两路结果的排名计算 RRF 分数，
        #再按照weights=[0.5,0.5]加权融合，重新排序后返回最终 Top 10 个 Chunk。形式是List[Document]
        self.ensemble_retriever = EnsembleRetriever(
            retrievers=[vector_retriever, bm25_retriever],
            weights=[0.5, 0.5],
            k=10
        )
        logger.info(
            f"[检索器] EnsembleRetriever 构建完成（向量+BM25），共 {len(docs)} 个文档参与构建集成检索器"
        )

    # ──────────────────────────────────────────────
    # 2. BGE Reranker — 比 LLM 打分更稳、更快
    # ─────────────────────────────────────────────—
    def _bge_rerank(self, query: str, docs: List[Document]):
        """BGE 精排。返回 (文档, 分数) 或 None。

        None 表示「这次没排成」——模型没就绪、推理并发已满、或推理报错。
        调用方应当保留 RRF 融合顺序继续走，而不是回头去调 LLM 重排：
        重排只是排序优化，为了它多花一次 3~5 秒的 LLM 调用不值得。
        """
        if not docs:
            return docs, []

        pairs = [[query, d.page_content[:512]] for d in docs]
        #用bge重排给配对的问题+文档chunk打分，BGE到底拿什么“算”出这个相关性分数
        #它不是计算两个句子的某个固定数学相似度，而是把 query 和 chunk 的 token 一起送进 Transformer，
        # 通过自注意力/交叉注意力让模型学习两段文本之间的关系，最后通过一个分类/打分层输出相关性分数。
        scores = reranker.score(pairs)
        if scores is None:
            return None
        #分数归一化
        min_s, max_s = min(scores), max(scores)
        if max_s - min_s > 1e-8:
            scores = [(s - min_s) / (max_s - min_s) for s in scores]
            #如果相等
        else:
            scores = [0.5] * len(scores)
        #sorted参数要求：1.iterable —— 要排序的数据，2.key —— 按什么规则排序，这里必须是一个函数
        #3.reverse —— 是否反向排序，最后sorted返回的是一个列表
        scored = sorted(zip(scores, docs), key=lambda x: x[0], reverse=True)

        logger.info(f"[BGE Reranker] 完成 {len(docs)} 个文档评分")
        all_scores = [s for s, _ in scored]
        return [d for _, d in scored][:self.final_top_k], all_scores[:self.final_top_k]

    # ──────────────────────────────────────────────
    # 3. LLM 重排序（降级方案）
    # ──────────────────────────────────────────────
    def _llm_rerank(self, query: str, docs: List[Document]) -> List[Document]:
        if not docs:
            return docs
        try:
            rerank_prompt = PromptTemplate.from_template(
                "用户问题：{query}\n\n"
                "参考文档：\n{docs_text}\n\n"
                "请从高到低给这些文档与问题的相关性打分（0-10分），"
                "输出格式：每行一个「分数 文档序号」，只输出分数和序号。"
            )
            #以下是生成器表达式类似列表推到式，从后往前赋值，join的返回值是字符串格式
            docs_text = "\n".join(
                f"[{i}] {d.page_content[:200]}"
                for i, d in enumerate(docs)
            )
            result = (rerank_prompt | self.model | StrOutputParser()).invoke(
                {"query": query, "docs_text": docs_text}
            )
            scored = []
            #split的返回值是列表
            for line in result.strip().split("\n"):
                parts = line.strip().split()
                if len(parts) >= 2:
                    try:
                        score = float(parts[0])
                        idx = int(parts[-1])
                        if 0 <= idx < len(docs):
                            scored.append((score, docs[idx]))
                    except (ValueError, IndexError):
                        pass
            if scored:
                scored.sort(key=lambda x: x[0], reverse=True)
                logger.info(f"[LLM 重排序] {len(scored)} 个文档完成评分")
                return [d for _, d in scored][:self.final_top_k]
            return docs[:self.final_top_k]
        except Exception as e:
            logger.warning(f"[LLM 重排序] 失败，保持原序: {e}")
            return docs[:self.final_top_k]

    # ──────────────────────────────────────────────
    # 4. 上下文压缩 — 按分数阈值过滤 + 截断过长内容
    # ──────────────────────────────────────────────
    SCORE_THRESHOLD = 0.3
    MAX_CHARS_PER_DOC = 800

    def _compress_context(self, docs: List[Document], scores: List[float] = None) -> List[Document]:
        compressed = []
        if scores is None:
            logger.debug("[上下文压缩] 无分数信息，仅做长度截断（预期行为：LLM 降级路径）")
        for i, doc in enumerate(docs):
            if scores and scores[i] < self.SCORE_THRESHOLD:
                continue

            content = doc.page_content[:self.MAX_CHARS_PER_DOC]
            compressed.append(Document(page_content=content, metadata=doc.metadata))
        if not compressed:
            return docs[:1]
        return compressed

    # ──────────────────────────────────────────────
    # 5. 执行重排序（自动选择方案）
    # ──────────────────────────────────────────────
    def _rerank(self, query: str, docs: List[Document]) -> List[Document]:
        """三档策略，按「当前这个进程此刻能承担多少」选择。

        这三档的区别不是精确度，而是**代价**：
            READY    → BGE 精排（约 100~300ms，限并发、忙则被闸门拦下）
            DISABLED/FAILED → LLM 重排（一次额外生成调用，3~5 秒，但至少排了）
            其它（加载中/未开始）→ 直接用 RRF 顺序，一次额外调用都不花

        第三档是压测里最容易被忽略的一档：模型还在加载的那 40~60 秒里，
        如果每个请求都退到 LLM 重排，模型加载本身还没完成，
        LLM 配额先被重排请求打满（DashScope 侧直接 429），
        于是「等模型」变成「全线 429」。用 RRF 顺序顶这一段，
        用户拿到的是「排序略粗但立刻返回」的答案。
        """
        state = reranker.state
        if state == STATE_READY:
            result = self._bge_rerank(query, docs)
            if result is not None:
                reranked, scores = result
                return self._compress_context(reranked, scores)
            logger.info(
                "[重排] 精排不可用（并发已满或推理失败），保留 RRF 顺序",
                extra={"context": {"event": "rerank.degrade", "reason": "busy_or_error"}},
            )
            return self._compress_context(docs[:self.final_top_k])

        if state in (STATE_DISABLED, STATE_FAILED):
            return self._compress_context(self._llm_rerank(query, docs))

        # STATE_IDLE / STATE_LOADING
        logger.info(
            "[重排] 模型尚未就绪，本次使用 RRF 顺序",
            extra={"context": {"event": "rerank.degrade", "reason": state}},
        )
        return self._compress_context(docs[:self.final_top_k])

    # ──────────────────────────────────────────────
    # 6. 构建最终上下文
    # ──────────────────────────────────────────────
    def _build_context(self, docs: List[Document]) -> str:
        context = ""
        for i, doc in enumerate(docs):
            source = doc.metadata.get("source", doc.metadata.get("file_path", "未知来源"))
            context += (
                f"【参考资料{i + 1}】\n"
                f"内容：{doc.page_content}\n"
                f"来源：{source}\n"
                f"相关度排名：{i + 1}\n\n"
            )
        return context

    # ──────────────────────────────────────────────
    # 7. 构建管道
    # ──────────────────────────────────────────────
    def _build_chain(self):
        def safe_rewrite(inputs):
            try:
                chain = (
                    PromptTemplate.from_template(QUERY_REWRITE_TEMPLATE)
                    | self.model
                    | StrOutputParser()
                )
                rewritten = chain.invoke({"input": inputs["input"]})
                rewritten = rewritten.strip().strip('"').strip("'")
                logger.info(f"[查询重写] 原始: {inputs['input']} → 改写: {rewritten}")
                return rewritten
            except Exception as e:
                logger.warning(f"[查询重写] 失败，使用原始查询: {e}")
                return inputs["input"]

        def retrieve_and_rerank(inputs):
            query = inputs["input"]
            rewritten = inputs["rewritten_query"]
            docs = self.ensemble_retriever.invoke(rewritten)
            logger.info(f"[集成检索] 向量+BM25 共 {len(docs)} 条")

            reranked = self._rerank(query, docs)
            context = self._build_context(reranked)
            return {"input": query, "context": context}

        generate_chain = self.prompt_template | self.model | StrOutputParser()
        #RunnableLambda() 就是一个“包装器”，把普通 Python 函数包装成 LangChain 的 Runnable 对象。
        # RunnablePassthrough.assign保留旧的input并在此基础上添加新的指定字段内容
        return (
            RunnablePassthrough.assign(rewritten_query=RunnableLambda(safe_rewrite))
            | RunnableLambda(retrieve_and_rerank)
            | generate_chain
        )
    #同步调用
    def rag_summarize(self, query: str) -> str:
        return self.chain.invoke({"input": query})
    #异步调用
    async def astream_rag_summarize(self, query: str) -> AsyncGenerator[str, None]:
        async for chunk in self.chain.astream({"input": query}):
            yield chunk


if __name__ == '__main__':
    vs = VectorStoreService()
    vs.load_document()

    rag = RagSummarizeService()
    print("=== 企业级 RAG 测试 ===")
    result = rag.rag_summarize("大户型适合哪些扫地机器人")
    print(result)
