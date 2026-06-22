import sys
_original_stdout = sys.stdout
sys.stdout = sys.stderr

import asyncio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from Weather_search import fetch_weather_data, format_weather

from agent_core import AgentCore
from vector_store import search
from config import VECTOR_DB_PATH, TOP_K, GROQ_API_KEY, OPENWEATHER_API_KEY

load_dotenv()

mcp = FastMCP(
    name="AI Assistant",
    instructions="该服务提供天气查询和私有知识库问答能力，LLM 会根据用户问题自动选择合适的工具。",

)



@mcp.tool()
async def query_weather(city: str) -> str:
    """
    查询指定城市的实时天气信息。
    :param city: 城市名称（中文或英文，如 "Beijing" 或 "北京"）
    """
    data = await fetch_weather_data(city)
    if data is None:
        return "无法获取天气数据，请稍后再试。"
    return format_weather(data)


_agent = AgentCore()


@mcp.tool()
async def ask_knowledge_base(query: str) -> str:
    """
    向私有知识库提问，获取基于已上传文档的智能回答。
    支持多轮对话记忆（记住之前的问题和回答）。

    :param query: 用户问题（中英文均可）
    """
    if not GROQ_API_KEY:
        return "错误: 未配置 GROQ_API_KEY，请在 .env 文件中设置。"
    try:
        return await asyncio.to_thread(_agent.chat, query)
    except Exception as e:
        return f"问答处理出错: {type(e).__name__}: {e}"
    

@mcp.tool()
async def search_knowledge_base(query: str, top_k: int = TOP_K) -> str:
    """
    在知识库中执行语义搜索，只返回最相关的文档片段原文（不经 LLM 处理）。
    适合查看原始检索结果或调试检索质量。

    :param query: 搜索关键词（中英文均可）
    :param top_k: 返回的片段数量，默认 8
    """
    try:
        docs = await asyncio.to_thread(search, query, top_k=top_k)
    except FileNotFoundError as e:
        return f"知识库未构建: {e}"
    except Exception as e:
        return f"检索出错: {type(e).__name__}: {e}"

    if not docs:
        return "未找到相关文档片段。"

    parts = []
    for i, doc in enumerate(docs, 1):
        source = doc.metadata.get("source", "未知来源")
        parts.append(f"[片段 {i}] 来源: {source}\n{doc.page_content[:300]}\n")
    return "\n".join(parts)


@mcp.tool()
async def check_kb_status() -> str:
    """
    检查知识库状态：向量库是否就绪、片段数量、API Key 配置情况。
    """
    from pathlib import Path

    db_path = Path(VECTOR_DB_PATH)
    lines = []
    lines.append(f"Groq API Key: {'已配置' if GROQ_API_KEY else '未配置'}")
    lines.append(f"OpenWeather API Key: {'已配置' if OPENWEATHER_API_KEY else '未配置'}")
    lines.append(f"知识库路径: {VECTOR_DB_PATH}")

    if not db_path.exists() or not list(db_path.iterdir()):
        lines.append("状态: 未构建 — 请通过 Streamlit 应用上传文档构建知识库")
        return "\n".join(lines)

    def _check():
        import chromadb
        persistent_client = chromadb.PersistentClient(path=str(db_path))
        collection = persistent_client.get_collection("langchain")
        return collection.count()

    try:
        count = await asyncio.to_thread(_check)
        lines.append(f"文档片段数: {count}")
        lines.append("状态: 就绪")
    except Exception as e:
        lines.append(f"状态: 异常 ({e})")

    return "\n".join(lines)


@mcp.tool()
async def clear_memory() -> str:
    """清空当前会话的对话记忆，之后的问题将不再有历史上下文。"""
    await asyncio.to_thread(_agent.memory.clear)
    return "对话记忆已清空。"


if __name__ == "__main__":
    sys.stdout = _original_stdout  # 恢复 stdout，MCP 协议需要它
    mcp.run(transport="stdio")




