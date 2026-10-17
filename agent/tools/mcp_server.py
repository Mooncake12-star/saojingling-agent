import os
import sys
'''
sys.platform      # 'win32'    → 解释器是在什么系统上编译运行的
sys.path          # 列表        → 解释器去哪找模块
sys.stdout        # 对象        → 解释器的输出口
sys.stdin         # 输入口      → Python 从外面收东西
sys.version       # '3.11.5'   → 解释器自己的版本
sys.argv          # 列表        → 解释器启动时收到的命令行参数
'''
import urllib.parse   #它是专门用来解析、拆解、拼接和编码 URL（网址）的工具箱。
import json
import base64
from mcp.server.fastmcp import FastMCP
import requests       #专门负责帮你去访问网页、下载数据，或者调用远程的 API 接口
import random
from typing import Optional

#确保项目真正的【工程根目录】被注入到 sys.path 中
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if project_root not in sys.path: sys.path.insert(0, project_root)

from utils.config_handler import agent_conf
from utils.path_tool import get_abs_path
from db.database import get_connection
from cache.redis_client import get_cache, set_cache
#导包的真实原理：包名为子目录，去python的路径列表找根目录，然后进行拼接
'''
你写：
from utils.config_handler import agent_conf
           ↓
Python 拆出模块名："utils.config_handler"
           ↓
Python 把点换成斜杠："utils/config_handler.py"
           ↓
Python 去 sys.path 列表里挨个翻：
  ["...\\agent\\tools",
   "...\\Lib",
   "...\\site-packages"]
           ↓
  "...\\agent\\tools\\utils\\config_handler.py"    ✗ 没有
  "...\\Lib\\utils\\config_handler.py"              ✗ 没有
  "...\\site-packages\\utils\\config_handler.py"    ✗ 没有
'''

# 强制固化标准流编码，彻底防止 Windows Stdio 传输中文时的管道闭合异常
if sys.platform == "win32":
    import io
    # sys.stdout.buffer → 底层二进制输出口
    # TextIOWrapper → 包一层 utf-8 编码器，写文本时自动编码为字节
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 初始化基础日志兜底
import logging

logger = logging.getLogger("MCP_Server")
logger.setLevel(logging.INFO)

# =====================================================================
#  全局初始化隔离：防止 RagSummarizeService 报错导致整站闪退
# =====================================================================
rag_service = None

try:
    from rag.rag_service import RagSummarizeService
    from utils.logger_handler import logger

    #强行在此处安全实例化，就算 RAG 找不到数据库文件，也只会打印错误，不会导致子进程死掉
    rag_service = RagSummarizeService()

    # 预加载 Reranker 模型（首次加载约 100s），让 API 调用秒级响应
    try:
        from rag.rag_service import _get_bge_reranker
        _reranker = _get_bge_reranker()
        if _reranker is not None:
            logger.info("[MCP Server] Reranker 模型预加载完成")
        else:
            logger.info("️[MCP Server] 无 Reranker 模型，使用 LLM 重排序降级方案")
    except Exception as e:
        logger.warning(f"[MCP Server] Reranker 预加载失败，调用时再重试: {e}")

    logger.info("[MCP Server] RagSummarizeService 核心服务成功拉起！")
except Exception as e:
    logger.error(f"[MCP Server 警告] 核心业务组件加载抛出异常，降级无RAG模式运行: {str(e)}")


# 创建 FastMCP 实例
mcp = FastMCP("ZhiSaoTong-Core-Service")

# =====================================================================
# 工具 1: 本地 RAG 知识库检索
# =====================================================================
@mcp.tool(description="从本地向量存储中检索参考资料，并针对用户的提问进行RAG总结回复。")
def rag_summarize(query: str) -> str:
    """
    通过本地知识库回答问题
    :param query: 用户的查询疑问
    """
    if not rag_service:
        return "⚠️ 错误：本地 RAG 服务由于内部路径或向量库加载异常未能成功就绪。"

    # 先查缓存
    cache_key = f"rag:{query}"
    cached = get_cache(cache_key)
    if cached:
        logger.info(f"[MCP Tool] 缓存命中: {query}")
        return cached

    try:
        logger.info(f"[MCP Tool] 收到本地知识库检索请求: {query}")
        result = rag_service.rag_summarize(query)
        set_cache(cache_key, result, ttl=600)  # 缓存 10 分钟
        return result
    except Exception as e:
        logger.error(f"[MCP Tool] 本地检索失败: {str(e)}")
        return f"检索本地知识库时发生错误: {str(e)}"


# =====================================================================
# 工具 2: 连接外部网络的工具
# =====================================================================
@mcp.tool(description="连接外部网络进行实时搜索，获取最新的网页、资讯或技术资料。当本地知识库没有相关信息时使用。")
def sougou_search(query: str) -> str:
    """
    通过外部网络执行在线搜索
    :param query: 需要搜索的关键词或短语
    """
    import httpx
    logger.info(f"[MCP Tool] 执行网络搜索: {query}")

    encoded_query = urllib.parse.quote(query)
    url = f"https://www.sogou.com/web?query={encoded_query}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    try:
        #follow_redirects：自动跟随跳转。有些网站会先返回一个"去这个新地址"的响应，这个参数让 httpx 自动去新地址拿数据
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            if response.status_code == 200:
                return f"已成功连接外部网站，搜索结果摘要如下：\n\n{response.text[:3000]}..."
            else:
                return f"外部网连接成功，但搜索服务返回了状态码: {response.status_code}"
    except Exception as e:
        return f"外部网络连接请求异常: {str(e)}"


