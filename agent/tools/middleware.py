import asyncio
from utils.path_tool import ensure_project_root_in_syspath
ensure_project_root_in_syspath()

from typing import Callable
from langchain.agents import AgentState
from langchain.agents.middleware import wrap_tool_call, before_model
from langchain.tools.tool_node import ToolCallRequest
from langchain_core.messages import ToolMessage
from langgraph.runtime import Runtime
from langgraph.types import Command
from utils.logger_handler import logger

#handler（request）全过程
'''
中间件 + 工具调用链路总结
LLM 决定调 rag_summarize(query="大户型")
  ↓
引擎创建 ToolCallRequest
  ↓
中间件链：
  monitor_tool(request, handler=路由函数)
    ├─ 打日志："执行工具：rag_summarize"
     handler 是调用工具”可理解成：handler 这一层只负责把参数原样交给 execute，execute 
     再把参数交给 _execute_tool_async，真正的 tool.ainvoke 发生在最里面。
    ├─ await handler(request)
    │    └─ 路由函数：
    │         ├─ tool_dict["rag_summarize"]  → 找到 Tool 对象
    │         └─ Tool.invoke({"query": "大户型"})
    │              └─ mcp_tool_func(**{"query": "大户型"})
    │                   └─ call_mcp_tool("rag_summarize", {"query": "大户型"})
    │                        └─ Stdio 管道 → 子进程执行真正的 rag_summarize
    │                                              ↓
    │                         ← 子进程返回文本结果 ←
    └─ 返回结果给引擎
  ↓
引擎把结果包装成 ToolMessage
  ↓
LLM 读到 ToolMessage → 生成最终回答
ToolCallRequest(
    tool_call={...},
    runtime=Runtime(...)  ← 属性是 Runtime 对象
)
'''
@wrap_tool_call
async def monitor_tool(
        # 请求的数据封装
        request: ToolCallRequest,
        # 执行的函数本身
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
) -> ToolMessage | Command:             # 工具执行的监控
    logger.info(f"[tool monitor]执行工具：{request.tool_call['name']}")
    logger.info(f"[tool monitor]传入参数：{request.tool_call['args']}")

    try:
        logger.info("=== ⚖️ 【绝对在工具执行前】 ===")
        result = await handler(request) if asyncio.iscoroutinefunction(handler) else handler(request)
        logger.info(f"=== 🎁 【工具真实返回的结果是】：{result} ===")
        logger.info(f"[tool monitor]工具{request.tool_call['name']}调用成功")

        return result
    except Exception as e:
        logger.error(f"工具{request.tool_call['name']}调用失败，原因：{str(e)}")
        raise e


@before_model
def log_before_model(
        state: AgentState,          # 整个Agent智能体中的状态记录
        runtime: Runtime,           # 运行时上下文（@before_model 会传两个参数：state 和 runtime）
):         # 在模型执行前输出日志
    logger.info(f"[log_before_model]即将调用模型，带有{len(state['messages'])}条消息。")

    logger.debug(f"[log_before_model]{type(state['messages'][-1]).__name__} | {state['messages'][-1].content.strip()}")

    return None


if __name__ == '__main__':
    from langchain.agents import middleware

    print(middleware.__file__)
    import langchain

    print(langchain.__version__)