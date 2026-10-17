# -*- coding: utf-8 -*-
"""
检索效果评测 —— 一个文件、一条命令、几个数字。

目的：用最少的代码，验证「混合检索」「BGE 重排」「查询改写」「端到端生成」
      到底有没有用，跑出来的数字可以直接写进简历 / README。

评测集：data/ 下两个「100问」文件里现成的 (问题, 答案) 对，随机抽样（seed 固定，可复现）；
        另有一批手写的「口语化难例」（HARD_CASES），用于暴露检索层的真实难度。
命中判定：标准答案中的关键短语（≥5 字的连续中文片段）出现在检索结果中即算命中。

对照配置：
    A 纯向量检索      B 向量 + BM25（RRF 融合）      C 混合检索 + BGE 重排
    D 端到端生成（仅 --e2e 时跑，真调 LLM 出答案，看答案里有没有关键短语）

两个开关：
    --e2e         多跑 D 档（慢，每条多一次 LLM 调用）
    --no-rewrite  跳过查询改写，用原始 query 直接检索（用于量化改写增益）
不加开关时默认：查询改写=开，D 档=关。

用法（在项目根目录执行）：

# 只在难例上跑（15 条，约 3.5 分钟）—— 看重排价值
python tests/eval_quick.py hard

# 只在原句上跑（40 条，约 9 分钟）—— 看混合检索价值
python tests/eval_quick.py easy

# 两者都跑（55 条）—— 会分开报两个子集的结果
python tests/eval_quick.py 40 all

# 加 --e2e：额外跑 D 档（端到端，真调 LLM 生成答案），报告里多看一个「答案命中率」
python tests/eval_quick.py hard --e2e

# 加 --no-rewrite：跳过查询改写，用原始 query 直接检索
python tests/eval_quick.py hard --no-rewrite

# 两个开关可以叠加
python tests/eval_quick.py hard --e2e --no-rewrite
"""

import os
import re
import sys
import glob
import time
import random
import logging

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 脚本位于 tests/ 下，默认看不到项目根目录；先手动补进去，才能 import utils / rag
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from utils.path_tool import ensure_project_root_in_syspath

ensure_project_root_in_syspath()

from rag.rag_service import RagSummarizeService, _get_bge_reranker
from rag.vector_store import VectorStoreService
from utils.logger_handler import logger

TOP_K = 10   # 三种配置统一取 top 10 候选，再分别统计 Hit@1 / @3 / @5
SEED = 42

Q_RE = re.compile(r"^\s*\d+\s*[.、]\s*\*\*(.+?)\*\*\s*$")
A_RE = re.compile(r"^\s*[-•]\s*(\S.*)$")
SPLIT_RE = re.compile(r"[，。；、,.;!！?？：:（）()「」【】\s]+")


def key_phrases(answer):
    """把标准答案拆成若干可匹配的关键短语。"""
    cand = [s for s in SPLIT_RE.split(answer) if len(s) >= 5]
    if not cand:
        cand = [re.sub(r"\W", "", answer)]
    return cand


