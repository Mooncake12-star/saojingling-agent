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
from utils.config_handler import agent_conf
from utils.logger_handler import logger

# 单次工具调用上限：MCP 子进程卡住时，async 调用会永久等待，
# 不设上限就把整轮对话一起拖死（评测里 t036 就是这么挂的）。
_TOOL_TIMEOUT = float((agent_conf.get("guard") or {}).get("tool_timeout", 90))

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
        call = handler(request) if not asyncio.iscoroutinefunction(handler) else handler(request)
        result = await asyncio.wait_for(call, timeout=_TOOL_TIMEOUT)
        logger.info(f"=== 🎁 【工具真实返回的结果是】：{result} ===")
        logger.info(f"[tool monitor]工具{request.tool_call['name']}调用成功")

        return result
    except asyncio.TimeoutError:
        # 超时不能往外抛：抛出去整轮对话就断了，用户什么也拿不到。
        # 把超时变成一个"工具结果"喂回模型，让它自己降级（改用知识库或直接说明查不到），
        # 这样 SSE 流不中断，前端还能拿到一句人话。
        msg = f"工具{request.tool_call['name']}调用超时（>{_TOOL_TIMEOUT:.0f}s），本次未获取到结果。"
        logger.error(f"[tool monitor]{msg} 请改用其他信息源或如实告知用户。")
        return ToolMessage(content=msg, tool_call_id=request.tool_call.get("id", ""))
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