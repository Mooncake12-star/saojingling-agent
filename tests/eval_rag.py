import os, sys
from utils.path_tool import ensure_project_root_in_syspath
ensure_project_root_in_syspath()

from rag.rag_service import RagSummarizeService
from model.factory import get_chat_model
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import StrOutputParser

TEST_SET = [
    ("大户型适合什么扫地机器人",
     "建议选择电池容量大于5200mAh、激光导航、吸力3000Pa以上的扫拖一体机"),
    ("扫地机器人滚刷缠绕头发怎么处理",
     "关机后拆解滚刷仓，用剪刀清理缠绕的毛发，定期清理可减少缠绕"),
    ("扫地机器人多久保养一次",
     "每次使用后清空尘盒，每周拆洗滚刷和边刷，每月检查拖地模组"),
    ("扫地机器人和吸尘器哪个好",
     "扫地机器人适合日常自动维护清洁，吸尘器适合深度清洁和多场景灵活使用"),
    ("宠物家庭适合什么扫地机器人",
     "建议选择吸力5000Pa以上、防缠绕设计、支持宠物模式的机型"),
    ("扫地机器人水箱不出水怎么办",
     "检查水箱水量、出水口是否堵塞，可用牙签疏通，检查拖布是否过脏"),
]

def evaluate_with_reference(question: str, predicted: str, reference: str) -> dict:
    prompt = PromptTemplate.from_template(
        "对比标准答案和模型回答，按以下维度评分（0或1）：\n"
        "问题：{question}\n"
        "标准答案：{reference}\n"
        "模型回答：{predicted}\n\n"
        "1. 事实正确：模型回答是否包含与标准答案一致的关键信息\n"
        "2. 无幻觉：模型回答是否包含了标准答案中没有的错误信息\n"
        "3. 完整度：模型回答是否涵盖了标准答案的大部分要点\n\n"
        "输出格式：\n"
        "事实正确: 0/1\n"
        "无幻觉: 0/1\n"
        "完整度: 0/1"
    )
    result = (prompt | get_chat_model() | StrOutputParser()).invoke({
        "question": question,
        "reference": reference,
        "predicted": predicted
    })
    scores = {}
    for line in result.strip().split("\n"):
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip()
            try:
                scores[key] = int(val.strip())
            except ValueError:
                pass
    return scores

def run_evaluation():
    rag = RagSummarizeService()
    all_scores = {"事实正确": [], "无幻觉": [], "完整度": []}

    print(f"{'问题':<38} {'事实正确':<8} {'无幻觉':<8} {'完整度':<8} {'耗时':<8}")
    print("-" * 78)

    for question, reference in TEST_SET:
        import time
        start = time.time()
        predicted = rag.rag_summarize(question)
        elapsed = time.time() - start
        scores = evaluate_with_reference(question, predicted, reference)
        for k in all_scores:
            all_scores[k].append(scores.get(k, 0))
        print(f"{question[:36]:<38} {scores.get('事实正确', '-'):<8} "
              f"{scores.get('无幻觉', '-'):<8} {scores.get('完整度', '-'):<8} {elapsed:<8.1f}s")

    print(f"\n{'='*78}")
    print(f"{'平均':<38} ", end="")
    for k in all_scores:
        avg = sum(all_scores[k]) / len(all_scores[k])
        print(f"{avg:.0%}       ", end="")
    print(f"\n总分: {sum(all_scores['事实正确']) + sum(all_scores['无幻觉']) + sum(all_scores['完整度'])}/18")

if __name__ == "__main__":
    run_evaluation()
