"""Agent 行为评测 runner。

跑法（在项目根目录执行）：
    python tests/eval/run_agent_eval.py --limit 3 --no-judge    # 冒烟，验证管道
    python tests/eval/run_agent_eval.py                         # 全量（含 LLM 判答案）

产出：
    reports/agent_eval_<时间戳>.md   人看的报表
    tests/eval/baseline.json         机器读的基线（改 prompt 后对比退化）

设计要点：
1. **不改业务代码**。工具调用轨迹从 monitor_tool 的日志里抓（它本来就打了
   "执行工具：xxx / 传入参数：{...}"），评测脚本挂一个 logging.Handler 收走。
   代价是依赖日志文案，文案改了要么改正则、要么把 middleware 换成结构化回调。
2. **每条用例独立 session**，避免上一条的工具结果污染下一条的上下文。
3. **判分三层**：工具集合是否精确匹配 / 首个工具是否选对 / 答案要点是否覆盖。
   前两层是客观比对，第三层才用 LLM 当裁判。
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import csv
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# 项目根入 sys.path，保证从任意目录执行都能 import
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

EVAL_DIR = Path(__file__).resolve().parent
CSV_PATH = EVAL_DIR / "agent_eval_set.csv"
BASELINE_PATH = EVAL_DIR / "baseline.json"
CHECKPOINT_PATH = EVAL_DIR / ".checkpoint.jsonl"   # 断点续跑：每跑完一条立即落盘
REPORT_DIR = ROOT / "reports"

# 单条用例的硬超时（秒）。Agent 正常 40~60s，超过这个数说明卡住了
# （实测过一次：模型 API 请求无响应，整批 51 条卡在第 42 条 77 分钟不动）
DEFAULT_CASE_TIMEOUT = 240

# 图片类用例需要真实文件路径，否则 image_analyze 只会回"文件不存在"
IMAGE_SAMPLE = ROOT / "uploads" / "img_test.jpg"

# monitor_tool 的两条日志文案（改动 middleware 时这里要同步）
RE_TOOL = re.compile(r"\[tool monitor\]执行工具：(\S+)")
RE_ARGS = re.compile(r"\[tool monitor\]传入参数：(.*)$")


# ──────────────────────────────────────────────────────────
# 工具调用捕获
# ──────────────────────────────────────────────────────────
class ToolCallRecorder(logging.Handler):
    """挂在 "agent" logger 上，把 monitor_tool 打的工具名与参数收进列表。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.calls: List[Dict[str, Any]] = []
        self._pending_name: Optional[str] = None

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return

        m = RE_TOOL.search(msg)
        if m:
            self._pending_name = m.group(1)
            return

        m = RE_ARGS.search(msg)
        if m and self._pending_name:
            raw = m.group(1).strip()
            try:
                # 日志里是 Python dict 的 repr（单引号），用 literal_eval 还原
                args = ast.literal_eval(raw)
                if not isinstance(args, dict):
                    args = {"_raw": raw}
            except (ValueError, SyntaxError):
                args = {"_raw": raw}
            self.calls.append({"name": self._pending_name, "args": args})
            self._pending_name = None

    def reset(self) -> None:
        self.calls = []
        self._pending_name = None


@dataclass
class CaseResult:
    cid: str
    category: str
    query: str
    expected_tools: List[str]
    actual_tools: List[str]
    actual_args: List[Dict[str, Any]]
    answer: str
    elapsed: float
    tools_exact: bool = False
    tools_top1: bool = False
    tools_recall: bool = True
    args_ok: bool = True
    judge_score: Optional[int] = None
    judge_pass: Optional[bool] = None
    error: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)


# ──────────────────────────────────────────────────────────
# 判分
# ──────────────────────────────────────────────────────────
def split_multi(value: str) -> List[str]:
    """CSV 里用 | 分隔多项。"""
    return [p.strip() for p in (value or "").split("|") if p.strip()]


def check_args(expected_args: str, actual_calls: List[Dict[str, Any]]) -> bool:
    """期望参数形如 `product_name=智扫 X3 Ultra|budget=3000`。

    只要有一次工具调用里存在对应 key、且值包含期望字符串，就算命中。
    这是宽松匹配——不做类型强校验，因为我们评的是"参数抽取方向对不对"，
    不是"数值是否一分不差"。
    """
    pairs = split_multi(expected_args)
    if not pairs:
        return True

    normalized = []
    for call in actual_calls:
        flat = {}
        for k, v in (call.get("args") or {}).items():
            flat[k.lower()] = str(v).replace(" ", "").lower()
        normalized.append(flat)

    for pair in pairs:
        if "=" not in pair:
            continue
        key, want = pair.split("=", 1)
        key = key.strip().lower()
        want_norm = want.replace(" ", "").lower()
        hit = any(
            key in flat and (want_norm in flat[key] or flat[key] in want_norm)
            for flat in normalized
        )
        if not hit:
            return False
    return True


