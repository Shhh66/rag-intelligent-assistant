"""benchmark_cache.py — 实测 Redis 检索缓存的真实收益

背景：简历写「Redis 缓存大幅削减高频检索耗时」，但无基线无数字。本脚本量化
「缓存命中 vs 未命中」的耗时差异，拿到真实数字。

原理（对齐 retriever.answer_with_fallback）：
- 缓存命中：跳过「查询改写 + 翻译 + 中英文检索 + 合并去重」，直接返回缓存的 merged。
- 重排(BGE) 与 LLM 生成是缓存命中后仍要执行的，不参与对比。

本脚本实测：
1. 冷缓存：完整检索段（改写 + 翻译 + 中英文检索 + 合并去重）的耗时
2. 热缓存：真实 Redis get 的耗时（search_cache.get = redis.get + 反序列化）

前提：知识库已就绪(26 chunks)、.env 已配 LLM key、Redis 已启动(localhost:6379)。
"""

import time
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import TOP_K
from retriever import _search, _translate_query_for_search
from query_rewriter import rewrite_query
from search_cache import get_cache

# 取自 rag_eval 评测集（覆盖简单事实/多跳/跨语言/边缘模糊）
QUERIES = [
    "6G低空无人机调研报告中，ISAC技术的核心理念是什么？",
    "什么是ISCC？它协同优化哪三个维度？",
    "低空无人机ISAC商业化面临哪些现有设计问题？请综合说明。",
    "What is ISAC in the context of 6G UAV networks?",
    "那个无人机报告里说的能量消耗问题到底是咋回事？",
]


def bench_cold(query):
    """冷缓存：完整检索段耗时（改写 + 翻译 + 中英文检索 + 合并去重）。"""
    t0 = time.perf_counter()

    # 0. 查询改写（clarify）
    search_query = query
    t_rewrite = 0.0
    try:
        tr = time.perf_counter()
        r = rewrite_query(query, history=None, mode="clarify")
        t_rewrite = time.perf_counter() - tr
        if isinstance(r, str) and r.strip():
            search_query = r
    except Exception as e:
        print(f"    [改写降级] {type(e).__name__}")

    # 1. 中文检索
    tr = time.perf_counter()
    docs_cn = _search(search_query, top_k=TOP_K, kb_groups=None)
    t_cn = time.perf_counter() - tr

    # 2. 翻译 + 英文检索
    t_translate = 0.0
    t_en = 0.0
    docs_en = []
    try:
        tr = time.perf_counter()
        en_query = _translate_query_for_search(search_query)
        t_translate = time.perf_counter() - tr
        if en_query and en_query.strip():
            tr = time.perf_counter()
            docs_en = _search(en_query, top_k=TOP_K, kb_groups=None)
            t_en = time.perf_counter() - tr
    except Exception as e:
        print(f"    [翻译/英文检索降级] {type(e).__name__}")

    # 3. 合并去重（对齐 answer_with_fallback）
    seen = set()
    merged = []
    for doc in docs_cn + docs_en:
        key = doc.page_content[:120]
        if key not in seen:
            seen.add(key)
            merged.append(doc)

    total = time.perf_counter() - t0
    return {
        "total": total,
        "rewrite": t_rewrite,
        "translate": t_translate,
        "search_cn": t_cn,
        "search_en": t_en,
        "n_docs": len(merged),
        "docs": merged,
    }


def bench_hot(query, kb_groups=None):
    """热缓存：真实 Redis get 的耗时（search_cache.get = redis.get + 反序列化）。"""
    t0 = time.perf_counter()
    cached = get_cache().get(query, kb_groups)
    dt = time.perf_counter() - t0
    return dt, cached is not None


def main():
    print("=== 检索缓存收益实测（真实 Redis）===")
    print(f"查询数: {len(QUERIES)} | 知识库: 26 chunks\n")

    # 预热：首个查询会触发 BM25 索引构建 / 混合检索首次加载，不计入统计
    print("[预热] 加载检索链路（不计入统计）...")
    bench_cold(QUERIES[0])
    print()

    cold_times, hot_times = [], []
    for i, q in enumerate(QUERIES, 1):
        print(f"[{i}] {q[:44]}")
        cold = bench_cold(q)
        # 写真实 Redis 缓存（对齐 answer_with_fallback 的 set）
        get_cache().set(q, None, cold["docs"])
        # 热检索：真实 Redis get
        hot, hit = bench_hot(q)
        cold_times.append(cold["total"])
        hot_times.append(hot)
        print(f"    冷·检索段 {cold['total']*1000:8.1f} ms  = 改写 {cold['rewrite']*1000:7.0f} + 翻译 {cold['translate']*1000:7.0f} + 中文检索 {cold['search_cn']*1000:6.0f} + 英文检索 {cold['search_en']*1000:6.0f}  ({cold['n_docs']} 片段)")
        print(f"    热·Redis get {hot*1000:8.2f} ms  (命中={hit})")
        print()

    avg_cold = sum(cold_times) / len(cold_times)
    avg_hot = sum(hot_times) / len(hot_times)
    saved = avg_cold - avg_hot

    print("=" * 60)
    print("汇总（缓存命中 vs 未命中的检索段耗时，真实 Redis）")
    print(f"  冷缓存·完整检索段 平均: {avg_cold*1000:8.1f} ms")
    print(f"  热缓存·Redis get    平均: {avg_hot*1000:8.2f} ms")
    print(f"  缓存省下（每查询）:      {saved*1000:8.1f} ms")
    print(f"  削减比例:                {(saved/avg_cold*100) if avg_cold else 0:6.1f}%")
    print("=" * 60)
    print("注意：以上是『检索段』耗时，不含重排(BGE)与 LLM 生成——这两步缓存命中后仍要执行。")


if __name__ == "__main__":
    main()
