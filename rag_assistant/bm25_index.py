"""BM25 关键词检索索引 —— 混合检索的稀疏通道。

与 ChromaDB 同源：从向量库拉取全量 chunk 文本构建 BM25 索引。
用「签名」(chunk 数 + 内容指纹) 驱动懒重建：知识库变了才重建，否则加载持久化索引。

中文用 jieba 分词 + 停用词过滤，英文按空格；索引持久化到 pkl，避免每次全量重建。
所有对外函数在向量库不可用 / 依赖缺失时安全降级（返回空结果），绝不阻断主检索。
"""

import os
import re
import sys
import pickle
import hashlib

from config import (
    BM25_INDEX_PATH, BM25_STOPWORDS_PATH, BM25_TOP_K,
)

# ─────────────────────────────────────────────
# 全局缓存（进程内单例）
# ─────────────────────────────────────────────
_bm25 = None                 # BM25Okapi 实例
_bm25_docs = None            # list[langchain Document]，与 BM25 语料顺序一一对应
_bm25_signature = None       # 当前索引对应的知识库签名
_stopwords = None            # set[str]

_INDEX_ABS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          os.path.basename(BM25_INDEX_PATH))


def _log(msg):
    print(f"   🔤 BM25: {msg}", file=sys.stderr, flush=True)


# ─────────────────────────────────────────────
# 分词
# ─────────────────────────────────────────────
def _load_stopwords():
    """加载中文停用词表（不存在则返回空集，不过滤）。"""
    global _stopwords
    if _stopwords is not None:
        return _stopwords
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        os.path.basename(BM25_STOPWORDS_PATH))
    words = set()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                words = {line.strip() for line in f if line.strip()}
            _log(f"停用词 {len(words)} 条")
        except Exception as e:
            _log(f"停用词加载失败(忽略): {e}")
    _stopwords = words
    return words


def tokenize(text: str) -> list:
    """jieba 中文分词 + 英文按词，过滤停用词与纯标点/空白。"""
    import jieba
    stop = _load_stopwords()
    tokens = []
    for tok in jieba.lcut(text or ""):
        tok = tok.strip().lower()
        if not tok or tok in stop:
            continue
        # 过滤纯标点/空白（保留中英文与数字）
        if not re.search(r"[\w一-鿿]", tok):
            continue
        tokens.append(tok)
    return tokens


# ─────────────────────────────────────────────
# 语料拉取 & 签名
# ─────────────────────────────────────────────
def _fetch_all_chunks():
    """从 ChromaDB 拉全量 chunk，返回 list[Document]。库不存在时返回 []。"""
    try:
        from vector_store import load_vector_store
        from langchain_core.documents import Document
        vs = load_vector_store()
        data = vs.get(include=["documents", "metadatas"])
        docs = []
        for content, meta in zip(data.get("documents", []),
                                 data.get("metadatas", [])):
            docs.append(Document(page_content=content or "", metadata=meta or {}))
        return docs
    except Exception as e:
        _log(f"拉取语料失败: {e}")
        return []


def _compute_signature(docs) -> str:
    """知识库签名：chunk 数 + 内容前 64 字 + 权限元数据的 MD5。

    纳入 kb_group/visibility：权限变更（内容不变）时签名也变，
    驱动 BM25 索引重建，避免用旧 metadata 做权限过滤（两通道不一致）。
    """
    h = hashlib.md5()
    h.update(str(len(docs)).encode())
    for d in docs:
        h.update((d.page_content[:64]).encode("utf-8", errors="ignore"))
        m = d.metadata or {}
        h.update(str(m.get("kb_group", "")).encode("utf-8", errors="ignore"))
        h.update(str(m.get("visibility", "")).encode("utf-8", errors="ignore"))
    return h.hexdigest()


# ─────────────────────────────────────────────
# 索引构建 / 持久化 / 加载
# ─────────────────────────────────────────────
def _build_index(docs):
    """用 docs 构建 BM25Okapi，返回 (bm25, docs, tokenized)。"""
    from rank_bm25 import BM25Okapi
    tokenized = [tokenize(d.page_content) for d in docs]
    # 极端情况：某 chunk 分词后为空，补一个占位避免 BM25 空文档报错
    tokenized = [t if t else ["_empty_"] for t in tokenized]
    bm25 = BM25Okapi(tokenized)
    return bm25, docs, tokenized


