"""混合检索 —— BM25 稀疏通道 + 向量稠密通道，RRF 加权融合。

设计（呼应 重排.md 第十四章）：
- 两路并行召回：向量（vector_store.search，含权限过滤）+ BM25（bm25_index.search_bm25）。
- RRF 融合：score(d) = Σ weight_i × 1/(RRF_K + rank_i(d))，只用排名不用绝对分数，
  回避「余弦分数与 BM25 分数不可比」的问题。
- 融合后候选集直接交给现有 rerank() 精排（本模块不做精排，重排层零改动）。

降级：BM25 不可用（依赖缺失/空库）→ 自动退回纯向量；总开关 HYBRID_ENABLED 可全局关闭。
"""

import sys

from config import (
    HYBRID_ENABLED, RRF_K, BM25_RRF_WEIGHT, VECTOR_RRF_WEIGHT,
    BM25_TOP_K, VECTOR_TOP_K, TOP_K,
)


def _log(msg):
    print(f"   🔀 混合检索: {msg}", file=sys.stderr, flush=True)


def _doc_key(doc):
    """稳定去重键：优先 chunk_id，退化用内容前缀。"""
    cid = (doc.metadata or {}).get("chunk_id")
    if cid:
        return f"id::{cid}"
    return f"txt::{doc.page_content[:120]}"


def _rrf_fuse(vec_docs, bm25_docs, top_k):
    """RRF 加权融合两路排名，返回融合后 top_k 的 Document 列表。"""
    scores = {}   # key -> 累计 RRF 分
    doc_map = {}  # key -> Document（首次出现即存）

    for rank, doc in enumerate(vec_docs):
        k = _doc_key(doc)
        scores[k] = scores.get(k, 0.0) + VECTOR_RRF_WEIGHT * (1.0 / (RRF_K + rank))
        doc_map.setdefault(k, doc)

    for rank, doc in enumerate(bm25_docs):
        k = _doc_key(doc)
        scores[k] = scores.get(k, 0.0) + BM25_RRF_WEIGHT * (1.0 / (RRF_K + rank))
        doc_map.setdefault(k, doc)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [doc_map[k] for k, _ in ranked[:top_k]]


def hybrid_search(query: str, top_k: int = None, kb_groups: list = None):
    """混合检索统一入口。

    返回 list[Document]。
    - HYBRID_ENABLED=False 或 BM25 无结果 → 退回纯向量检索。
    - 向量检索本身失败会向上抛（由调用方按原逻辑处理），保持与 search() 行为一致。
    - kb_groups 透传给向量通道做权限过滤（None=不限权限，不过滤）。
    """
    top_k = top_k or TOP_K
    from vector_store import search  # 延迟导入，避免循环依赖

    # 向量通道（含权限过滤，复用现有 search）
    vec_docs = search(query, top_k=max(VECTOR_TOP_K, top_k), kb_groups=kb_groups)

    if not HYBRID_ENABLED:
        return vec_docs[:top_k]

    # BM25 通道（安全降级：异常/空库返回 []）
    try:
        from bm25_index import search_bm25
        # 同样透传 kb_groups，BM25 通道与向量通道权限一致（防跨组泄露）
        bm25_docs = search_bm25(query, top_k=BM25_TOP_K, kb_groups=kb_groups)
    except Exception as e:
        _log(f"BM25 不可用，退回纯向量: {e}")
        bm25_docs = []

    if not bm25_docs:
        return vec_docs[:top_k]

    fused = _rrf_fuse(vec_docs, bm25_docs, top_k)
    _log(f"向量 {len(vec_docs)} + BM25 {len(bm25_docs)} → RRF 融合 {len(fused)} 条")
    return fused


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("=== 混合检索自测 ===")
    for q in ["小蓝的身高体重是多少", "ISAC 是什么", "物理层安全"]:
        hits = hybrid_search(q, top_k=5)
        print(f"\n查询: {q} → {len(hits)} 条")
        for i, d in enumerate(hits, 1):
            print(f"  [{i}] {d.page_content[:55]}...")
    print("\n🎉 混合检索自测完成")