# =====================================================================
# 工具 3: 基础天气查询工具
# =====================================================================
@mcp.tool(description="获取指定城市的天气，以消息字符串的形式返回。")
def get_current_weather(city: str) -> str:
    """
    获取指定城市的实时天气信息。
    当用户询问某个城市、地区的天气、温度、风向或是否下雨时，调用此工具。

    参数:
    city (str): 城市的名称，支持中文城市名（如 "北京"、"东莞"），暂不支持英文。
    """
    import urllib.parse
    encoded = urllib.parse.quote(city)
    url = f"http://wthrcdn.etouch.cn/weather_mini?city={encoded}"

    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("status") != 1000:
                return f"无法获取【{city}】的天气信息：{data.get('desc', '未知错误')}"
            d = data["data"]
            forecast = d.get("forecast", [{}])[0]
            result = (
                f"【{city}】当前温度：{d.get('wendu', '未知')}°C\n"
                f"天气：{forecast.get('type', '未知')}\n"
                f"风向：{forecast.get('fengxiang', '未知')}（{forecast.get('fengli', '')}）\n"
                f"最高温：{forecast.get('high', '未知')}，最低温：{forecast.get('low', '未知')}\n"
                f"温馨提示：{d.get('ganmao', '')}"
            )
            return result
        return f"无法获取【{city}】的天气，接口返回状态码：{response.status_code}"
    except Exception as e:
        return f"获取【{city}】天气时发生异常: {str(e)}"



# =====================================================================
# 工具 4: 产品推荐
# =====================================================================
def _row_to_product(row):
    """把数据库行转成和原来 JSON 结构一致的 dict"""
    return {
        "id": row["id"],
        "brand": row["brand"],
        "name": row["name"],
        "type": row["type"],
        "price": row["price"],
        "specs": json.loads(row["specs"]) if row["specs"] else {},
        "features": json.loads(row["features"]) if row["features"] else [],
        "suitable_for": json.loads(row["suitable_for"]) if row["suitable_for"] else [],
        "rating": row["rating"],
    }

@mcp.tool(description="根据用户的户型、预算、需求推荐合适的扫地机器人/扫拖一体机型号，返回推荐结果。")
def product_recommend(
    house_size: str,
    budget: Optional[float] = None,
    floor_type: Optional[str] = None,
    has_pet: Optional[bool] = None,
    prefer_type: Optional[str] = None
) -> str:
    """
    推荐合适的产品型号
    :param house_size: 户型大小描述，如 "小户型（<90㎡）"、"中户型（90-140㎡）"、"大户型（>140㎡）"
    :param budget: 预算上限（元），可选
    :param floor_type: 地面类型，如 "瓷砖"、"木地板"、"混合地面"，可选
    :param has_pet: 是否有宠物，可选
    :param prefer_type: 偏好类型，如 "扫地机器人"、"扫拖一体机器人"，可选
    """
    conn = get_connection()
    rows = conn.execute("SELECT * FROM products").fetchall()
    conn.close()

    if not rows:
        return "产品数据库暂未就绪"

    products = [_row_to_product(r) for r in rows]

    scored = []
    for p in products:
        score = 0
        reasons = []

        # 户型匹配（suitable_for 是列表，直接用 in 判断）
        if house_size and house_size in p.get("suitable_for", []):
            score += 3
            reasons.append(f"适合{house_size}")

        # 类型匹配
        if prefer_type and prefer_type in p.get("type", ""):
            score += 2
            reasons.append(f"类型匹配：{prefer_type}")

        # 预算匹配
        if budget and p.get("price", 0) <= budget:
            score += 2
            reasons.append(f"价格{int(p['price'])}元在预算内")
        elif budget and p.get("price", 0) > budget * 1.2:
            score -= 1

        # 地面类型匹配
        if floor_type and floor_type in str(p.get("suitable_for", [])):
            score += 1
            reasons.append(f"适用{floor_type}")

        # 宠物家庭
        if has_pet and "宠物" in str(p.get("suitable_for", [])):
            score += 2
            reasons.append("适合宠物家庭")

        scored.append((score, p, reasons))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [s for s in scored if s[0] > 0][:3] or scored[:2]

    lines = ["为您推荐以下产品：\n"]
    for s, p, reasons in top:
        lines.append(
            f"【{p['name']}】¥{p['price']}  评分{p['rating']}\n"
            f"  吸力：{p['specs'].get('suction', 'N/A')}  导航：{p['specs'].get('navigation', 'N/A')}\n"
            f"  推荐理由：{'、'.join(reasons) if reasons else '综合评分较高'}\n"
        )
    return "\n".join(lines)