def judge_answer(query: str, points: List[str], answer: str) -> tuple[Optional[int], Optional[bool]]:
    """LLM 当裁判：答案是否覆盖期望要点。返回 (1-5 分, 是否通过)。

    用 1-5 分而不是 0/1，是因为"部分覆盖"在 Agent 场景很常见，
    只有分数才能看出改动是让回答变好还是变差。
    """
    if not points or not answer.strip():
        return None, None

    try:
        from model.factory import get_chat_model
        from langchain_core.output_parsers import StrOutputParser

        prompt = (
            "你是评测裁判。判断【模型回答】是否覆盖了【期望要点】。\n"
            f"问题：{query}\n"
            f"期望要点：{' / '.join(points)}\n"
            f"模型回答：{answer}\n\n"
            "评分标准（只看要点覆盖与事实一致性，不看文采）：\n"
            "5 = 全部要点覆盖且无编造\n"
            "4 = 覆盖主要要点，遗漏不重要的细节\n"
            "3 = 覆盖一半左右\n"
            "2 = 只沾边\n"
            "1 = 完全没覆盖或答非所问\n\n"
            "只输出一个 1-5 的数字，不要任何解释。"
        )
        raw = (get_chat_model() | StrOutputParser()).invoke(prompt)
        m = re.search(r"[1-5]", raw or "")
        if not m:
            return None, None
        score = int(m.group(0))
        return score, score >= 4
    except Exception as e:  # 判分失败不该让整批评测挂掉
        logging.getLogger("eval").warning(f"判分失败: {e}")
        return None, None


# ──────────────────────────────────────────────────────────
# 单条用例
# ──────────────────────────────────────────────────────────
async def run_case(manager, recorder: ToolCallRecorder, row: Dict[str, str], timeout: int) -> CaseResult:
    cid = row["id"]
    category = row["category"]
    query = row["query"].strip()

    # 需要图片的用例补上真实路径，否则工具必然返回"文件不存在"。
    # 注意不能只看 category=="image"：multi 分类里也有既看图又查资料的用例（t045），
    # 只按分类注入会让 Agent 拿不到路径、一个工具都不调。
    needs_image = category == "image" or "image_analyze" in row["expect_tools"]
    if needs_image and IMAGE_SAMPLE.exists():
        query = f"{query}（图片路径：{IMAGE_SAMPLE}）"

    expected_tools = split_multi(row["expect_tools"])
    recorder.reset()
    started = time.time()
    answer_parts: List[str] = []
    error = ""

    async def _drain(agent) -> None:
        async for chunk in agent.execute_stream(query, manager.mcp_mgr):
            answer_parts.append(chunk)

    try:
        # 每条独立 session：上一条的工具结果不能进下一条的上下文
        agent = manager.get_agent(user_id=1, session_id=f"eval-{cid}")
        await asyncio.wait_for(_drain(agent), timeout=timeout)
    except asyncio.TimeoutError:
        error = f"超时({timeout}s)：模型或工具无响应"
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    elapsed = time.time() - started
    actual_calls = list(recorder.calls)
    actual_tools = [c["name"] for c in actual_calls]
    answer = "".join(answer_parts).strip()

    result = CaseResult(
        cid=cid,
        category=category,
        query=query,
        expected_tools=expected_tools,
        actual_tools=actual_tools,
        actual_args=[c.get("args") or {} for c in actual_calls],
        answer=answer,
        elapsed=elapsed,
        error=error,
    )
    score_case(result, row, actual_calls)
    return result


