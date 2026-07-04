"""重排模块 —— 精排候选文档片段，提升 RAG 检索精度

架构：向量检索（粗排，高召回）→ 本模块（精排，高精度）→ LLM 生成
插入点：retriever.py 合并去重后、Prompt 构建前
"""

import sys
import hashlib
import time
import re
import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from config import (
    RERANK_ENABLED, RERANK_STRATEGY, RERANK_TOP_K,
    RERANK_MAX_CANDIDATES, RERANK_MODEL,
    RERANK_MAX_TOKENS, RERANK_MIN_SCORE, RERANK_CALIBRATION_MAX_RATIO,
)


# ═══════════════════════════════════════════════════════════════
# 全局单例（懒加载）
# ═══════════════════════════════════════════════════════════════

_model = None
_tokenizer = None
_device = None

# Query 缓存
_cache = {}
_CACHE_TTL = 600       # 10 分钟
_CACHE_MAX = 50         # 最大缓存条数


# ═══════════════════════════════════════════════════════════════
# 设备与模型管理
# ═══════════════════════════════════════════════════════════════

def _get_device() -> str:
    """自动适配设备：GPU → MPS(Mac) → CPU"""
    global _device
    if _device is None:
        if torch.cuda.is_available():
            _device = "cuda"
        elif hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            _device = "mps"
        else:
            _device = "cpu"
    return _device


def _get_reranker():
    """全局单例，懒加载 BGE-Reranker，首次加载打印提示"""
    global _model, _tokenizer
    if _model is None:
        print("   ⏳ 正在加载重排模型（首次使用需下载 ~570MB，请稍候）...",
              file=sys.stderr, flush=True)
        _tokenizer = AutoTokenizer.from_pretrained(RERANK_MODEL)
        _model = AutoModelForSequenceClassification.from_pretrained(RERANK_MODEL)

        device = _get_device()
        if device == "cpu":
            try:
                _model = _model.half()  # fp16
                print("   📌 重排模型: CPU 半精度", file=sys.stderr, flush=True)
            except Exception:
                print("   📌 重排模型: CPU 全精度（设备不支持半精度）",
                      file=sys.stderr, flush=True)
        else:
            _model = _model.to(device)
            print(f"   📌 重排模型: {device.upper()} 加速", file=sys.stderr, flush=True)

        _model.eval()
        print("   ✅ 重排模型加载完成", file=sys.stderr, flush=True)
    return _tokenizer, _model


# ═══════════════════════════════════════════════════════════════
# 文本预处理
# ═══════════════════════════════════════════════════════════════

def _build_rerank_text(doc) -> str:
    """拼接标题路径 + 正文，让重排模型感知章节主题"""
    parts = [doc.metadata.get(k, '') for k in ('h1', 'h2', 'h3')
             if doc.metadata.get(k) and doc.metadata[k] != '文档前言']
    header = " > ".join(parts)
    if doc.metadata.get('h1') == '文档前言' and not header:
        header = "文档前言"
    return f"【{header}】{doc.page_content}" if header else doc.page_content


def _smart_truncate(text: str, query: str, max_chars: int = 512) -> str:
    """智能截断：关键词定位 → 标题+前半段兜底 → 纯截断保底"""
    if len(text) <= max_chars:
        return text

    # 策略1: 关键词定位 — 找 query 中 2-gram 在文本中的位置
    keywords = [query[i:i+2] for i in range(len(query) - 1)]
    best_pos = None
    for kw in keywords:
        pos = text.find(kw)
        if pos != -1:
            best_pos = pos
            break

    if best_pos is not None:
        # 以关键词为中心取上下文
        half = max_chars // 2
        start = max(0, best_pos - half)
        end = min(len(text), start + max_chars)
        return text[start:end]
    else:
        # 策略2: 兜底 — 优先保留标题 + 正文前半段
        if text.startswith("【"):
            header_end = text.find("】", 0, 200)
            if header_end > 0:
                header = text[:header_end + 1]
                body_limit = min(len(text), max_chars - len(header))
                return header + text[header_end + 1:body_limit]
        # 策略3: 纯正文，取前半段
        return text[:max_chars]


def _is_chinese_doc(text: str) -> bool:
    """综合判断中文文档（避免中英混排误判）"""
    total = max(len(text), 1)
    cn_chars = sum(1 for c in text if '一' <= c <= '鿿')
    cn_ratio = cn_chars / total

    if cn_ratio > 0.3:
        return True

    en_words = len(re.findall(r'[a-zA-Z]{3,}', text))
    code_symbols = sum(1 for c in text if c in '{}[]();<>=+-*/|&^%$#@!~`')
    code_ratio = code_symbols / total

    if en_words > 5 and code_ratio > 0.1 and cn_ratio < 0.1:
        return False

    return cn_ratio > 0.1


