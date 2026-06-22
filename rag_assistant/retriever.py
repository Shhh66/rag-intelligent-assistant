"""RAG 检索增强生成模块 —— 检索 + 生成回答"""

import sys
from openai import OpenAI
from config import GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL, TOP_K
from vector_store import search


def _translate_query_for_search(query: str) -> str:
    """将中文查询翻译为英文关键词，提升英文文档检索命中率"""
    client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=30.0)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{
            "role": "user",
            "content": f"将以下中文问题翻译为适合英文文档检索的英文关键词（5-10个词即可）：\n\n{query}\n\n只输出英文关键词，不要解释。"
        }],
        temperature=0,
        max_tokens=50,
    )
    en_keywords = resp.choices[0].message.content.strip()
    print(f"   🌐 英文检索词: {en_keywords}", file=sys.stderr)
    return en_keywords


def build_prompt(query: str, retrieved_docs: list) -> str:
    """构建注入检索结果的 Prompt，让 LLM 自主判断检索结果是否相关"""
    # 拼接检索到的文档片段
    context_parts = []
    for i, doc in enumerate(retrieved_docs, 1):
        context_parts.append(f"[参考片段 {i}]\n{doc.page_content}")
    context = "\n\n".join(context_parts)

    prompt = f"""你是一个智能知识库助手。请根据以下参考信息回答用户问题。

## 重要规则（务必遵守）
1. 优先基于参考信息回答，在末尾标注来源（如 [参考片段 1][参考片段 2]）
2. 如果参考信息部分相关，就基于相关部分回答，不清楚的地方如实说
3. 如果参考信息与用户问题**完全无关**（比如用户问"宇宙是什么"而参考信息全是通信技术文档），请**忽略参考信息，直接用你自己的知识正面回答用户问题**，并在末尾附上：
   > ⚠️ 本回答并非基于上传的知识库文档，由大模型直接生成。
   **注意：这种情况下，你必须给出实质性的回答内容，绝对不能说"无法回答"或"没有相关信息"。**
4. 但凡你在回答中引用了任何一个参考片段，就**不要**加第3条的免责声明。

## 参考信息
{context}

## 用户问题
{query}

## 你的回答"""
    return prompt



def answer_with_fallback(query: str, top_k: int = TOP_K) -> str:
    """统一入口：双语检索 → 合并去重 → 注入Prompt → LLM自主判断相关性并回答"""
    # 1. 中文 + 英文双语检索，合并去重
    docs_cn, docs_en = [], []
    db_error = False

    try:
        print(f"   🔍 中文检索: {query[:40]}...", file=sys.stderr)
        docs_cn = search(query, top_k=top_k)
        print(f"      找到 {len(docs_cn)} 个片段", file=sys.stderr)
    except Exception as e:
        db_error = True
        print(f"   ⚠️ 检索失败: {e}", file=sys.stderr)

    if not db_error:
        try:
            en_query = _translate_query_for_search(query)
            print(f"   🔍 英文检索: {en_query}", file=sys.stderr)
            docs_en = search(en_query, top_k=top_k)
            print(f"      找到 {len(docs_en)} 个片段", file=sys.stderr)
        except Exception as e:
            print(f"   ⚠️ 英文检索失败: {e}", file=sys.stderr)

    # 2. 无知识库或检索失败 → LLM 直接回答
    if db_error or (not docs_cn and not docs_en):
        print(f"   ⚠️ 知识库不可用，LLM 直接回答", file=sys.stderr)
        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=30.0)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": query}],
            temperature=0.7,
            max_tokens=2000,
        )
        answer = response.choices[0].message.content
        return answer + "\n\n> ⚠️ 本回答并非基于上传的知识库文档，由大模型直接生成。"

    # 3. 合并去重
    seen = set()
    merged = []
    for doc in docs_cn + docs_en:
        key = doc.page_content[:120]
        if key not in seen:
            seen.add(key)
            merged.append(doc)
    print(f"   📄 合并去重后共 {len(merged)} 个片段", file=sys.stderr)

    # 2. 构建 Prompt 并调用 LLM
    prompt = build_prompt(query, merged)

    client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=30.0)
    response = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=2000,
    )

    answer = response.choices[0].message.content
    return answer