def _save_index(sig, docs, tokenized):
    try:
        with open(_INDEX_ABS, "wb") as f:
            pickle.dump({"sig": sig, "docs": docs, "tokenized": tokenized}, f)
        _log(f"索引已持久化: {_INDEX_ABS}")
    except Exception as e:
        _log(f"索引持久化失败(忽略): {e}")


def _load_persisted(sig):
    """尝试加载 pkl；签名匹配才用，否则返回 None。"""
    if not os.path.exists(_INDEX_ABS):
        return None
    try:
        with open(_INDEX_ABS, "rb") as f:
            payload = pickle.load(f)
        if payload.get("sig") != sig:
            _log("签名不匹配，需重建")
            return None
        from rank_bm25 import BM25Okapi
        bm25 = BM25Okapi(payload["tokenized"])
        return bm25, payload["docs"]
    except Exception as e:
        _log(f"加载持久化索引失败: {e}")
        return None


def _ensure_index():
    """确保内存索引与当前知识库一致（懒加载 + 签名驱动重建）。

    Returns: (bm25, docs) 或 (None, None)（库为空/依赖缺失）。
    """
    global _bm25, _bm25_docs, _bm25_signature

    docs = _fetch_all_chunks()
    if not docs:
        _bm25, _bm25_docs, _bm25_signature = None, None, None
        return None, None

    sig = _compute_signature(docs)

    # 内存命中
    if _bm25 is not None and _bm25_signature == sig:
        return _bm25, _bm25_docs

    # 尝试持久化命中
    loaded = _load_persisted(sig)
    if loaded is not None:
        _bm25, _bm25_docs, _bm25_signature = loaded[0], loaded[1], sig
        _log(f"加载持久化索引（{len(_bm25_docs)} chunks）")
        return _bm25, _bm25_docs

    # 重建
    _log(f"重建索引（{len(docs)} chunks）...")
    bm25, docs, tokenized = _build_index(docs)
    _bm25, _bm25_docs, _bm25_signature = bm25, docs, sig
    _save_index(sig, docs, tokenized)
    return _bm25, _bm25_docs


# ─────────────────────────────────────────────
# 对外检索接口
# ─────────────────────────────────────────────
def _filter_by_permission(docs, kb_groups):
    """按 kb_groups 过滤文档，对齐 vector_store.search_with_permission 的 where 语义。

    kb_groups=[]    → 仅 public 文档
    kb_groups=[...] → 组内文档 + public 文档
    """
    def allowed(d):
        m = d.metadata or {}
        if m.get("visibility") == "public":
            return True
        return bool(kb_groups) and m.get("kb_group") in kb_groups
    return [d for d in docs if allowed(d)]


def search_bm25(query: str, top_k: int = None, kb_groups: list = None):
    """BM25 关键词检索，返回 list[Document]（降序）。失败/空库返回 []。

    kb_groups：权限分组（None=不限权限/不过滤，兼容旧行为）。
    """
    top_k = top_k or BM25_TOP_K
    try:
        bm25, docs = _ensure_index()
        if bm25 is None or not docs:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        scores = bm25.get_scores(q_tokens)
        ranked = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
        results = [d for d, s in ranked if s > 0]
        # 权限过滤（在 top_k 截断前，否则过滤后数量不足）
        if kb_groups is not None:
            results = _filter_by_permission(results, kb_groups)
        results = results[:top_k]
        _log(f"'{query[:20]}' → {len(results)} 条")
        return results
    except Exception as e:
        _log(f"检索异常(降级空结果): {e}")
        return []


def invalidate():
    """使内存索引失效（增删改后可调用，下次检索自动重建）。"""
    global _bm25, _bm25_docs, _bm25_signature
    _bm25, _bm25_docs, _bm25_signature = None, None, None


# ─────────────────────────────────────────────
# 自测
# ─────────────────────────────────────────────
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("=== BM25 索引自测 ===")
    for q in ["ISAC 核心理念", "小蓝 身高 体重", "physical layer security"]:
        hits = search_bm25(q, top_k=3)
        print(f"\n查询: {q} → {len(hits)} 条")
        for i, d in enumerate(hits, 1):
            print(f"  [{i}] {d.page_content[:60]}...")
    print("\n🎉 BM25 自测完成")