# ═══════════════════════════════════════════════════════════════
# 打分与校准
# ═══════════════════════════════════════════════════════════════

def _compute_pair_score(query: str, doc_text: str, tokenizer, model,
                         device: str = "cpu") -> float:
    """单条打分 + 异常隔离"""
    try:
        inputs = tokenizer([(query, doc_text)], padding=True, truncation=True,
                          max_length=512, return_tensors="pt")
        if device != "cpu":
            inputs = {k: v.to(device) for k, v in inputs.items()}
        with torch.no_grad():
            score = model(**inputs, return_dict=True).logits.view(-1).item()
        return score
    except Exception:
        return -999.0  # 异常片段给最低分，不影响整体排序


def _calibrate_bilingual_scores(docs: list, scores: list) -> list:
    """中英文得分校准：统计平均分，拉平基准"""
    cn_idx, en_idx = [], []
    for i, doc in enumerate(docs):
        text = doc.page_content[:200]
        if _is_chinese_doc(text):
            cn_idx.append(i)
        else:
            en_idx.append(i)

    if cn_idx and en_idx:
        cn_avg = sum(scores[i] for i in cn_idx) / len(cn_idx)
        en_avg = sum(scores[i] for i in en_idx) / len(en_idx)
        if en_avg < cn_avg:
            ratio = min(cn_avg / max(en_avg, 0.001), RERANK_CALIBRATION_MAX_RATIO)
            for i in en_idx:
                scores[i] = min(scores[i] * ratio, max(scores))
    return scores


# ═══════════════════════════════════════════════════════════════
# 重排策略
# ═══════════════════════════════════════════════════════════════

def rerank_cross_encoder(query: str, docs: list) -> list:
    """BGE-Reranker 精排（默认策略）"""
    if len(docs) <= 1:
        return docs

    tokenizer, model = _get_reranker()
    device = _get_device()

    # 拼接标题路径 + 智能截断
    texts = [_smart_truncate(_build_rerank_text(d), query) for d in docs]

    # 先尝试批量推理
    try:
        pairs = [[query, t] for t in texts]
        with torch.no_grad():
            inputs = tokenizer(pairs, padding=True, truncation=True,
                              max_length=512, return_tensors="pt")
            if device != "cpu":
                inputs = {k: v.to(device) for k, v in inputs.items()}
            scores = model(**inputs, return_dict=True).logits.view(-1).cpu().tolist()
    except Exception:
        # 批量失败 → 降级循环单条
        print("   ⚠️ 批量推理失败，降级循环单条打分", file=sys.stderr, flush=True)
        scores = [_compute_pair_score(query, t, tokenizer, model, device) for t in texts]

    # 双语校准
    scores = _calibrate_bilingual_scores(docs, scores)

    # 按分数降序
    ranked = list(zip(docs, scores))
    ranked.sort(key=lambda x: x[1], reverse=True)

    # TOP_K 硬截断：只留最相关的 N 条
    ranked = ranked[:RERANK_TOP_K]

    # 低分过滤（-999 = 关闭，显式设置合理阈值时才启用）
    if RERANK_MIN_SCORE > -100:
        ranked = [(d, s) for d, s in ranked if s >= RERANK_MIN_SCORE]
        if not ranked:
            # 至少保留最高分 1 条
            best = max(zip(docs, scores), key=lambda x: x[1])
            ranked = [best]

    # 得分写入元数据
    for rank, (doc, score) in enumerate(ranked):
        doc.metadata["rerank_score"] = round(score, 4)
        doc.metadata["rerank_rank"] = rank + 1

    # 日志
    _log_rerank_result(ranked, docs)

    return [d for d, _ in ranked]


def rerank_llm(query: str, docs: list) -> list:
    """LLM 排名制重排（兜底策略）"""
    import json as _json
    from config import LLM_MODEL, GROQ_BASE_URL, GROQ_API_KEY
    from openai import OpenAI

    if len(docs) <= 1:
        return docs

    prompt = "请按与问题的相关性从高到低排列以下文档片段。\n"
    prompt += "只输出 JSON 数组，按相关性降序列出文档序号。不要任何解释。\n\n"
    prompt += f"示例输出: [3, 0, 5, 1, 2]\n\n问题: {query}\n\n"

    for i, doc in enumerate(docs):
        text = _smart_truncate(_build_rerank_text(doc), query, 300)
        prompt += f"[{i}] {text}\n\n"

    prompt += "相关性从高到低排列（只输出 JSON 数组）:"

    try:
        client = OpenAI(base_url=GROQ_BASE_URL, api_key=GROQ_API_KEY, timeout=10)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        content = response.choices[0].message.content.strip()
        # 从 token_tracker 记录用量
        from token_tracker import get_tracker
        get_tracker().record(LLM_MODEL, response.usage, call_site="reranker.llm")

        # 提取 JSON 数组
        match = re.search(r'\[[\d,\s]+\]', content)
        if match:
            order = _json.loads(match.group())
            ranked = [docs[i] for i in order if 0 <= i < len(docs)]

            # 得分写入元数据（LLM 重排无分数，用排名序号代替）
            for rank, doc in enumerate(ranked):
                doc.metadata["rerank_score"] = float(len(ranked) - rank)
                doc.metadata["rerank_rank"] = rank + 1

            _log_rerank_result(list(zip(ranked, [len(ranked)-i for i in range(len(ranked))])), docs)
            return ranked
    except Exception as e:
        print(f"   ⚠️ LLM 重排失败 ({e})，使用原始排序", file=sys.stderr, flush=True)

    return docs


