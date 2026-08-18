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


# ── 工具权限声明 + 启动期校验（P4 工具鉴权）──

def register_tool(required_perms, name=None):
    """注册工具并声明权限。required_perms 必填（开发时忘配 → TypeError）。

    通过 @mcp.tool(meta={"required_perms": ...}) 把权限声明进 MCP 元数据，
    主进程 ToolRegistry 据此做工具级鉴权（["*"] = 公开）。
    """
    def decorator(func):
        tool_name = name or func.__name__
        return mcp.tool(name=tool_name, meta={"required_perms": required_perms})(func)
    return decorator


def _validate_tool_permissions():
    """启动期校验：扫描所有注册工具，未声明 required_perms 直接启动报错。

    把「运行时 Agent 调用失败」提前到「启动阶段」——新增工具忘配权限，
    程序直接跑不起来。风险②：公开工具打 warning 提醒确认是否需精细化。
    """
    registered = mcp._tool_manager.list_tools()
    missing = [info.name for info in registered
               if not (getattr(info, 'meta', None) and info.meta.get("required_perms"))]
    if missing:
        raise RuntimeError(
            f"[启动检查] 以下工具缺少权限配置，请补充 required_perms: {missing}"
        )
    for info in registered:
        perms = info.meta.get("required_perms") if getattr(info, 'meta', None) else None
        if perms == ["*"]:
            print(f"   ⚠️ 工具 {info.name} 声明为公开(['*'])，确认是否需要精细化权限",
                  file=sys.stderr, flush=True)


@register_tool(required_perms=["*"])
async def query_weather(city: str) -> str:
    """
    查询指定城市的实时天气信息。
    :param city: 城市名称（中文或英文，如 "Beijing" 或 "北京"）
    """
    data = await fetch_weather_data(city)
    if data is None:
        return "无法获取天气数据，请稍后再试。"
    return format_weather(data)


@register_tool(required_perms=["*"])
async def ask_knowledge_base(query: str, kb_groups: list = None) -> str:
    """
    向私有知识库提问，获取基于已上传文档的智能回答。

    :param query: 用户问题（中英文均可）
    :param kb_groups: 用户可访问的知识库分组列表（如 ["dept_rd"]）。None 表示不限权限（管理员），空列表表示仅公开文档。由主进程从登录态解析后透传，子进程无状态、不缓存。
    """
    if not GROQ_API_KEY:
        return "错误: 未配置 GROQ_API_KEY，请在 .env 文件中设置。"
    try:
        return await asyncio.to_thread(answer_with_fallback, query, TOP_K, None, kb_groups)
    except Exception as e:
        print(f"[MCP] 问答异常: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return f"问答处理出错: {type(e).__name__}: {e}"


@register_tool(required_perms=["*"])
async def search_knowledge_base(query: str, top_k: int = TOP_K,
                                kb_groups: list = None) -> str:
    """
    在知识库中执行语义搜索，只返回最相关的文档片段原文（不经 LLM 处理）。
    适合查看原始检索结果或调试检索质量。

    :param query: 搜索关键词（中英文均可）
    :param top_k: 返回的片段数量，默认 8
    :param kb_groups: 用户可访问的知识库分组列表。None 表示不限权限（管理员），空列表表示仅公开文档。
    """
    try:
        docs = await asyncio.to_thread(search, query, top_k, kb_groups)
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


@register_tool(required_perms=["*"])
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


@register_tool(required_perms=["*"])
async def debug_rerank(query: str, top_k: int = TOP_K,
                       kb_groups: list = None) -> str:
    """
    调试工具：对比重排前后的检索结果。用于评估重排效果。

    返回：初召回片段数 + 重排后精选片段数 + 排名变化 + 得分。
    适合在 MCP Inspector 中直观观察重排对检索精度的影响。

    :param query: 搜索关键词（中英文均可）
    :param top_k: 每次检索返回的片段数量，默认 8
    :param kb_groups: 用户可访问的知识库分组列表。None 表示不限权限（管理员），空列表表示仅公开文档。
    """
    from retriever import _translate_query_for_search
    from reranker import rerank, _build_rerank_text
    import os as _os

    # 1. 中文 + 英文双语检索（透传 kb_groups，与正式检索一致）
    docs_cn = await asyncio.to_thread(search, query, top_k, kb_groups)
    docs_en = []
    try:
        en_query = await asyncio.to_thread(_translate_query_for_search, query)
        if en_query:
            docs_en = await asyncio.to_thread(search, en_query, top_k, kb_groups)
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


@register_tool(required_perms=["manage_users"])
async def clear_memory() -> str:
    """清空当前会话的对话记忆，之后的问题将不再有历史上下文。"""
    return "对话记忆已清空。（记忆由智能体统一管理，此操作已通知智能体）"


# ═══════════════════════════════════════════════
# 河海大学教务系统工具
# ═══════════════════════════════════════════════

_edu_session = None

def _get_edu_session():
    global _edu_session
    if _edu_session is None:
        from tools_edu import HuleSession
        _edu_session = HuleSession()
        if not _edu_session.check_session():
            _edu_session.login()
    return _edu_session


@register_tool(required_perms=["*"])
async def edu_query_schedule(week: str = "", semester: str = "") -> str:
    """查询河海大学课表。

    :param week: 第几周(1-20),留空=当前周(暑假建议指定,如'8')
    :param semester: 学期ID(如'2024-2025-2'为春季,'2024-2025-1'为秋季),留空=当前学期(暑假无课)
    :return: 按周几+节次排列的课程列表(课程名/教师/周次/教室)
    """
    edu = _get_edu_session()
    if not edu.check_session():
        ok = edu.login()
        if not ok:
            return "❌ 教务系统登录失败，请检查学号密码配置"
    return await asyncio.to_thread(edu.fetch_schedule, xnxq01id=semester, zc=week)


if __name__ == "__main__":
    sys.stdout = _original_stdout  # 恢复 stdout，MCP 协议需要它
    _validate_tool_permissions()   # 启动期工具权限校验（忘配 required_perms → 启动报错）
    mcp.run(transport="stdio")




