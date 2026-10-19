import asyncio
import os
import sys
from utils.path_tool import ensure_project_root_in_syspath
ensure_project_root_in_syspath()

from typing import AsyncGenerator, List, Dict, Any, Optional
from model.factory import get_chat_model
from utils.prompt_loader import load_main_prompts
from agent.tools.middleware import monitor_tool, log_before_model
from db.database import get_connection
from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage, AIMessage



class ReactMCPContextManager:
    """MCP 服务端生命周期与工具路由管理器 """


    '''
        command — 要执行的程序名（如 "python"、"node"）
        args — 传给该程序的参数列表（如 ["server.py", "--port", "8080"]）
        script_dir — 可选，脚本所在的目录
        扩展知识：cwd何时传入：不传 cwd 时，子进程继承父进程的当前工作目录（CWD）。args 里的相对路径是相对于子进程的 CWD 解析的。
        扩展知识：只有当要拉起的进程与主进程在同一目录时，才可以不传 cwd
    '''
    def __init__(self, command: str, args: List[str], script_dir: Optional[str] = None):
        self.command = command
        # 在初始化时，如果指定了脚本所在的目录，强行将 args 里的脚本名转化为绝对路径！
        if script_dir and args:
            self.args = [os.path.normpath(os.path.join(script_dir, arg)) if not os.path.isabs(arg) else arg for arg in args]

            script_path = self.args[0]
            if not os.path.exists(script_path):
                print(f"[MCP子进程] 启动失败，文件不存在: {script_path}")
                self.args = []
        else:
            self.args = args

        self.script_dir = script_dir
        self._client_context = None
        self.session: Optional[ClientSession] = None
        self.llm_tools_schema: List[Dict[str, Any]] = []
        # 本地优先的工具实现：{工具名: callable(arguments: dict) -> str}
        # MCP 子进程只负责"提供工具定义 + 协议适配"，重计算能力若主进程已有，
        # 就直接在主进程执行，避免子进程重复加载模型（见 register_local_tool）
        self.local_tools: Dict[str, Any] = {}

    async def __aenter__(self):
        """进入上下文：建立 I/O 管道并初始化 MCP 会话"""
        #把当前进程的全部环境变量复制一份，得到一个新的 dict,不修改原来的环境变量
        current_env = os.environ.copy()
        current_env["PYTHONIOENCODING"] = "utf-8"

        current_env["PYTHONUTF8"] = "1"

        # 显式注入 PYTHONPATH 确保子进程环境干净，
        # 子进程 Python 的 sys.path 在没有 PYTHONPATH ，拉起子进程时把 PYTHONPATH 里的路径追加到 sys.path 里。
        if self.script_dir:
            project_root = os.path.dirname(os.path.dirname(self.script_dir))
            #PYTHONPATH 是环境变量，作用只有一个：告诉 Python 解释器"除了默认位置，还要去哪些目录找模块
            current_env["PYTHONPATH"] = os.pathsep.join([project_root, self.script_dir, current_env.get("PYTHONPATH", "")])

        # 打印调试日志，让我们在控制台一眼看出拉起的到底是什么绝对路径
        print(f"[底层诊断] 脚本绝对路径: {self.args}")
        print(f"[底层诊断] 子进程 CWD 将继承父进程: {os.getcwd()}（未通过 Popen 指定）")

        server_params = StdioServerParameters(
            #构造 MCP Server 的启动参数（有类型校验的配置对象）
            command=self.command,
            args=self.args,
            env=current_env
        )

        #stdio_client(server_params)创建一个“负责启动和管理 MCP Server 通信生命周期”的异步上下文管理器对象。
        self._client_context = stdio_client(server_params)

        '''
        启动 MCP 子进程、准备管道通信资源等操作，底层可能涉及需要等待的系统操作，因此使用异步机制。
        执行到 await self._client_context.__aenter__() 时，如果 __aenter__() 内部遇到需要等待的异步操作，
        当前协程会被挂起，让出执行权给事件循环，事件循环可以去处理其他就绪的协程和 I/O 事件。
        等 __aenter__() 所等待的异步操作完成后，事件循环再从挂起点恢复这个协程，从 await 的位置继续执行，
        最终拿到 read_stream 和 write_stream。write_stream ──→ 子进程 stdin     （父进程往子进程发送）
                                            read_stream  ←── 子进程 stdout    （子进程 → 父进程）              
        '''
        # 建立 Stdio 通信管道
        read_stream, write_stream = await self._client_context.__aenter__()

        # 初始化 MCP 客户端会话
        '''
            1. 封包：把 MCP 初始化参数（版本号、客户端能力声明）打包成 JSON-RPC 格式
            (JSON 本身只规定数据怎么表示,JSON-RPC 要求按照规定的字段组织)
            2. 写入 write_stream：字符流转字节流进管道，子进程的 stdin 收到
            3. 读取 read_stream：子进程回复确认，字节流转回 JSON，解析出服务端能力（支持哪些工具、资源等）
            不是管道解析成 RPC，是两端的代码主动把数据按 RPC 格式打包/解包。
            管道本身只传原始字节流，没有"解析"能力。
            4全流程.父进程的 ClientSession 把要发的调用（如 list_tools）打包成 JSON-RPC 格式的字节串，写入管道
             子进程那边的 MCP 服务器从 stdin 读到字节串，按 JSON-RPC 解析，执行对应功能，把结果再打成 JSON-RPC 字节串写回 stdout
             父进程的 ClientSession 从管道读到回复，解析成 Python 对象返回给你
            '''
        #ClientSession = MCP 协议的客户端实现。它把"裸管道"变成"能用 Python 对象调用的接口"
        self.session = ClientSession(read_stream, write_stream)
        await self.session.__aenter__()

        # 协议握手初始化
        # 必须设超时：子进程若在 import 阶段做耗时初始化、或直接崩溃不响应，
        # initialize() 会无限等待，日志上既不报错也不成功（只能看到"卡住"）。
        MCP_HANDSHAKE_TIMEOUT = 180  # 秒
        try:
            await asyncio.wait_for(self.session.initialize(), timeout=MCP_HANDSHAKE_TIMEOUT)
        except asyncio.TimeoutError:
            raise RuntimeError(
                f"MCP 握手超时（>{MCP_HANDSHAKE_TIMEOUT}s）：子进程未响应。"
                f"请检查 mcp_server.py 是否在 import 阶段做了耗时初始化。"
            )

        # 动态同步远程工具集，填充完毕self.llm_tools_schema
        #将MCP的是 ListToolsResult（FastMCP 内部对象），转换成大模型可以看懂的fucntion calling
        try:
            await asyncio.wait_for(self._sync_mcp_tools(), timeout=60)
        except asyncio.TimeoutError:
            raise RuntimeError("MCP 工具列表拉取超时（>60s）：服务端未响应 tools/list")
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """安全退出，释放进程管道"""
        if self.session:
            await self.session.__aexit__(exc_type, exc_val, exc_tb)
        if self._client_context:
            await self._client_context.__aexit__(exc_type, exc_val, exc_tb)

    async def _sync_mcp_tools(self):
        """拉取 MCP Server 注册的工具，并转化为大模型识别的 Tool Schema"""
        #得到mcp协议的返回格式
        '''
        ① ClientSession 打包成 JSON-RPC（在内存里）
           {"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
                ↓ 编码成 UTF-8 字节
        ② 写进 write_stream → 管道 → 子进程 stdin
                ↓
        ③ 子进程（FastMCP）从 stdin 读到，按 JSON-RPC 解析
           → 遍历所有用 @mcp.tool() 注册的函数
           → 生成工具清单（name / description / inputSchema）
                ↓ 打包成响应字节写回 stdout
        ④ 父进程从 read_stream 读到 → 解码 → 解析
        ⑤ 返回 ListToolsResult 对象
        '''
        mcp_result = await self.session.list_tools()
        #Schema 是数据的结构定义，描述数据长什么样、每个字段什么类型、必须传哪些、可选哪些。
        '''
        在 MCP 场景里，子进程把工具的 schema 发给父进程，父进程转给 LLM——LLM 根据 schema 知道"这个函数要怎么调、参数传什么格式"，
        然后生成符合 schema 的调用参数。Schema 就是接口契约，让不同的系统之间知道数据该怎么拼。
        '''
        self.llm_tools_schema = []
        # 把mcp协议返回的工具格式ListToolsResult对象转换成大模型可以看懂 OpenAI function-calling 格式
        for tool in mcp_result.tools:
            self.llm_tools_schema.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.inputSchema
                }
            })

    def register_local_tool(self, name: str, func) -> None:
        """把某个 MCP 工具的实现改为"本地优先"。

        背景（真实踩坑）：RAG 工具原本只在 MCP 子进程里执行，子进程为此要
        单独 import torch / transformers 并加载 BGE 重排模型（约 1.5GB、40s+）。
        主进程（FastAPI）里本来就有一份向量库和模型，8GB 内存的机器上两份叠
        加会把系统打进换页，子进程工作集被换出到几 MB，表现为"调用永久卡死"
        ——不是死锁，是没有内存可用。

        所以：工具定义仍由 MCP 提供（对大模型完全透明），但实现回落到主进程
        已有的单例，全系统只保留一份模型。
        """
        self.local_tools[name] = func

    async def call_mcp_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """代为执行外部 服务端工具"""
        # 本地优先：主进程内已有实现就不再跨进程调用（省一份模型的内存和时间）
        local_impl = self.local_tools.get(name)
        if local_impl is not None:
            # 这类实现是同步阻塞的（模型推理可能几十秒），必须丢线程池，
            # 否则会卡死 FastAPI 的事件循环，整个服务都失去响应。
            return await asyncio.to_thread(local_impl, arguments)

        if not self.session:
            raise RuntimeError("MCP Session 未建立连接")
        '''
        result 不是一个普通字符串，而是 MCP SDK 返回的工具调用结果对象。在你这段代码里，
        可以把它理解成一个 CallToolResult 对象
        CallToolResult(
        content=[
            TextContent(
                type="text",
                text="工具执行返回的文本"
            )
        ],
        isError=False
        )
        '''
        result = await self.session.call_tool(name, arguments)
        #hasattr(obj, name) 是 Python 内置函数，判断对象 obj 有没有叫 name 的属性。有返回 True，没有返回 False。
        #过滤出纯文本内容,这里的遍历等于content= TextContent（），等于实力化，自然可以拿 TextContent里面的属性
        return "".join([content.text for content in result.content if hasattr(content, 'text')])
        #create mcp tool的必要性