# ═══════════════════════════════════════════════════════════════
# 截断与缓存
# ═══════════════════════════════════════════════════════════════

def _truncate_by_tokens(docs: list, max_tokens: int,
                         min_docs: int = 3, max_docs: int = 8) -> list:
    """按 Token 数动态截断（粗略估算，不需要精确 tokenize）"""
    result = []
    total = 0
    for doc in docs:
        est = len(_build_rerank_text(doc)) // 2  # 中英文混合粗略估算
        if len(result) >= min_docs and total + est > max_tokens:
            break
        if len(result) >= max_docs:
            break
        result.append(doc)
        total += est
    return result


def _cache_key(query: str, docs: list) -> str:
    """query + 候选集内容 → MD5"""
    h = hashlib.md5(query.encode())
    for doc in docs:
        h.update(doc.page_content[:80].encode())
    return h.hexdigest()


def _cache_get(key: str):
    """读取缓存，过期返回 None"""
    entry = _cache.get(key)
    if entry and time.time() < entry[1]:
        return entry[0]
    return None


def _cache_set(key: str, result: list):
    """写入缓存，超过上限清理最早条目"""
    _cache[key] = (result, time.time() + _CACHE_TTL)
    if len(_cache) > _CACHE_MAX:
        oldest = min(_cache.items(), key=lambda x: x[1][1])
        del _cache[oldest[0]]


# ═══════════════════════════════════════════════════════════════
# 日志
# ═══════════════════════════════════════════════════════════════

def _log_rerank_result(ranked: list, original: list):
    """输出重排日志：排名变化"""
    print(f"   📊 重排: {len(original)}条候选 → {len(ranked)}条精选",
          file=sys.stderr, flush=True)
    for new_rank, (doc, score) in enumerate(ranked[:5]):
        try:
            old_rank = original.index(doc) + 1
        except ValueError:
            old_rank = "NEW"
        if isinstance(old_rank, int):
            if old_rank > new_rank + 1:
                change = f"↑{old_rank - new_rank - 1}"
            elif old_rank < new_rank + 1:
                change = f"↓{new_rank + 1 - old_rank}"
            else:
                change = "—"
        else:
            change = "NEW"
        preview = doc.page_content[:60].replace("\n", " ")
        print(f"   [{new_rank+1}] 得分{score:.2f} {change:<4} {preview}...",
              file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════════
# 统一入口
# ═══════════════════════════════════════════════════════════════

def rerank(query: str, docs: list) -> list:
    """重排统一入口：策略分发 → 打分 → 过滤 → 截断

    Args:
        query: 用户问题
        docs:   合并去重后的候选文档列表

    Returns:
        重排 + 截断后的文档列表
    """
    if not RERANK_ENABLED or len(docs) <= 1:
        return docs

    # Query 缓存
    key = _cache_key(query, docs)
    cached = _cache_get(key)
    if cached is not None:
        print(f"   ⚡ 重排缓存命中", file=sys.stderr, flush=True)
        return cached

    # 候选数上限
    if len(docs) > RERANK_MAX_CANDIDATES:
        docs = docs[:RERANK_MAX_CANDIDATES]

    # 策略分发
    if RERANK_STRATEGY == "cross-encoder":
        try:
            ranked = rerank_cross_encoder(query, docs)
        except Exception as e:
            print(f"   ⚠️ Cross-Encoder 重排失败 ({e})，降级 LLM 重排",
                  file=sys.stderr, flush=True)
            try:
                ranked = rerank_llm(query, docs)
            except Exception:
                print("   ⚠️ LLM 重排也失败，使用原始排序", file=sys.stderr, flush=True)
                ranked = docs
    elif RERANK_STRATEGY == "llm":
        try:
            ranked = rerank_llm(query, docs)
        except Exception:
            ranked = docs
    else:  # "none"
        ranked = docs

    # 动态 Token 截断（TOP_K 作为硬上限，Token 数作为软上限）
    result = _truncate_by_tokens(ranked, RERANK_MAX_TOKENS,
                                  min_docs=min(3, len(ranked)),
                                  max_docs=RERANK_TOP_K)

    # 写入缓存
    _cache_set(key, result)

    return result