def score_case(result: CaseResult, row: Dict[str, str], actual_calls: List[Dict[str, Any]]) -> None:
    """按**当前** CSV 标注给一条结果打分。

    抽成独立函数的原因：标注是会改的（第一轮就改了三处），而工具轨迹是历史事实。
    历史轨迹 + 最新标注 = 正确分数，所以复用检查点时也要重打分，
    不能直接沿用当时写下的 tools_exact/args_ok 布尔值。
    """
    expected_tools = split_multi(row["expect_tools"])
    actual_tools = result.actual_tools
    result.expected_tools = expected_tools

    # 第一层：工具集合精确匹配（多调/漏调都算错）
    result.tools_exact = sorted(set(actual_tools)) == sorted(set(expected_tools))
    # 第二层：首个工具是否选对
    if expected_tools:
        result.tools_top1 = bool(actual_tools) and actual_tools[0] == expected_tools[0]
    else:
        result.tools_top1 = not actual_tools  # 期望不调工具时，调了就算错
    # 第三层：期望的工具是否全部被调到（多调不算错，只算"没漏"）
    result.tools_recall = set(expected_tools) <= set(actual_tools) if expected_tools else not actual_tools
    # 第四层：关键参数
    result.args_ok = check_args(row["expect_args"], actual_calls)


# ──────────────────────────────────────────────────────────
# 报表
# ──────────────────────────────────────────────────────────
def summarize(results: List[CaseResult]) -> Dict[str, Any]:
    total = len(results)
    if not total:
        return {}

    def rate(seq: List[bool]) -> float:
        return round(sum(1 for x in seq if x) / len(seq) * 100, 1) if seq else 0.0

    with_args = [r for r in results if r.args_ok is not None]
    judged = [r for r in results if r.judge_pass is not None]
    boundary = [r for r in results if r.category == "boundary"]

    return {
        "total": total,
        "errors": sum(1 for r in results if r.error),
        "tools_exact_rate": rate([r.tools_exact for r in results]),
        "tools_recall_rate": rate([r.tools_recall for r in results]),
        "tools_top1_rate": rate([r.tools_top1 for r in results]),
        "args_ok_rate": rate([r.args_ok for r in with_args]),
        "judge_pass_rate": rate([bool(r.judge_pass) for r in judged]),
        "judged_count": len(judged),
        "boundary_clean_rate": rate([r.tools_exact for r in boundary]) if boundary else None,
        "avg_tool_calls": round(sum(len(r.actual_tools) for r in results) / total, 2),
        "avg_elapsed": round(sum(r.elapsed for r in results) / total, 2),
        "p95_elapsed": _p95([r.elapsed for r in results]),
    }