# ──────────────────────────────────────────────
# 口语化难例（Hard Cases）
# ──────────────────────────────────────────────
# 为什么需要：原句评测里，query 直接取自知识库原文，中文 bigram 几乎逐字命中，
# 检索层直接饱和（Hit@3 = 100%），导致 BGE 重排「没有提升空间」、增益归零。
#
# 难例的设计原则（三条，缺一不可）：
#   1) 口语化表述 —— query 不含知识库原文的关键词，模拟真实用户提问
#      例：问「滚刷缠头发」而不是原文的「机器人滚刷缠绕毛发怎么处理」
#   2) 同义/别名替换 —— 用日常说法替换专业术语
#      例：用「吸不干净」代替「吸力明显下降」；用「有味儿」代替「异味」
#   3) 多候选干扰 —— 让 top-k 里出现多个"看起来都相关"的文档，
#      这样只有交叉编码器（BGE）才有区分空间，重排价值才能显出来
#
# 每条格式：(口语化 query, 期望命中的关键短语, 来源说明)
# 关键短语必须是知识库里**真实存在**的原文片段，否则判定会失效。
HARD_CASES = [
    # —— 场景/口语化 ——
    ("家里养猫，地上到处是猫毛，机器老是缠住不动",
     ["防缠绕主刷", "毛发清理模式"], "扫地机器人100问2.txt:宠物家庭优化"),
    ("屋里总有股馊味儿，尘盒倒干净了还是有",
     ["滋生细菌", "更换发霉滤网"], "故障排除.txt:73 尘盒异味"),
    ("滚刷缠满了头发转不动，怎么弄下来",
     ["剪断主刷缠绕毛发", "剪断缠绕物"], "故障排除.txt:7 / 维护保养.txt"),
    ("我家 140 平还有两只狗，该买什么吸力的",
     ["3000Pa", "4000Pa"], "选购指南.txt:吸力参数"),
    ("拖完地一股霉味，抹布黑黑的",
     ["中性洗涤剂浸泡", "高温消毒"], "扫拖一体机器人100问.txt:16"),
    # —— 同义替换（不带原文关键词）——
    ("吸不干净了，怎么回事",
     ["剪断主刷缠绕毛发", "逐一清理堵塞部位"], "故障排除.txt:7 吸力下降"),
    ("它老是撞桌子腿，磕磕碰碰的",
     ["避障摄像头", "防撞条"], "故障排除.txt:5 频繁碰撞家具"),
    ("回不了窝，在客厅转圈找不着底座",
     ["清理充电座周围3米内障碍物", "回充传感器"], "故障排除.txt:10 找不到充电座"),
    ("拖布洗完了还是臭的，怎么办",
     ["中性洗涤剂", "避免发霉"], "扫拖一体机器人100问.txt:16"),
    ("一到地毯上就走不动了，卡那儿",
     ["清理主刷纤维", "地毯模式"], "故障排除.txt:44 地毯卡顿"),
    # —— 边界/多条件（考察语义理解而非关键词匹配）——
    ("复式楼两层都能扫吗，会不会掉下去",
     ["为每层楼单独建图", "手动将机器人搬至对应楼层"], "扫拖一体机器人100问.txt:70"),
    ("卫生间有水能不能进去拖",
     ["避免卫生间地面大量积水", "开启防跌落功能"], "扫地机器人100问2.txt:74"),
    ("门槛太高过不去，怎么办",
     ["门槛高度在机器人跨越范围内", "手动辅助机器人跨越"], "扫地机器人100问2.txt:61"),
    ("露天阳台能拖吗",
     ["不建议", "仅可在封闭阳台内使用"], "扫拖一体机器人100问.txt:34"),
    ("这个机器多久要换一次刷子",
     ["3-6个月更换一次", "1-3个月更换"], "扫地机器人100问2.txt:主刷更换周期"),
]


def build_eval_set():
    """从 data/ 的「100问」文件里解析出 (问题, 答案, 来源) 三元组。

    注意：路径锚定到「脚本所在项目的根目录」，而不是当前工作目录（CWD），
    这样在任意目录下执行本脚本都能找到 data/，不会因为 CWD 不同而扫不到文件。
    """
    pairs = []
    data_dir = os.path.join(_PROJECT_ROOT, "data")
    files = sorted(glob.glob(os.path.join(data_dir, "*100问*.txt")))
    if not files:
        print("[提示] 在 %s 下没找到 *100问*.txt 文件。" % data_dir)
        print("       当前工作目录：%s" % os.getcwd())
        print("       请确认 data/ 下存在「扫地机器人100问2.txt」「扫拖一体机器人100问.txt」。")
    for path in files:
        src = os.path.basename(path)
        cur_q = None
        for line in open(path, encoding="utf-8"):
            m = Q_RE.match(line)
            if m:
                cur_q = m.group(1).strip()
                continue
            m = A_RE.match(line)
            if m and cur_q:
                pairs.append((cur_q, m.group(1).strip(), src))
                cur_q = None
    return pairs



