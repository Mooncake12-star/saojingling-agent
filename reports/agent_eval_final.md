# Agent 行为评测报告 final

## 总分

| 指标 | 数值 |
| --- | --- |
| 用例数 | 51 |
| 运行异常 | 1 |
| 工具集合精确匹配（多调/漏调都算错） | 58.8% |
| 工具召回（期望工具都出现了） | 98.0% |
| 首个工具选对（Top-1） | 84.3% |
| 关键参数正确 | 100.0% |
| 答案要点通过（LLM 判） | 82.0%（50 条参评） |
| 越界用例未被误调工具 | 80.0% |
| 平均工具调用次数 | 1.78 |
| 平均耗时 | 47.21s（P95 81.44s） |

## 失败明细

| id | 分类 | 期望工具 | 实际工具 | 问题 |
| --- | --- | --- | --- | --- |
| t005 | rag | rag_summarize | rag_summarize | 要点未覆盖(3分) |
| t012 | recommend | product_recommend | product_recommend/rag_summarize/product_recommend | 工具集合不符 |
| t013 | recommend | product_recommend | product_recommend/rag_summarize/product_recommend | 工具集合不符 |
| t014 | recommend | product_recommend | product_recommend/product_recommend/rag_summarize/price_query/price_query | 工具集合不符 |
| t015 | recommend | product_recommend | rag_summarize/product_recommend/product_recommend | 工具集合不符 |
| t016 | recommend | product_recommend | rag_summarize/product_recommend/product_recommend | 工具集合不符 |
| t017 | recommend | product_recommend | rag_summarize/product_recommend/product_recommend | 工具集合不符 |
| t022 | price | price_query | rag_summarize/price_query | 工具集合不符 |
| t025 | search | sougou_search | sougou_search/rag_summarize | 工具集合不符；要点未覆盖(2分) |
| t027 | search | sougou_search | rag_summarize/sougou_search | 工具集合不符 |
| t028 | search | sougou_search | sougou_search/rag_summarize | 工具集合不符；要点未覆盖(2分) |
| t029 | weather | get_current_weather | get_current_weather/sougou_search | 工具集合不符；要点未覆盖(1分) |
| t030 | weather | get_current_weather | get_current_weather/sougou_search | 工具集合不符；要点未覆盖(2分) |
| t031 | weather | get_current_weather | get_current_weather/sougou_search | 工具集合不符；要点未覆盖(1分) |
| t032 | weather | get_current_weather | get_current_weather/rag_summarize | 工具集合不符 |
| t033 | image | image_analyze | image_analyze/rag_summarize | 工具集合不符 |
| t034 | image | image_analyze | image_analyze/rag_summarize | 工具集合不符 |
| t035 | image | image_analyze | image_analyze/rag_summarize | 工具集合不符 |
| t036 | report | export_report | export_report | 异常:超时(420s)：模型或工具无响应 |
| t042 | multi | product_recommend/export_report | product_recommend/rag_summarize/export_report | 工具集合不符 |
| t043 | multi | sougou_search/price_query | rag_summarize/price_query/price_query/sougou_search/rag_summarize | 工具集合不符；要点未覆盖(3分) |
| t044 | multi | get_current_weather/rag_summarize | get_current_weather/rag_summarize/sougou_search | 工具集合不符；要点未覆盖(3分) |
| t048 | boundary | - | - | 要点未覆盖(1分) |
| t049 | boundary | - | rag_summarize/sougou_search | 工具集合不符 |

## 逐条结果

| id | 分类 | 耗时 | 工具（实际） | 判分 |
| --- | --- | --- | --- | --- |
| t001 | rag | 54.5s | rag_summarize/product_recommend | 5 |
| t002 | rag | 39.8s | rag_summarize | 4 |
| t003 | rag | 41.2s | rag_summarize | 4 |
| t004 | rag | 48.5s | rag_summarize | 4 |
| t005 | rag | 45.9s | rag_summarize | 3 |
| t006 | rag | 52.5s | rag_summarize | 5 |
| t007 | rag | 41.1s | rag_summarize | 5 |
| t008 | rag | 53.6s | rag_summarize | 5 |
| t009 | rag | 50.8s | rag_summarize | 5 |
| t010 | rag | 38.7s | rag_summarize | 5 |
| t011 | recommend | 12.1s | product_recommend | 5 |
| t012 | recommend | 65.9s | product_recommend/rag_summarize/product_recommend | 4 |
| t013 | recommend | 58.0s | product_recommend/rag_summarize/product_recommend | 5 |
| t014 | recommend | 72.5s | product_recommend/product_recommend/rag_summarize/price_query/price_query | 5 |
| t015 | recommend | 72.0s | rag_summarize/product_recommend/product_recommend | 5 |
| t016 | recommend | 49.2s | rag_summarize/product_recommend/product_recommend | 5 |
| t017 | recommend | 37.6s | rag_summarize/product_recommend/product_recommend | 5 |
| t018 | recommend | 14.2s | product_recommend/product_recommend | 5 |
| t019 | price | 4.3s | price_query | 5 |
| t020 | price | 7.8s | price_query | 5 |
| t021 | price | 8.0s | price_query | 5 |
| t022 | price | 36.8s | rag_summarize/price_query | 5 |
| t023 | price | 4.6s | price_query | 5 |
| t024 | price | 8.7s | price_query | 5 |
| t025 | search | 79.3s | sougou_search/rag_summarize | 2 |
| t026 | search | 35.2s | rag_summarize | 5 |
| t027 | search | 33.7s | rag_summarize/sougou_search | 4 |
| t028 | search | 44.3s | sougou_search/rag_summarize | 2 |
| t029 | weather | 8.4s | get_current_weather/sougou_search | 1 |
| t030 | weather | 13.1s | get_current_weather/sougou_search | 2 |
| t031 | weather | 13.3s | get_current_weather/sougou_search | 1 |
| t032 | weather | 43.0s | get_current_weather/rag_summarize | 5 |
| t033 | image | 77.4s | image_analyze/rag_summarize | 5 |
| t034 | image | 72.8s | image_analyze/rag_summarize | 5 |
| t035 | image | 95.4s | image_analyze/rag_summarize | 4 |
| t036 | report | 420.3s | export_report | - |
| t037 | report | 61.6s | rag_summarize/export_report | 5 |
| t038 | report | 19.4s | export_report | 5 |
| t039 | multi | 43.3s | price_query/rag_summarize | 5 |
| t040 | multi | 24.2s | product_recommend/product_recommend/price_query | 5 |
| t041 | multi | 8.9s | price_query/price_query | 5 |
| t042 | multi | 71.5s | product_recommend/rag_summarize/export_report | 5 |
| t043 | multi | 81.4s | rag_summarize/price_query/price_query/sougou_search/rag_summarize | 3 |
| t044 | multi | 52.0s | get_current_weather/rag_summarize/sougou_search | 3 |
| t045 | multi | 100.4s | image_analyze/rag_summarize | 5 |
| t046 | multi | 29.7s | product_recommend/product_recommend/export_report | 5 |
| t047 | boundary | 10.6s | - | 5 |
| t048 | boundary | 5.2s | - | 1 |
| t049 | boundary | 39.7s | rag_summarize/sougou_search | 5 |
| t050 | boundary | 3.9s | - | 5 |
| t051 | boundary | 1.5s | - | 5 |