def _p95(values: List[float]) -> float:
    """样本很小时用最近秩法，避免 int(n*0.95)-1 变成 0 号位。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(len(ordered) * 0.95)) - 1))
    return round(ordered[idx], 2)


def write_report(results: List[CaseResult], metrics: Dict[str, Any], stamp: str) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"agent_eval_{stamp}.md"

    lines = [
        f"# Agent 行为评测报告 {stamp}",
        "",
        "## 总分",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 用例数 | {metrics['total']} |",
        f"| 运行异常 | {metrics['errors']} |",
        f"| 工具集合精确匹配（多调/漏调都算错） | {metrics['tools_exact_rate']}% |",
        f"| 工具召回（期望工具都出现了） | {metrics['tools_recall_rate']}% |",
        f"| 首个工具选对（Top-1） | {metrics['tools_top1_rate']}% |",
        f"| 关键参数正确 | {metrics['args_ok_rate']}% |",
        f"| 答案要点通过（LLM 判） | {metrics['judge_pass_rate']}%（{metrics['judged_count']} 条参评） |",
        f"| 越界用例未被误调工具 | {metrics['boundary_clean_rate']}% |",
        f"| 平均工具调用次数 | {metrics['avg_tool_calls']} |",
        f"| 平均耗时 | {metrics['avg_elapsed']}s（P95 {metrics['p95_elapsed']}s） |",
        "",
        "## 失败明细",
        "",
    ]

    # 运行异常也要进失败明细：超时的用例工具可能调对了，但答案没有产出
    failed = [r for r in results
              if r.error or not (r.tools_exact and r.args_ok and (r.judge_pass is not False))]
    if not failed:
        lines.append("无。")
    else:
        lines += [
            "| id | 分类 | 期望工具 | 实际工具 | 问题 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for r in failed:
            problems = []
            if not r.tools_exact:
                problems.append("工具集合不符")
            if not r.args_ok:
                problems.append("参数不符")
            if r.judge_pass is False:
                problems.append(f"要点未覆盖({r.judge_score}分)")
            if r.error:
                problems.append(f"异常:{r.error[:60]}")
            lines.append(
                f"| {r.cid} | {r.category} | {'/'.join(r.expected_tools) or '-'} | "
                f"{'/'.join(r.actual_tools) or '-'} | {'；'.join(problems)} |"
            )

    lines += ["", "## 逐条结果", "", "| id | 分类 | 耗时 | 工具（实际） | 判分 |", "| --- | --- | --- | --- | --- |"]
    for r in results:
        lines.append(
            f"| {r.cid} | {r.category} | {r.elapsed:.1f}s | "
            f"{'/'.join(r.actual_tools) or '-'} | {r.judge_score if r.judge_score is not None else '-'} |"
        )

    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ──────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────
async def main_async(args) -> int:
    if not CSV_PATH.exists():
        print(f"评测集不存在: {CSV_PATH}")
        return 1

    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8-sig")))
    if args.ids:
        wanted = {x.strip() for x in args.ids.split(",") if x.strip()}
        rows = [r for r in rows if r["id"] in wanted]
    if args.limit:
        rows = rows[: args.limit]
    print(f"载入 {len(rows)} 条用例")

    # ── 断点续跑：读已完成的结果，避免一条挂死就全文重跑 ──
    done: Dict[str, Dict[str, Any]] = {}
    if CHECKPOINT_PATH.exists() and not args.force:
        for line in CHECKPOINT_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                done[item["cid"]] = item
            except json.JSONDecodeError:
                continue
        print(f"检查点命中 {len(done)} 条已完成用例")
    elif args.force and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print("已清空检查点，全量重跑")

    # ── 只重算、不重跑：检查点里有完整轨迹，改完标注不必再烧一遍 token ──
    if args.report_only:
        results: List[CaseResult] = []
        for row in rows:
            cached = done.get(row["id"])
            if not cached:
                print(f"  {row['id']} 无历史结果，跳过")
                continue
            r = CaseResult(
                cid=row["id"],
                category=cached.get("category", row["category"]),
                query=cached.get("query", row["query"]),
                expected_tools=cached.get("expected_tools", []),
                actual_tools=cached.get("actual_tools", []),
                actual_args=cached.get("actual_args", []),
                answer=cached.get("answer", ""),
                elapsed=cached.get("elapsed", 0.0),
                judge_score=cached.get("judge_score"),
                judge_pass=cached.get("judge_pass"),
                error=cached.get("error", ""),
            )
            score_case(r, row, [{"name": n, "args": a} for n, a in
                                zip(r.actual_tools, r.actual_args or [{}] * len(r.actual_tools))])
            results.append(r)
        if not results:
            print("检查点里没有可重算的结果")
            return 1
        metrics = summarize(results)
        stamp = args.stamp or datetime.now().strftime("%Y%m%d_%H%M")
        report_path = write_report(results, metrics, stamp)
        if not args.ids and not args.limit:
            BASELINE_PATH.write_text(json.dumps({
                "timestamp": stamp,
                "config": {"limit": 0, "judge": True, "recomputed": True},
                "metrics": metrics,
                "cases": {
                    r.cid: {
                        "expected": r.expected_tools,
                        "actual": r.actual_tools,
                        "args": r.actual_args,
                        "tools_exact": r.tools_exact,
                        "judge_score": r.judge_score,
                        "elapsed": round(r.elapsed, 2),
                    }
                    for r in results
                },
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"重算 {len(results)} 条 → {report_path}")
        for k, v in metrics.items():
            print(f"  {k}: {v}")
        return 0

    # 只留评测关心的日志级别，避免刷屏
    logging.getLogger().setLevel(logging.WARNING)
    agent_logger = logging.getLogger("agent")
    agent_logger.setLevel(logging.INFO)
    recorder = ToolCallRecorder()
    agent_logger.addHandler(recorder)

    from fastapi_app.deps import AgentManager
    from rag.scope import use_scope

    manager = AgentManager()
    print("启动 Agent（加载 RAG + MCP，约 60s）...")
    with use_scope(1, None):
        await manager.startup()

        waited = 0
        while not manager.is_ready and waited < 180:
            await asyncio.sleep(2)
            waited += 2
        if not manager.is_ready:
            print("Agent 未能在 180s 内就绪，终止")
            return 2
        print(f"Agent 就绪（等待 {waited}s）")

        results: List[CaseResult] = []
        for i, row in enumerate(rows, 1):
            cid = row["id"]
            # 已完成的不重跑：工具决策是确定性的历史事实，重跑只会白烧 token。
            # 但 --ids 指定的用例必须重跑（否则改了标注也刷不出新轨迹）。
            if cid in done and not args.force and not args.ids:
                cached = done[cid]
                r = CaseResult(
                    cid=cid,
                    category=cached.get("category", row["category"]),
                    query=cached.get("query", row["query"]),
                    expected_tools=cached.get("expected_tools", []),
                    actual_tools=cached.get("actual_tools", []),
                    actual_args=cached.get("actual_args", []),
                    answer=cached.get("answer", ""),
                    elapsed=cached.get("elapsed", 0.0),
                    tools_exact=cached.get("tools_exact", False),
                    tools_top1=cached.get("tools_top1", False),
                    args_ok=cached.get("args_ok", True),
                    judge_score=cached.get("judge_score"),
                    judge_pass=cached.get("judge_pass"),
                    error=cached.get("error", ""),
                )
                # 用当前 CSV 重打分：轨迹是历史事实，标注可能已经改过
                score_case(r, row, [{"name": n, "args": a} for n, a in
                                    zip(r.actual_tools, r.actual_args or [{}] * len(r.actual_tools))])
                results.append(r)
                print(f"[{i}/{len(rows)}] {cid} 已完成，跳过")
                continue

            print(f"[{i}/{len(rows)}] {cid} {row['query'][:24]}...", end=" ", flush=True)
            r = await run_case(manager, recorder, row, args.timeout)

            if not args.no_judge and row["expect_points"].strip():
                score, passed = await asyncio.to_thread(
                    judge_answer, r.query, split_multi(row["expect_points"]), r.answer
                )
                r.judge_score, r.judge_pass = score, passed

            flag = "OK" if (r.tools_exact and r.args_ok) else "FAIL"
            print(f"{flag} {r.elapsed:.1f}s tools={'/'.join(r.actual_tools) or '-'}")
            if r.error:
                print(f"    异常: {r.error}")
            results.append(r)

            # 每跑完一条就落盘：下一次 --limit/--ids 续跑时直接复用
            with CHECKPOINT_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "cid": r.cid,
                    "category": r.category,
                    "query": r.query,
                    "expected_tools": r.expected_tools,
                    "actual_tools": r.actual_tools,
                    "actual_args": r.actual_args,
                    "answer": r.answer,
                    "elapsed": r.elapsed,
                    "tools_exact": r.tools_exact,
                    "tools_top1": r.tools_top1,
                    "args_ok": r.args_ok,
                    "judge_score": r.judge_score,
                    "judge_pass": r.judge_pass,
                    "error": r.error,
                }, ensure_ascii=False) + "\n")

        # MCP 的 stdio 客户端在 shutdown 时会抛 "cancel scope in a different task"，
        # 这是 anyio 的已知行为，进程马上退出，不影响评测结果，吞掉即可。
        try:
            await manager.shutdown()
        except RuntimeError as e:
            print(f"（关闭 MCP 时 anyio 报错，可忽略：{e}）")

    metrics = summarize(results)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    report_path = write_report(results, metrics, stamp)

    baseline = {
        "timestamp": stamp,
        "config": {"limit": args.limit, "judge": not args.no_judge, "model": "见 model/factory.py"},
        "metrics": metrics,
        "cases": {
            r.cid: {
                "expected": r.expected_tools,
                "actual": r.actual_tools,
                "args": r.actual_args,
                "tools_exact": r.tools_exact,
                "judge_score": r.judge_score,
                "elapsed": round(r.elapsed, 2),
            }
            for r in results
        },
    }
    # 只有全量跑才更新基线：--ids / --limit 是局部结果，
    # 覆盖基线会让下次对比"退化了吗"时拿错参照物。
    if not args.ids and not args.limit:
        BASELINE_PATH.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已更新: {BASELINE_PATH}")
    else:
        print("局部运行，不更新基线")

    print("\n===== 总分 =====")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    print(f"\n报表: {report_path}")
    print(f"基线: {BASELINE_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 行为评测")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 条（冒烟用）")
    parser.add_argument("--ids", default="", help="只跑指定 id，逗号分隔，如 t036,t037")
    parser.add_argument("--timeout", type=int, default=DEFAULT_CASE_TIMEOUT, help="单条超时秒数")
    parser.add_argument("--no-judge", action="store_true", help="跳过 LLM 判答案，省 token")
    parser.add_argument("--force", action="store_true", help="忽略检查点，全量重跑")
    parser.add_argument("--report-only", action="store_true",
                        help="只按最新标注重算检查点里的历史轨迹，不启动 Agent、不烧 token")
    parser.add_argument("--stamp", default="", help="报表时间戳（report-only 时用于区分版本）")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
