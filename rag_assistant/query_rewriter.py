"""查询改写（Query Rewrite）—— 检索前用 LLM 优化用户提问，提升召回。

三种模式（config.QUERY_REWRITE_MODE）：
  clarify : 规范化 + 指代消解（口语→书面、补全指代实体），返回单条 query
  multi   : 生成 N 条等价 query，返回 list[str]
  hyde    : 生成假设答案文本，返回单条（供向量检索用）

设计要点：
  - 无感降级：任何失败/空返回/超时 → 回退原始 query，绝不阻断检索。
  - LRU 缓存：无历史的改写结果缓存（带历史的指代消解不缓存，历史一变结果就变）。
  - 埋点：记录「原 query → 改写后」映射，对齐 Bad Case 分析。
  - 指代消解只取最近 QUERY_REWRITE_COREF_ROUNDS 轮，压 Prompt、避免久远上下文干扰。
"""

import sys
from collections import OrderedDict

from openai import OpenAI
from config import (
    GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL,
    QUERY_REWRITE_ENABLED, QUERY_REWRITE_MODE, QUERY_REWRITE_MULTI_N,
    QUERY_REWRITE_MAX_TOKENS, QUERY_REWRITE_CACHE_SIZE, QUERY_REWRITE_COREF_ROUNDS,
)
from token_tracker import get_tracker

# ── LRU 缓存（无历史改写）──
_cache = OrderedDict()


def _log(msg):
    print(f"   ✍️ 查询改写: {msg}", file=sys.stderr, flush=True)


def _cache_get(key):
    if QUERY_REWRITE_CACHE_SIZE <= 0:
        return None
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    return None


def _cache_put(key, value):
    if QUERY_REWRITE_CACHE_SIZE <= 0:
        return
    _cache[key] = value
    _cache.move_to_end(key)
    while len(_cache) > QUERY_REWRITE_CACHE_SIZE:
        _cache.popitem(last=False)


def _client():
    return OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=30.0)


def _history_snippet(history, rounds):
    """取最近 rounds 轮对话拼成上下文文本。history: list[{role, content}]。"""
    if not history:
        return ""
    # 每轮≈user+assistant 两条，取尾部 rounds*2 条
    tail = history[-rounds * 2:]
    lines = []
    for m in tail:
        role = "用户" if m.get("role") == "user" else "助手"
        lines.append(f"{role}：{m.get('content', '')}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# 三种改写
# ─────────────────────────────────────────────
def _rewrite_clarify(query, history):
    """规范化 + 指代消解 → 单条 query。"""
    ctx = _history_snippet(history, QUERY_REWRITE_COREF_ROUNDS)
    if ctx:
        prompt = (
            f"以下是对话历史：\n{ctx}\n\n"
            f"用户当前提问：{query}\n\n"
            "请把「当前提问」改写成一个**适合知识库检索**的清晰问题："
            "补全指代（它/这个/那个指向的实体）、口语转书面、保留关键术语。"
            "只输出改写后的问题，不要解释。"
        )
    else:
        prompt = (
            f"请把下面的问题改写成一个**适合知识库检索**的清晰问题："
            f"口语转书面、补全省略、保留关键术语与实体。只输出改写后的问题，不要解释。\n\n{query}"
        )
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0, max_tokens=QUERY_REWRITE_MAX_TOKENS,
    )
    get_tracker().record(LLM_MODEL, resp.usage, call_site="query_rewrite.clarify")
    return resp.choices[0].message.content.strip()


def _rewrite_multi(query, history):
    """生成 N 条等价 query → list[str]（含原 query）。"""
    n = QUERY_REWRITE_MULTI_N
    prompt = (
        f"请从不同角度为下面的问题生成 {n} 个语义等价但表述不同的检索查询，"
        f"每行一个，不要编号、不要解释：\n\n{query}"
    )
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=QUERY_REWRITE_MAX_TOKENS,
    )
    get_tracker().record(LLM_MODEL, resp.usage, call_site="query_rewrite.multi")
    lines = [l.strip(" -·。.") for l in resp.choices[0].message.content.splitlines()]
    variants = [l for l in lines if l]
    # 轻量去重（能力④）：去掉与已有高度重叠的（前 12 字相同）
    seen, deduped = set(), []
    for v in [query] + variants:
        k = v[:12]
        if k not in seen:
            seen.add(k)
            deduped.append(v)
    return deduped[:n + 1]


def _rewrite_hyde(query, history):
    """HyDE：生成一段假设答案 → 单条文本。"""
    prompt = (
        f"请针对下面的问题，写一段 2~3 句、像知识库文档一样的**假设性答案**"
        f"（内容可以是你的推测，用于检索匹配）。只输出这段话：\n\n{query}"
    )
    resp = _client().chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3, max_tokens=QUERY_REWRITE_MAX_TOKENS,
    )
    get_tracker().record(LLM_MODEL, resp.usage, call_site="query_rewrite.hyde")
    return resp.choices[0].message.content.strip()


# ─────────────────────────────────────────────
# 对外统一入口
# ─────────────────────────────────────────────
def rewrite_query(query: str, history: list = None, mode: str = None):
    """检索前查询改写统一入口。

    Returns:
        clarify/hyde → str；multi → list[str]。
        任何失败或未启用 → 回退（clarify/hyde 返回原 query，multi 返回 [query]）。
    """
    mode = mode or QUERY_REWRITE_MODE
    fallback = [query] if mode == "multi" else query

    if not QUERY_REWRITE_ENABLED or not query or not query.strip():
        return fallback

    # 缓存：仅无历史时启用（带历史的指代消解结果不可缓存）
    cacheable = not history
    cache_key = f"{mode}::{query}"
    if cacheable:
        cached = _cache_get(cache_key)
        if cached is not None:
            _log(f"缓存命中 [{mode}]")
            return cached

    try:
        if mode == "multi":
            result = _rewrite_multi(query, history)
        elif mode == "hyde":
            result = _rewrite_hyde(query, history)
        else:
            result = _rewrite_clarify(query, history)

        # 空返回回退
        if not result or (isinstance(result, str) and not result.strip()):
            _log("改写为空，回退原 query")
            return fallback

        # 埋点：原 → 改写
        preview = result if isinstance(result, str) else " | ".join(result)
        _log(f"[{mode}] '{query[:24]}' → '{preview[:48]}'")

        if cacheable:
            _cache_put(cache_key, result)
        return result
    except Exception as e:
        _log(f"改写失败(回退原 query): {e}")
        return fallback


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("=== 查询改写自测 ===")
    print("\n[clarify 无历史]")
    print(rewrite_query("那个能量消耗问题到底咋回事", mode="clarify"))
    print("\n[clarify 带历史-指代消解]")
    hist = [{"role": "user", "content": "介绍一下 ISAC 技术"},
            {"role": "assistant", "content": "ISAC 是通信感知一体化……"}]
    print(rewrite_query("它有什么安全隐患？", history=hist, mode="clarify"))
    print("\n[multi]")
    print(rewrite_query("无人机低空信道建模难在哪", mode="multi"))
    print("\n🎉 查询改写自测完成")
