import sys
_original_stdout = sys.stdout
sys.stdout = sys.stderr

import asyncio
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from Weather_search import fetch_weather_data, format_weather

from vector_store import search
from retriever import answer_with_fallback
from config import VECTOR_DB_PATH, TOP_K, GROQ_API_KEY, OPENWEATHER_API_KEY

load_dotenv()

# 预热：预加载嵌入模型，避免首次查询时阻塞（模型 420MB，加载需 5-15 秒）
print("[MCP Server] 预加载嵌入模型...", file=sys.stderr, flush=True)
try:
    from vector_store import get_embeddings
    get_embeddings()
    print("[MCP Server] 嵌入模型就绪", file=sys.stderr, flush=True)
except Exception as e:
    print(f"[MCP Server] 嵌入模型预加载失败: {e}", file=sys.stderr, flush=True)

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


@mcp.tool()
async def ask_knowledge_base(query: str) -> str:
    """
    向私有知识库提问，获取基于已上传文档的智能回答。

    :param query: 用户问题（中英文均可）
    """
    if not GROQ_API_KEY:
        return "错误: 未配置 GROQ_API_KEY，请在 .env 文件中设置。"
    try:
        return await asyncio.to_thread(answer_with_fallback, query)
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
async def debug_rerank(query: str, top_k: int = TOP_K) -> str:
    """
    调试工具：对比重排前后的检索结果。用于评估重排效果。

    返回：初召回片段数 + 重排后精选片段数 + 排名变化 + 得分。
    适合在 MCP Inspector 中直观观察重排对检索精度的影响。

    :param query: 搜索关键词（中英文均可）
    :param top_k: 每次检索返回的片段数量，默认 8
    """
    from retriever import _translate_query_for_search
    from reranker import rerank, _build_rerank_text
    import os as _os

    # 1. 中文 + 英文双语检索
    docs_cn = await asyncio.to_thread(search, query, top_k=top_k)
    docs_en = []
    try:
        en_query = await asyncio.to_thread(_translate_query_for_search, query)
        if en_query:
            docs_en = await asyncio.to_thread(search, en_query, top_k=top_k)
    except Exception as e:
        pass  # 英文检索失败不阻塞

    # 2. 合并去重
    seen = set()
    merged = []
    for doc in docs_cn + docs_en:
        key = doc.page_content[:120]
        if key not in seen:
            seen.add(key)
            merged.append(doc)

    pre_count = len(merged)

    # 3. 构建初召回预览
    lines = []
    lines.append(f"## 🔍 重排调试: {query}")
    lines.append("")
    lines.append(f"### 📥 初召回: {pre_count} 个片段")
    lines.append(f"中文检索: {len(docs_cn)} 条 | 英文检索: {len(docs_en)} 条 | 去重后: {pre_count} 条")
    lines.append("")
    lines.append("| # | 来源 | 内容预览 |")
    lines.append("|---|------|---------|")
    for i, doc in enumerate(merged[:10], 1):
        source = _os.path.basename(doc.metadata.get("source", "")) or "未知"
        preview = doc.page_content[:80].replace("\n", " ").replace("|", "/")
        lines.append(f"| {i} | {source} | {preview}... |")
    if pre_count > 10:
        lines.append(f"| ... | ... | *(共 {pre_count} 条，仅显示前 10)* |")

    # 4. 重排
    reranked = rerank(query, merged)
    post_count = len(reranked)

    # 5. 构建重排后预览
    lines.append("")
    lines.append(f"### 📤 重排后: {post_count} 个片段")
    lines.append("")
    lines.append("| # | 得分 | 排名变化 | 来源 | 内容预览 |")
    lines.append("|---|------|---------|------|---------|")
    for i, doc in enumerate(reranked):
        score = doc.metadata.get("rerank_score", 0)
        rank = doc.metadata.get("rerank_rank", i + 1)
        # 计算原始排名
        try:
            old_rank = merged.index(doc) + 1
            if old_rank > rank:
                change = f"↑{old_rank - rank}"
            elif old_rank < rank:
                change = f"↓{rank - old_rank}"
            else:
                change = "—"
        except ValueError:
            change = "NEW"
        source = _os.path.basename(doc.metadata.get("source", "")) or "未知"
        preview = doc.page_content[:80].replace("\n", " ").replace("|", "/")
        lines.append(f"| {rank} | {score:.2f} | {change} | {source} | {preview}... |")

    # 6. 统计摘要
    lines.append("")
    lines.append(f"### 📊 统计")
    lines.append(f"- 初召回: **{pre_count}** 条")
    lines.append(f"- 重排后送入 Prompt: **{post_count}** 条")
    lines.append(f"- 过滤掉: **{pre_count - post_count}** 条" if pre_count > post_count else "- 无过滤（候选数 ≤ 截断上限）")
    lines.append(f"- 压缩比: **{post_count}/{pre_count}** ({post_count/max(pre_count,1)*100:.0f}%)")

    return "\n".join(lines)


@mcp.tool()
async def clear_memory() -> str:
    """清空当前会话的对话记忆，之后的问题将不再有历史上下文。"""
    return "对话记忆已清空。（记忆由智能体统一管理，此操作已通知智能体）"


if __name__ == "__main__":
    sys.stdout = _original_stdout  # 恢复 stdout，MCP 协议需要它
    mcp.run(transport="stdio")