def key_phrases(answer):
    """把标准答案拆成若干可匹配的关键短语。"""
    cand = [s for s in SPLIT_RE.split(answer) if len(s) >= 5]
    if not cand:
        cand = [re.sub(r"\W", "", answer)]
    return cand


def hit(docs, phrases):
    """top-k 结果里只要有任一条命中任一关键短语，即算命中。"""
    return any(p in d.page_content for d in docs for p in phrases)


def _rewrite_query(rag, query):
    """调用 rag_service 里同款的查询改写链路（Prompt + 模型 + 抽掉引号）。

    这里是从外部复用 RagSummarizeService 已有的组件拼装，不改动 rag_service.py。
    失败时返回 None，调用方会退回原始 query —— 与源码里 safe_rewrite 的容错行为一致。
    """
    try:
        from langchain_core.prompts import PromptTemplate
        from langchain_core.output_parsers import StrOutputParser
        from rag.rag_service import QUERY_REWRITE_TEMPLATE

        chain = (
            PromptTemplate.from_template(QUERY_REWRITE_TEMPLATE)
            | rag.model
            | StrOutputParser()
        )
        out = chain.invoke({"input": query}).strip().strip('"').strip("'")
        return out or None
    except Exception:
        return None


def main():
    n = 40
    mode = "all"
    use_e2e = False       # 是否额外跑 D 档（端到端：真调 LLM 生成答案）
    use_rewrite = True    # 是否启用查询改写（默认开；--no-rewrite 关闭）
    for arg in sys.argv[1:]:
        if arg.isdigit():
            n = int(arg)
        elif arg in ("hard", "easy", "all"):
            mode = arg
        elif arg == "--e2e":
            use_e2e = True
        elif arg == "--no-rewrite":
            use_rewrite = False

    logger.setLevel(logging.WARNING)

    pairs = build_eval_set()
    if not pairs:
        print("[错误] 没解析到问答对，请确认 data/ 下的「100问」文件存在。")
        sys.exit(1)

    random.seed(SEED)
    sample = random.sample(pairs, min(n, len(pairs)))
    easy_set = [(q, key_phrases(a), src) for q, a, src in sample]
    hard_set = [(q, ph, src) for q, ph, src in HARD_CASES]

    if mode == "easy":
        hard_set = []
    elif mode == "hard":
        easy_set = []

    cases = [(q, ph, src, "原句") for q, ph, src in easy_set] + \
            [(q, ph, src, "难例") for q, ph, src in hard_set]

    print("[评测集] 原句候选 %d 条 → 抽样 %d 条 | 口语化难例 %d 条 | 模式=%s"
          % (len(pairs), len(easy_set), len(hard_set), mode))
    print("[配置] 查询改写=%s | 端到端D档=%s"
          % ("开" if use_rewrite else "关（--no-rewrite）",
             "开（--e2e）" if use_e2e else "关"))

    if not cases:
        print("[错误] 评测集为空。")
        sys.exit(1)

    # 关键前置检查：BGE 加载失败时 _bge_rerank 会「静默」降级成 LLM 重排，
    # 那样配置 C 实际测的是 LLM 重排，数字是假的。必须先卡住。
    if _get_bge_reranker() is None:
        print("[中止] BGE Reranker 不可用——配置 C 会静默降级为 LLM 重排，结论不可比。")
        print("       请确认 sentence-transformers 已安装且模型可正常加载。")
        sys.exit(1)

    t_init = time.time()
    rag = RagSummarizeService()
    vs = VectorStoreService()
    try:
        n_chunks = vs.vector_store._collection.count()
    except Exception:
        n_chunks = -1
    print("[就绪] 向量库 %s 个 chunk | 初始化耗时 %.1fs\n" % (n_chunks, time.time() - t_init))

    stats = {k: {1: 0, 3: 0, 5: 0} for k in "ABCD"}
    subset = {s: {k: {1: 0, 3: 0, 5: 0} for k in "ABCD"} for s in ("原句", "难例")}
    sub_n = {"原句": 0, "难例": 0}
    e2e_hit = {"原句": 0, "难例": 0}     # D 档：答案里命中关键短语的条数
    misses = []
    t0 = time.time()

    for i, (q, ph, src, kind) in enumerate(cases, 1):
        sub_n[kind] += 1

        # 检索用哪个 query，取决于是否启用改写（对齐 rag_service.py 里的链路）
        search_q = q
        if use_rewrite:
            rewritten = _rewrite_query(rag, q)
            if rewritten:
                search_q = rewritten

        # 带 backoff 的检索：高频调用会触发 embedding/LLM 接口限流（429），
        # 直接失败会浪费整轮评测；指数退避后重试，最多 4 次
        def retrieve_with_backoff(fn):
            for attempt in range(4):
                try:
                    return fn()
                except Exception as e:
                    if attempt == 3:
                        raise
                    wait_s = 15 * (2 ** attempt)
                    print("    [限流退避] %ds 后重试（第 %d 次）: %s"
                          % (wait_s, attempt + 1, type(e).__name__))
                    time.sleep(wait_s)

        vec_docs = retrieve_with_backoff(
            lambda: vs.vector_store.similarity_search(search_q, k=TOP_K))  # A 纯向量
        hyb_docs = retrieve_with_backoff(
            lambda: rag.ensemble_retriever.invoke(search_q))               # B 向量 + BM25
        bge_docs, _ = rag._bge_rerank(q, hyb_docs)                         # C 混合 + BGE 重排

        for key, docs in (("A", vec_docs), ("B", hyb_docs), ("C", bge_docs)):
            for k in (1, 3, 5):
                if hit(docs[:k], ph):
                    stats[key][k] += 1
                    subset[kind][key][k] += 1

        # D 档（--e2e）：真跑一次生成，看关键短语有没有出现在最终答案里
        if use_e2e:
            try:
                answer = rag.rag_summarize(q)
            except Exception as e:
                answer = ""
                print("    [E2E 异常] %s → %s" % (q, e))
            if any(p in (answer or "") for p in ph):
                e2e_hit[kind] += 1

        # 记录「重排把正确答案救回第一位」的样本 —— 这是重排价值的最直观证据
        if kind == "难例" and not hit(hyb_docs[:1], ph) and hit(bge_docs[:1], ph):
            misses.append((q, "BGE 救回", src))
        elif kind == "难例" and not hit(bge_docs[:5], ph):
            misses.append((q, "未命中", src))

        if i % 5 == 0 or i == len(cases):
            print("  进度 %d/%d  用时 %.0fs" % (i, len(cases), time.time() - t0))

    n_s = len(cases)
    fmt = lambda v, d: "%.1f%%" % (v / d * 100) if d else "  -  "

    print("\n" + "=" * 58)
    print("【全量 N=%d】" % n_s)
    print("A 纯向量检索        Hit@1 %-8s Hit@3 %-8s Hit@5 %s"
          % (fmt(stats["A"][1], n_s), fmt(stats["A"][3], n_s), fmt(stats["A"][5], n_s)))
    print("B 向量+BM25 (RRF)   Hit@1 %-8s Hit@3 %-8s Hit@5 %s"
          % (fmt(stats["B"][1], n_s), fmt(stats["B"][3], n_s), fmt(stats["B"][5], n_s)))
    print("C 混合 + BGE 重排    Hit@1 %-8s Hit@3 %-8s Hit@5 %s"
          % (fmt(stats["C"][1], n_s), fmt(stats["C"][3], n_s), fmt(stats["C"][5], n_s)))
    if use_e2e:
        tot_hit = e2e_hit["原句"] + e2e_hit["难例"]
        print("D 端到端生成答案    答案命中率 %s   ← 真调 LLM，不是文档命中"
              % fmt(tot_hit, n_s))

    for kind in ("原句", "难例"):
        d = sub_n[kind]
        if not d:
            continue
        print("\n【%s N=%d】" % (kind, d))
        for key, name in (("A", "纯向量      "), ("B", "混合(RRF)   "),
                          ("C", "混合+BGE重排"), ):
            print("  %s Hit@1 %-8s Hit@3 %-8s Hit@5 %s"
                  % (name, fmt(subset[kind][key][1], d),
                     fmt(subset[kind][key][3], d), fmt(subset[kind][key][5], d)))
        if use_e2e:
            print("  端到端生成 答案命中率 %s" % fmt(e2e_hit[kind], d))
    print("=" * 58)
    print("总耗时 %.0fs\n" % (time.time() - t0))

    def pct(d, key, metric):
        return subset[d][key][metric] / sub_n[d] * 100 if sub_n[d] else 0.0

    # 难例上的增益最有说服力：它才是重排真正发挥作用的地方
    if sub_n["难例"]:
        hb1, hc1 = pct("难例", "B", 1), pct("难例", "C", 1)
        hb3, hc3 = pct("难例", "B", 3), pct("难例", "C", 3)
        print("【难例子集上的增益】——重排价值的真实体现")
        print("  混合检索 vs 纯向量   Hit@1 %+.1fpp   Hit@3 %+.1fpp"
              % (pct("难例", "B", 1) - pct("难例", "A", 1),
                 pct("难例", "B", 3) - pct("难例", "A", 3)))
        print("  BGE 重排 vs 混合检索 Hit@1 %+.1fpp   Hit@3 %+.1fpp" % (hc1 - hb1, hc3 - hb3))

    if sub_n["原句"]:
        eb1, ec1 = pct("原句", "B", 1), pct("原句", "C", 1)
        print("\n【原句子集】——检索层已饱和，重排无发挥空间")
        print("  混合检索 Hit@1 %.0f%% | BGE 重排 Hit@1 %.0f%%（增益 %+.1fpp）"
              % (eb1, ec1, ec1 - eb1))

    if misses:
        print("\n【难例逐条明细】")
        for q, tag, src in misses:
            print("  [%s] %s  (%s)" % (tag, q, src))

    # 端到端 vs 检索层的差距：说明「上下文给对了但 LLM 没用好」
    if use_e2e:
        print("\n【检索层 vs 端到端】——两者差距说明生成环节的损耗")
        for kind in ("原句", "难例"):
            d = sub_n[kind]
            if not d:
                continue
            c1 = pct(kind, "C", 1)
            e = e2e_hit[kind] / d * 100
            print("  %s：检索 Hit@1 %.0f%% → 答案命中 %.0f%%（差 %+.1fpp）"
                  % (kind, c1, e, e - c1))
        if not use_rewrite:
            print("  注：本轮 --no-rewrite 已关闭查询改写，检索输入为原始 query。")

    # 查询改写消融提示（脚本本身不做两次运行，需用户自行对比）
    if not use_rewrite:
        print("\n【改写消融】本轮已关闭查询改写（--no-rewrite）。")
        print("  对比方法：同一模式再跑一次不加该参数，两侧数字之差即改写增益。")
        print("  例：python tests/eval_quick.py hard            # 有改写")
        print("      python tests/eval_quick.py hard --no-rewrite  # 无改写")

    print("\n可写进简历的句子（用难例子集，重排增益才有说服力）：")
    if sub_n["难例"]:
        print("  构建 %d 条口语化难例评测集（query 不含知识库原文关键词）："
              % sub_n["难例"])
        print("  混合检索 + BGE 重排将 Hit@1 从 %.0f%% 提升至 %.0f%%。"
              % (pct("难例", "B", 1), pct("难例", "C", 1)))
    else:
        print("  （当前为原句模式，检索层饱和，重排增益不显著——请加 hard 参数跑难例）")



if __name__ == "__main__":
    main()