'''
        核心原因：LangChain 和 MCP 不认识彼此的工具格式。
        MCP 工具格式：    @mcp.tool() 装饰器 + JSON Schema 参数
        LangChain 要求：  @langchain_tool() 装饰器 + Pydantic 参数模型
        如果没有 _create_mcp_tool，create_agent() 拿到的就是一堆 JSON Schema 字典，LangChain 根本不知道如何调用它们。
        这个函数做了格式翻译：把 MCP 的工具定义转成 LangChain 的 Tool 对象，Agent 才能正常调度。
        '''
def  _wrap_as_langchain_tool (mcp_manager: ReactMCPContextManager, name: str, description: str, input_schema: dict):
        """工厂函数：为每个 MCP 工具创建一个 LangChain Tool 对象"""
        from langchain_core.tools import tool as langchain_tool
        from pydantic import Field, create_model

        fields = {}
        if input_schema and "properties" in input_schema:
            required = input_schema.get("required", [])
            TYPE_MAP = {"string": str, "integer": int, "number": float, "boolean": bool, "array": list, "object": dict}
            #items:字典方法，返回所有键值对，每个元素是一个 (key, value) 元组。
            for prop_name, prop_info in input_schema["properties"].items():
                py_type = TYPE_MAP.get(prop_info.get("type", "string"), str)
                default = ... if prop_name in required else None
                fields[prop_name] = (py_type, Field(default=default, description=prop_info.get("description", "")))
        #** 是字典解包，把字典的每个键值对拆成关键字参数传进去
        args_schema = create_model(f"{name}Arguments", **fields) if fields else None

        @langchain_tool(description=description, args_schema=args_schema)
        async def mcp_tool_func(**kwargs) -> str:
            result = await mcp_manager.call_mcp_tool(name, kwargs)
            return result

        mcp_tool_func.name = name
        return mcp_tool_func


