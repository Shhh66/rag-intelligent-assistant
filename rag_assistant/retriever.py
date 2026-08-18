"""RAG 检索增强生成模块 —— 检索 + 生成回答"""

import sys
from openai import OpenAI
from config import GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL, TOP_K, HYBRID_ENABLED, SEARCH_CACHE_ENABLED
from vector_store import search
from token_tracker import get_tracker


def _search(query: str, top_k: int = TOP_K, kb_groups: list = None):
    """检索入口：HYBRID_ENABLED 时走混合检索(BM25+向量+RRF)，否则纯向量。

    混合检索模块任何异常都降级回纯向量 search()，绝不阻断主链路。
    kb_groups 透传给底层做权限过滤（None=不限权限，不过滤）。
    """
    if HYBRID_ENABLED:
        try:
            from hybrid_retriever import hybrid_search
            return hybrid_search(query, top_k=top_k, kb_groups=kb_groups)
        except Exception as e:
            print(f"   ⚠️ 混合检索降级纯向量: {e}", file=sys.stderr)
    return search(query, top_k=top_k, kb_groups=kb_groups)

def _call_llm(messages, temperature, max_tokens, call_site, timeout=30.0):
    """统一 DeepSeek 调用入口（子进程）：统一超时 + token 埋点。

    不做熔断——子进程是 per-request 的（每次查询新建，见 unified_agent._run 的
    stdio_client 生命周期），熔断器状态无法跨请求累积，在这里形同虚设。
    单次请求内的失败由 answer_with_fallback 的 fallback 兜底（检索失败→直答、
    翻译失败→跳过英文检索）。熔断器放在主进程的决策引擎（decision_engine）。
    """
    client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=timeout)
    resp = client.chat.completions.create(
        model=LLM_MODEL, messages=messages,
        temperature=temperature, max_tokens=max_tokens,
    )
    get_tracker().record(LLM_MODEL, resp.usage, call_site=call_site)
    return resp


def _translate_query_for_search(query: str) -> str:
    """将中文查询翻译为英文关键词，提升英文文档检索命中率。

    注意：LLM_MODEL(deepseek-v4-flash) 是推理模型，会先输出 reasoning_content，
    max_tokens 过小会导致正式 content 为空。故给足 512 token 空间。
    """
    resp = _call_llm(
        messages=[{
            "role": "user",
            "content": f"将以下中文问题翻译为适合英文文档检索的英文关键词（5-10个词即可）：\n\n{query}\n\n只输出英文关键词，不要解释。"
        }],
        temperature=0, max_tokens=512, call_site="retriever.translate_query",
    )
    en_keywords = resp.choices[0].message.content.strip()
    print(f"   🌐 英文检索词: {en_keywords}", file=sys.stderr)
    return en_keywords


def build_prompt(query: str, retrieved_docs: list) -> str:
    """构建注入检索结果的 Prompt，让 LLM 自主判断检索结果是否相关"""
    import os as _os

    # 拼接检索到的文档片段，注入文件名和页码出处
    context_parts = []
    for i, doc in enumerate(retrieved_docs, 1):
        # 提取文件名（去除路径）
        source_path = doc.metadata.get('source', '')
        filename = _os.path.basename(source_path) if source_path else '未知文档'
        # page_content 已由 text_splitter 注入 [第X页 - Section ...] 前缀
        context_parts.append(
            f"[参考片段 {i}] 来源：{filename}\n{doc.page_content}"
        )
    context = "\n\n".join(context_parts)

    prompt = f"""你是一个智能知识库助手。请根据以下参考信息回答用户问题。

## 重要规则（务必遵守）
1. 优先基于参考信息回答，回答中引用具体数据、事实时要标注出处，格式为：
   `（来源：文件名，第X页）` 或 `（来源：文件名）`，让读者能快速定位原文。
   例如："人工智能的定义是……（来源：AI入门.pdf，第3页）"
2. 在回答末尾，列出本次引用到的所有来源，格式：
   > 📚 参考来源：
   > - AI入门.pdf, 第3页
   > - 机器学习笔记.docx
3. 如果参考信息部分相关，就基于相关部分回答，不清楚的地方如实说
4. 如果参考信息与用户问题**完全无关**（比如用户问"宇宙是什么"而参考信息全是通信技术文档），请**忽略参考信息，直接用你自己的知识正面回答用户问题**，并在末尾附上：
   > ⚠️ 本回答并非基于上传的知识库文档，由大模型直接生成。
   **注意：这种情况下，你必须给出实质性的回答内容，绝对不能说"无法回答"或"没有相关信息"。**
5. 但凡你在回答中引用了任何一个参考片段，就**不要**加第4条的免责声明。

## 参考信息
{context}

## 用户问题
{query}

## 你的回答"""
    return prompt



