# Agent 行为评测

对手写版 Agent（`agent/react_agent.py`）做**行为层**评测，回答一个问题：
**给定这句用户输入，Agent 是否调了正确的工具、传了正确的参数、给了可用的答案。**

它和 `tests/eval_rag.py` 不是一回事：

| | eval_rag.py | 本目录 |
| --- | --- | --- |
| 评的对象 | 检索链（向量 + BM25 + 重排） | Agent 的决策与执行 |
| 核心指标 | Hit@1 / Hit@3 | 工具选择准确率、参数正确率、答案要点覆盖 |
| 是否管工具 | 否，直接调检索 | 是，看它自己决定调谁 |

## 一、文件

```
tests/eval/
├── agent_eval_set.csv     评测集（51 条，人工标注）
├── run_agent_eval.py      跑批 + 判分 + 出报表
├── baseline.json          最近一次的基线（机器读，可 git diff 看退化）
└── README.md              本文件

reports/agent_eval_<时间戳>.md   人看的报表
```

## 二、评测集字段

| 列 | 含义 | 例 |
| --- | --- | --- |
| `id` | 用例编号 | `t019` |
| `category` | 分类 | `rag` / `recommend` / `price` / `search` / `weather` / `image` / `report` / `multi` / `boundary` |
| `query` | 用户输入 | `智扫 X3 Ultra 多少钱` |
| `expect_tools` | 期望调用的工具，`\|` 分隔，空表示不该调任何工具 | `price_query` |
| `expect_args` | 关键参数断言，`key=值`，`\|` 分隔 | `product_name=智扫 X3 Ultra` |
| `expect_points` | 答案必须覆盖的要点，`\|` 分隔 | `4599` |
| `判断依据` | 为什么这么标（给人看的） | — |
| `你的确认` | 标注状态 | `√` |

51 条的分布：rag 10、recommend 8、multi 8、price 6、boundary 5、search 4、weather 4、image 3、report 3。

## 三、指标口径

| 指标 | 怎么算 | 严格程度 |
| --- | --- | --- |
| **工具集合精确匹配** | `set(实际) == set(期望)` | 最严：多调一个、漏调一个都算错 |
| **工具召回** | `set(期望) ⊆ set(实际)` | 中等：只要求"该调的调了"，多调不算错 |
| **首个工具选对（Top-1）** | 第一次工具调用的名字 == 期望列表第一个 | 中等：只看决策起点 |
| **关键参数正确** | `expect_args` 里每个 `key=值` 都能在某次调用的入参里找到 | 宽松：子串匹配，不校验类型 |
| **答案要点通过** | LLM 裁判给 1–5 分，≥4 算通过 | 主观项，但用同一 prompt 保持可比 |
| **越界用例未被误调工具** | `boundary` 类里 `actual_tools` 为空的占比 | 安全项：误调就等于越权执行 |

**为什么要同时看「精确匹配」和「召回」**：这个 Agent 拿到任何工具结果后，倾向再补一次
`rag_summarize` 去找官方依据——这是架构决定的（RAG Agent 要 grounding），不是错误。
只报精确匹配会把大量正确行为判成失败；只报召回又会掩盖"乱调工具"。
两个一起看才有信息量：**精确匹配低 + 召回高 = 多调，不是漏调。**

**标注写错比 Agent 出错更常见**：第一轮 14 条"参数不符"里 11 条是标注本身的问题
（把枚举值 `中户型` 当成工具入参、键名写成中文 `城市` 而工具参数是 `city`、
在值里写描述性文字"uploads/ 下图片"）。改标注不用重跑——

```bash
# 按最新标注重算检查点里的历史轨迹，不启动 Agent、不烧 token（几秒）
python tests/eval/run_agent_eval.py --report-only --stamp v2
```

## 四、怎么跑

```bash
# 冒烟：3 条，跳过判分（约 3 分钟，主要耗时是启动 Agent）
python tests/eval/run_agent_eval.py --limit 3 --no-judge

# 全量：51 条含判分（约 45–60 分钟，每条一次真实 Agent 调用 + 一次裁判调用）
python tests/eval/run_agent_eval.py

# 指定用例重跑（改了标注或怀疑某条是偶发，--ids 会无视检查点强制重跑）
python tests/eval/run_agent_eval.py --ids t045,t036 --timeout 180

# 只重算分数，不重跑
python tests/eval/run_agent_eval.py --report-only
```

单条耗时 40–60 秒，瓶颈是 RAG 检索（20s）+ MCP 子进程往返 + 模型生成，属正常。

**注意 `--ids` 会强制重跑**：不加这个语义，改了标注也刷不出新轨迹，检查点反而会
把新标注套在旧轨迹上、让你以为模型行为变了。

## 五、工具调用轨迹怎么抓的

`agent/tools/middleware.py` 里的 `monitor_tool` 本来就打了两行日志：

```
[tool monitor]执行工具：price_query
[tool monitor]传入参数：{'product_name': '智扫 X3 Ultra'}
```

评测脚本挂一个 `logging.Handler` 到 `"agent"` logger 上，正则把这些捞出来，**不改一行业务代码**。

代价：**依赖上面两行日志文案**。如果改了 `middleware.py` 的措辞，必须同步改
`run_agent_eval.py` 顶部的 `RE_TOOL` / `RE_ARGS`，否则工具轨迹会全空、分数会掉到 0。

## 六、基线怎么用

`baseline.json` 存的是最近一次的逐条结果。改完 prompt、换模型、加工具之后重跑，对比：

```bash
# 重跑后看哪些用例从过变挂
python -c "
import json
new = json.load(open('tests/eval/baseline.json', encoding='utf-8'))
print(new['metrics'])
"
```

判断标准：**工具集合匹配率掉 3 个点以上，就说明这次改动伤到了决策，要回滚或补 prompt**。
只涨不跌的改动才进主干。

## 七、维护约定

1. 新增能力（比如加了「提交工单」工具）→ 在 CSV 里补 3–5 条覆盖它的用例，跑一次更新基线。
2. 标注和实际行为长期不一致时，先问一句「是 Agent 错了，还是我标窄了」。
   第一轮跑出来 `t001`（大户型适合什么扫地机器人）就是这种情况：标注只让走检索，
   Agent 却调了 `product_recommend` + `rag_summarize`——它是对的，标注该改。
3. 不要为了让分数好看去放宽 `expect_tools`。评测集是尺子，尺子不能对着结果改。

## 八、和运行时护栏的关系

Agent 侧有三层护栏（`config/agent.yml` 的 `guard`：模型 90s、工具 90s、流式空闲 150s、
步数 12）。评测的单条超时默认 **240s**，必须大于「模型 90s × 重试 + 工具 90s」，否则
护栏还没生效就先被评测判超时了。

护栏生效时，用例的表现是**有答案、答案末尾带一句中断说明**，而不是整批挂死：

```
[42/51] t042 ... OK 104.0s tools=export_report
（本次处理已中断：150 秒内模型没有返回任何内容。请重试，或把问题拆成更小的步骤。）
```

这类结果要单独看：工具选对了、但答案没产出，`judge_pass` 会偏低，别把它当成"模型不会答"。