class ReactAgent:
    def __init__(self, session_id: str = "default"):
        self.model = get_chat_model()
        self.system_prompt = load_main_prompts()
        self.session_id = session_id
        self.conversation_history: list = []
        self._load_history()

    def _load_history(self):
        conn = get_connection()
        rows = conn.execute(
            "SELECT role, content FROM conversations WHERE session_id = ? ORDER BY id",
            (self.session_id,)
        ).fetchall()
        conn.close()
        self.conversation_history = []
        for row in rows:
            if row["role"] == "human":
                self.conversation_history.append(HumanMessage(content=row["content"]))
            else:
                self.conversation_history.append(AIMessage(content=row["content"]))
        # 只保留最近 4 轮
        MAX_HISTORY = 4
        if len(self.conversation_history) > MAX_HISTORY * 2:
            self.conversation_history = self.conversation_history[-(MAX_HISTORY * 2):]

    def _save_message(self, role: str, content: str):
        conn = get_connection()
        conn.execute(
            "INSERT INTO conversations (session_id, role, content) VALUES (?, ?, ?)",
            (self.session_id, role, content)
        )
        conn.commit()
        conn.close()

    async def execute_stream(self, query: str, mcp_manager: ReactMCPContextManager) -> AsyncGenerator[str, None]:

        mcp_tools = []
        for tool_schema in mcp_manager.llm_tools_schema:
            name = tool_schema["function"]["name"]
            description = tool_schema["function"]["description"]
            parameters = tool_schema["function"]["parameters"]
            mcp_tools.append( _wrap_as_langchain_tool (mcp_manager, name, description, parameters))

        agent = create_agent(
            self.model,
            mcp_tools,
            system_prompt=self.system_prompt,
            middleware=[monitor_tool, log_before_model],
        )

        messages = list(self.conversation_history)
        messages.append(HumanMessage(content=query))
        inputs = {"messages": messages}
        full = ""
        #agent.astream执行流执行过程
        '''
        agent.astream(inputs, stream_mode="values")
  ↓
第 1 次循环：
  ├─ system_prompt → main_prompt.txt（create_agent 时固定注入）
  ├─ @before_model → log_before_model
  │    日志：即将调用模型，带有 N 条消息
  ├─ LLM 推理（qwen3-max 看到问题 + 工具列表）
  ├─ 输出：Thought → "我需要检索本地知识库"
  ├─ 输出：tool_calls → rag_summarize(query="大户型适合什么")
  └─ yield update = {"messages": [HumanMessage, ...], "agent": {...}}（决策）
  ↓
你的循环体：
  messages = update.get("messages", [])
  last = messages[-1]          → AIMessage(tool_calls=[...])
  last.type == "ai" ✓
  getattr(last, "tool_calls", None) → 有值！不是 None！ → 不 yield，跳过
  ↓
第 2 次循环（引擎自动调工具）：
  ├─ @wrap_tool_call → monitor_tool
  │    日志：执行工具 rag_summarize
  │    调用子进程 → 子进程返回文本
  │    日志：工具返回结果
  ├─ 引擎把 ToolMessage 追加到 messages 列表
  ├─ yield update = {"messages": [..., ToolMessage]}（执行）
  ↓
你的循环体：
  last = messages[-1]          → ToolMessage
  last.type == "ai" ✗ → 跳过
  ↓
第 3 次循环：
  ├─ system_prompt → main_prompt.txt
  ├─ @before_model → log_before_model
  ├─ LLM 推理（看到 ToolMessage 中的工具结果）
  ├─ 输出：最终回答 "推荐智扫 X3 Ultra，因为..."
  └─ yield update = {"messages": [..., AIMessage(content="推荐...")]}（回答）
  ↓
你的循环体：
  last = messages[-1]          → AIMessage(content="推荐...")
  last.type == "ai" ✓
  getattr(last, "tool_calls", None) → None ✓ → 是最终回答
  last.content 有内容 ✓
  full += "推荐智扫 X3 Ultra..."
  yield "推荐智扫 X3 Ultra..."   ← 这里才往外面（前端）吐数据
  ↓
第 4 次循环结束（LLM 无 tool_calls，Agent 停止）
        '''
        async for update in agent.astream(inputs, stream_mode="values"):
            messages = update.get("messages", [])
            if messages:
                last = messages[-1]
                if last.type == "ai" and not getattr(last, "tool_calls", None):
                    if last.content:
                        full += last.content
                        yield last.content

        self._save_message("human", query)
        self._save_message("ai", full)
        self.conversation_history.append(HumanMessage(content=query))
        self.conversation_history.append(AIMessage(content=full))
        MAX_HISTORY = 4
        if len(self.conversation_history) > MAX_HISTORY * 2:
            self.conversation_history = self.conversation_history[-(MAX_HISTORY * 2):]

    def clear_history(self):
        self.conversation_history.clear()
        conn = get_connection()
        conn.execute("DELETE FROM conversations WHERE session_id = ?", (self.session_id,))
        conn.commit()
        conn.close()

if __name__ == '__main__':
    # 本地联调测试逻辑
    async def run_main():
        agent = ReactAgent()
        SERVER_COMMAND = sys.executable


        # 无论从哪个盘符、哪个目录下触发执行该命令，计算出的绝对路径都是死钉在 agent/tools 目录下的
        current_file_abs = os.path.abspath(__file__)
        current_dir_abs = os.path.dirname(current_file_abs)
        tools_dir_abs = os.path.normpath(os.path.join(current_dir_abs, "tools"))

        SERVER_SCRIPT_NAME = "mcp_server.py"

        print(f"[联调日志] 正在安全拉起 MCP 服务端...")
        async with ReactMCPContextManager(command=SERVER_COMMAND, args=[SERVER_SCRIPT_NAME],
                                          script_dir=tools_dir_abs) as mcp_mgr:
            query_task = "大户型适合那些扫地机器人"
            print(f"[Client 发起任务]: {query_task}\n[流式响应中]: ")
            async for chunk in agent.execute_stream(query_task, mcp_manager=mcp_mgr):
                print(chunk, end="", flush=True)
        print("\n[任务执行完毕]")


    asyncio.run(run_main())