def answer_with_fallback(query: str, top_k: int = TOP_K, history: list = None,
                         kb_groups: list = None) -> str:
    """统一入口：查询改写 → 混合检索(双语) → 合并去重 → 重排 → 注入Prompt → LLM回答

    kb_groups：显式传入时做请求级权限过滤（无串台）；None 时不限权限（不过滤）。
    query 改写与混合检索均由 config 开关控制，失败自动降级。
    """
    # 检索缓存：命中则跳过「改写 + 检索 + 翻译 + 合并」，直接用缓存的 merged
    merged = None
    if SEARCH_CACHE_ENABLED:
        try:
            from search_cache import get_cache
            cached = get_cache().get(query, kb_groups)
            if cached:
                merged = cached
                print(f"   ⚡ 检索缓存命中（Redis），跳过检索", file=sys.stderr)
        except Exception as e:
            print(f"   ⚠️ 缓存查询失败(忽略): {e}", file=sys.stderr)

    if merged is None:
        # 0. 查询改写（clarify：规范化+指代消解），失败无感回退原 query
        search_query = query
        try:
            from query_rewriter import rewrite_query
            rewritten = rewrite_query(query, history=history, mode="clarify")
            if isinstance(rewritten, str) and rewritten.strip():
                search_query = rewritten
        except Exception as e:
            print(f"   ⚠️ 查询改写跳过: {e}", file=sys.stderr)

        # 检索用改写后的 query；原 query 仍用于重排与 Prompt（保留用户真实意图）
        # 1. 中文 + 英文双语检索（混合检索：BM25+向量），合并去重
        docs_cn, docs_en = [], []
        db_error = False

        try:
            print(f"   🔍 中文检索: {search_query[:40]}...", file=sys.stderr)
            docs_cn = _search(search_query, top_k=top_k, kb_groups=kb_groups)
            print(f"      找到 {len(docs_cn)} 个片段", file=sys.stderr)
        except Exception as e:
            db_error = True
            print(f"   ⚠️ 检索失败: {e}", file=sys.stderr)

        if not db_error:
            try:
                en_query = _translate_query_for_search(search_query)
                if en_query and en_query.strip():
                    print(f"   🔍 英文检索: {en_query}", file=sys.stderr)
                    docs_en = _search(en_query, top_k=top_k, kb_groups=kb_groups)
                    print(f"      找到 {len(docs_en)} 个片段", file=sys.stderr)
            except Exception as e:
                print(f"   ⚠️ 英文检索失败: {e}", file=sys.stderr)

        # 2. 无知识库或检索失败 → LLM 直接回答
        if db_error or (not docs_cn and not docs_en):
            print(f"   ⚠️ 知识库不可用，LLM 直接回答", file=sys.stderr)
            response = _call_llm(
                messages=[{"role": "user", "content": query}],
                temperature=0.7, max_tokens=4000, call_site="retriever.direct_answer",
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

        # 写缓存（命中后下次跳过检索）
        if SEARCH_CACHE_ENABLED and merged:
            try:
                get_cache().set(query, kb_groups, merged)
            except Exception as e:
                print(f"   ⚠️ 缓存写入失败(忽略): {e}", file=sys.stderr)

    # 3.5 重排：精排候选片段，提升相关性
    from reranker import rerank
    merged = rerank(query, merged)

    # 4. 构建 Prompt 并调用 LLM
    prompt = build_prompt(query, merged)

    response = _call_llm(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=4000, call_site="retriever.rag_answer",
    )

    answer = response.choices[0].message.content
    return answer


def retrieve_and_answer(
    query: str,
    top_k: int = TOP_K,
    use_bilingual: bool = True,
    use_rerank: bool = True,
    use_hybrid: bool = False,
    use_rewrite: bool = False,
) -> tuple[str, list[str]]:
    """评测专用入口：检索 → (可选双语/混合/改写/重排) → 生成，返回答案与上下文。

    与 answer_with_fallback 的区别：额外返回 contexts（RAGAS 评测强制需要），
    并用开关控制 A/B 对比：
      - Baseline  ：use_bilingual=F, use_rerank=F, use_hybrid=F, use_rewrite=F（纯向量单路）
      - Optimized ：use_bilingual=T, use_rerank=T（双语 + BGE 重排）
      - use_hybrid=T ：检索走混合(BM25+向量+RRF)，可单独评估混合检索增益
      - use_rewrite=T：检索前做 query 改写(clarify)，可单独评估改写增益

    Returns:
        (answer, contexts)：contexts 为送入 Prompt 的片段文本列表（检索不到时为空列表）。
    """
    # 检索用 query（可选改写）；原 query 用于重排与 Prompt
    search_query = query
    if use_rewrite:
        try:
            from query_rewriter import rewrite_query
            r = rewrite_query(query, mode="clarify")
            if isinstance(r, str) and r.strip():
                search_query = r
        except Exception as e:
            print(f"   ⚠️ 改写跳过: {e}", file=sys.stderr)

    def _do_search(q, k):
        if use_hybrid:
            try:
                from hybrid_retriever import hybrid_search
                return hybrid_search(q, top_k=k)
            except Exception as e:
                print(f"   ⚠️ 混合检索降级: {e}", file=sys.stderr)
        return search(q, top_k=k)

    # 1. 中文检索（始终执行）
    docs_cn, docs_en = [], []
    try:
        print(f"   🔍 中文检索: {search_query[:40]}...", file=sys.stderr)
        docs_cn = _do_search(search_query, top_k)
        print(f"      找到 {len(docs_cn)} 个片段", file=sys.stderr)
    except Exception as e:
        print(f"   ⚠️ 检索失败: {e}", file=sys.stderr)
        return f"检索失败: {e}", []

    # 2. 英文检索（可选，A/B 开关）
    if use_bilingual:
        try:
            en_query = _translate_query_for_search(search_query)
            if en_query and en_query.strip():
                print(f"   🔍 英文检索: {en_query}", file=sys.stderr)
                docs_en = _do_search(en_query, top_k)
                print(f"      找到 {len(docs_en)} 个片段", file=sys.stderr)
            else:
                print(f"   ⏭️ 翻译为空，跳过英文检索", file=sys.stderr)
        except Exception as e:
            print(f"   ⚠️ 英文检索失败: {e}", file=sys.stderr)

    # 3. 合并去重
    seen = set()
    merged = []
    for doc in docs_cn + docs_en:
        key = doc.page_content[:120]
        if key not in seen:
            seen.add(key)
            merged.append(doc)

    if not merged:
        return "知识库中未检索到相关内容。", []

    # 4. 重排（可选，A/B 开关）
    if use_rerank:
        from reranker import rerank
        merged = rerank(query, merged)

    # 5. 构建 Prompt 并调用 LLM
    contexts = [doc.page_content for doc in merged]
    prompt = build_prompt(query, merged)
    response = _call_llm(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=4000, call_site="retriever.eval_answer", timeout=60.0,
    )
    answer = response.choices[0].message.content
    return answer, contexts