# =====================================================================
# 工具 5: 价格查询
# =====================================================================
@mcp.tool(description="查询指定产品的参考价格。返回该产品的官方指导价。")
def price_query(product_name: str) -> str:
    """
    查询产品参考价格
    :param product_name: 产品名称，如 "智扫 X1 Pro"、"X2 Max"
    """
    conn = get_connection()
    rows = conn.execute("SELECT * FROM products").fetchall()
    conn.close()

    if not rows:
        return "产品数据库暂未就绪"

    products = [_row_to_product(r) for r in rows]
    matches = [p for p in products if product_name.lower() in p["name"].lower() or product_name.lower() in p["id"].lower()]
    if not matches:
        all_names = "\n".join(f"  - {p['name']}（¥{p['price']}）" for p in products)
        return f"未找到产品「{product_name}」，可选产品：\n{all_names}"

    lines = []
    for p in matches:
        lines.append(
            f"【{p['name']}】\n"
            f"  类型：{p['type']}\n"
            f"  官方指导价：¥{p['price']}\n"
            f"  主要参数：吸力{p['specs'].get('suction', 'N/A')}、{p['specs'].get('navigation', 'N/A')}\n"
            f"  评分：{p['rating']}/5.0\n"
        )
    return "\n".join(lines)


# =====================================================================
# 工具 6: 图片分析（多模态）
# =====================================================================
@mcp.tool(description="分析用户上传的故障照片或产品图片，识别问题并给出处理建议。传入图片的本地文件路径。用户上传的图片会保存到 uploads/ 目录下。")
def image_analyze(file_path: str, question: str = "请分析这张图片中的问题是什么") -> str:
    """
    分析图片内容
    :param file_path: 图片文件的本地绝对路径
    :param question: 针对图片的问题描述
    """
    if not os.path.exists(file_path):
        return f"图片文件不存在：{file_path}"
    try:
        import base64
        from dashscope import MultiModalConversation
        from utils.env_utils import DASHSCOPE_API_KEY

        with open(file_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("utf-8")

        ext = os.path.splitext(file_path)[1].lower()
        mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(ext.lstrip("."), "jpeg")

        messages = [{
            "role": "user",
            "content": [
                {"image": f"data:image/{mime};base64,{b64}"},
                {"text": question}
            ]
        }]
        response = MultiModalConversation.call(
            model="qwen-vl-max",
            messages=messages,
            api_key=DASHSCOPE_API_KEY
        )
        if response.status_code == 200:
            return response.output.choices[0].message.content[0]["text"]
        return f"图片分析失败：{response.message}"
    except ImportError:
        return "图片分析功能不可用：缺少 dashscope 依赖"
    except Exception as e:
        return f"图片分析异常：{str(e)}"


# =====================================================================
# 工具 7: 导出报告 PDF
# =====================================================================
@mcp.tool(description="将对话分析和产品推荐结果导出为 PDF 报告文件，返回报告文件路径。")
def export_report(
    title: str,
    content: str,
    filename: Optional[str] = None,
) -> str:
    """
    导出 PDF 报告
    :param title: 报告标题
    :param content: 报告正文内容（支持多行文本）
    :param filename: 导出文件名（不含后缀），默认自动生成
    """
    try:
        from fpdf import FPDF
        from datetime import datetime

        font_path = get_abs_path("fonts/SimSun.ttf")
        pdf = FPDF()
        pdf.add_page()

        if os.path.exists(font_path):
            pdf.add_font("CN", "", font_path, uni=True)
            pdf.set_font("CN", "", 16)
            pdf.cell(0, 15, title, align="C")
            pdf.ln(20)
            pdf.set_font("CN", "", 12)
            date_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            pdf.cell(0, 10, f"生成时间：{date_str}")
            pdf.ln(15)
            pdf.set_font("CN", "", 11)
            for line in content.split("\n"):
                line = line.strip()
                if not line:
                    pdf.ln(5)
                    continue
                pdf.multi_cell(0, 8, line)
                pdf.ln(2)
        else:
            pdf.set_font("Helvetica", "", 16)
            pdf.cell(0, 15, "[ZhiSaoTong Report]", align="C")
            pdf.ln(20)
            pdf.set_font("Helvetica", "", 10)
            pdf.cell(0, 10, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
            pdf.ln(15)
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 8, content.encode("ascii", errors="replace").decode())
            pdf.ln(10)
            pdf.cell(0, 10, "(Chinese text requires SimSun.ttf in project fonts/ directory)")

        reports_dir = get_abs_path("logs/reports")
        os.makedirs(reports_dir, exist_ok=True)
        name = filename or f"report_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        pdf_path = os.path.join(reports_dir, f"{name}.pdf")
        pdf.output(pdf_path)
        logger.info(f"[MCP Tool] 报告已导出: {pdf_path}")
        return f"报告已成功导出：{pdf_path}"
    except ImportError:
        return "PDF导出功能不可用：缺少 fpdf2 库"
    except Exception as e:
        return f"PDF导出异常：{str(e)}"


if __name__ == "__main__":
    # 唤起服务端 Stdio 监听
    mcp.run()