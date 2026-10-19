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
# 1. 专业 Reranker — 惰性加载，首次调用时才下载模型
# ──────────────────────────────────────────────
RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"

_RERANKER_INSTANCE = None
# None = 还没探测过；True/False = 探测结果。
# 注意不要在这里 import sentence_transformers：它会把 torch + transformers
# 一起拖进来（实测约 38 秒、占用 1.5GB+ 内存）。任何"只是想用向量检索"的
# 进程（例如 MCP 子进程）不应该为它付这个代价。
_HAS_BGE_RERANKER = None


def _local_snapshot_dir(repo_id: str):
    """在 HuggingFace 缓存里找已下载的模型快照目录，找不到返回 None。

    为什么要这么绕：sentence-transformers 5.x 即使传 local_files_only=True，
    仍会去校验仓库元数据（实测 hf-mirror 返回 502 时整个加载直接失败）。
    直接把本地快照目录当"路径"传进去，hf 侧就不会发起任何网络请求——
    断网、镜像站抖动都不影响。
    """
    hf_home = os.getenv("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    snapshots = os.path.join(hf_home, "hub", "models--" + repo_id.replace("/", "--"), "snapshots")
    if not os.path.isdir(snapshots):
        return None
    candidates = [
        os.path.join(snapshots, name)
        for name in os.listdir(snapshots)
        if os.path.isfile(os.path.join(snapshots, name, "config.json"))
    ]
    if not candidates:
        return None
    return max(candidates, key=os.path.getmtime)


def _get_bge_reranker():
    global _RERANKER_INSTANCE
    global _HAS_BGE_RERANKER
    if _RERANKER_INSTANCE is not None:
        return _RERANKER_INSTANCE
    if _HAS_BGE_RERANKER is False:
        return None

    try:
        #CrossEncoder加载模型的接口，先访问本地如果本地没有去huggingface下载
        #访问的本地文件路径C:\Users\ADMIN\.cache\huggingface\hub
        from sentence_transformers import CrossEncoder
    except ImportError:
        _HAS_BGE_RERANKER = False
        logger.warning("[Reranker] sentence-transformers 未安装，降级到 LLM 重排序")
        return None

    _HAS_BGE_RERANKER = True
    try:
        # 先走本地快照（完全不联网），没有缓存再按仓库名联网下载
        local_dir = _local_snapshot_dir(RERANKER_MODEL_NAME)
        if local_dir:
            _RERANKER_INSTANCE = CrossEncoder(local_dir)
            logger.info(f"[Reranker] 已从本地缓存加载模型: {local_dir}")
        else:
            logger.info("[Reranker] 本地无缓存，开始在线下载")
            _RERANKER_INSTANCE = CrossEncoder(RERANKER_MODEL_NAME)
            logger.info(f"[Reranker] 已加载模型: {RERANKER_MODEL_NAME}")
        return _RERANKER_INSTANCE
    except Exception as e:
        logger.warning(f"[Reranker] 模型加载失败，降级到 LLM 重排序: {e}")
        _HAS_BGE_RERANKER = False
        return None


class RagSummarizeService:
    def __init__(self, keyword_top_k: int = 10, final_top_k: int = 6):
        self.vector_store_svc = VectorStoreService()
        self.model = get_chat_model()
        self.keyword_top_k = keyword_top_k
        self.final_top_k = final_top_k
        self.prompt_text = load_rag_prompts()
        self.prompt_template = PromptTemplate.from_template(self.prompt_text)

        self._init_ensemble_retriever()
        self.chain = self._build_chain()


    #给文档打分，然后从高到低按top_k拿文档
    def _init_ensemble_retriever(self):
        """初始化集成检索器：向量检索 + BM25 关键词检索"""
        vector_retriever = self.vector_store_svc.get_retriever()
        raw_chroma = self.vector_store_svc.vector_store

        all_data = raw_chroma.get(include=["documents", "metadatas"])
        #get("documents", [])如果 all_data 里面不存在 "documents" 这个 key，就返回空列表 []，而不是直接报 KeyError。
        texts = all_data.get("documents", [])
        metadatas = all_data.get("metadatas", [])

        if not texts:
            logger.warning("[检索器] 向量库中无文档，回退到纯向量检索")
            self.ensemble_retriever = vector_retriever
            return

        docs = [
            Document(page_content=t, metadata=m or {})
            for t, m in zip(texts, metadatas or [{}] * len(texts))
        ]
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
        logger.info(f"[检索器] EnsembleRetriever 构建完成（向量+BM25），共 {len(docs)} 个文档参与构建集成检索器")

    # ──────────────────────────────────────────────
    # 2. BGE Reranker — 比 LLM 打分更稳、更快
    # ─────────────────────────────────────────────—
    def _bge_rerank(self, query: str, docs: List[Document]):
        if not docs:
            return docs, []

        reranker_model = _get_bge_reranker()
        if reranker_model is None:
            return self._llm_rerank(query, docs), []
        pairs = [[query, d.page_content[:512]] for d in docs]
        #用bge重排给配对的问题+文档chunk打分，BGE到底拿什么“算”出这个相关性分数
        #它不是计算两个句子的某个固定数学相似度，而是把 query 和 chunk 的 token 一起送进 Transformer，
        # 通过自注意力/交叉注意力让模型学习两段文本之间的关系，最后通过一个分类/打分层输出相关性分数。
        scores = reranker_model.predict(pairs)
        scores = [float(s) for s in scores]
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
        if _get_bge_reranker() is not None:
            reranked, scores = self._bge_rerank(query, docs)
            compressed = self._compress_context(reranked, scores)
            return compressed
        else:
            llm_reranked = self._llm_rerank(query, docs)
            return self._compress_context(llm_reranked)

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
