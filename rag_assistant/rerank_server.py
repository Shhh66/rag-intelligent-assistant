"""重排模型独立服务 —— 将 BGE-Reranker 从 RAG 主进程拆出，只做纯打分推理

启动：uvicorn rerank_server:app --port 8002
接口：
  GET  /rerank  → {"status": "ok", "model_loaded": true/false}
  POST /rerank  → {"query": "...", "texts": [...]} → {"scores": [...]}

职责划分：
  服务端 = 纯模型打分（query × texts 的 Cross-Encoder logits）
  主进程 reranker.py = 截断 / 双语校准 / TopK 排序 / 降级（业务逻辑保留在主进程）
"""

import sys
import os
import threading
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI
from pydantic import BaseModel

from config import RERANK_MODEL, HF_ENDPOINT


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时后台预加载模型，避免 /ready 永远等不到 model_loaded=true"""
    print("   🚀 重排服务启动，后台预加载模型...", file=sys.stderr, flush=True)
    threading.Thread(target=_ensure_loaded, daemon=True).start()
    yield


app = FastAPI(title="RAG Rerank Service", lifespan=lifespan)

# ═══════════════════════════════════════════════════════════════
# 全局单例（懒加载 + 线程安全）
# ═══════════════════════════════════════════════════════════════

_model = None
_tokenizer = None
_device = None
_lock = threading.Lock()


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


def _load_reranker():
    """加载 BGE-Reranker（复用 reranker.py 的加载策略：CPU 半精度 / GPU 加速）"""
    global _model, _tokenizer
    from transformers import AutoModelForSequenceClassification, AutoTokenizer
    from huggingface_hub import try_to_load_from_cache

    # 设置 HF 镜像（国内访问 huggingface.co 不稳定）
    if "HF_ENDPOINT" not in os.environ:
        os.environ["HF_ENDPOINT"] = HF_ENDPOINT

    # 本地有缓存则强制离线加载（避免走默认 huggingface.co 下载超时）
    cached = try_to_load_from_cache(
        repo_id=RERANK_MODEL, filename="model.safetensors"
    )

    print(f"   ⏳ 重排服务加载模型 {RERANK_MODEL}（首次 ~570MB）...",
          file=sys.stderr, flush=True)
    if cached:
        print(f"   📌 使用本地缓存（离线模式）", file=sys.stderr, flush=True)
        _tokenizer = AutoTokenizer.from_pretrained(RERANK_MODEL, local_files_only=True)
        _model = AutoModelForSequenceClassification.from_pretrained(
            RERANK_MODEL, local_files_only=True
        )
    else:
        _tokenizer = AutoTokenizer.from_pretrained(RERANK_MODEL)
        _model = AutoModelForSequenceClassification.from_pretrained(RERANK_MODEL)

    device = _get_device()
    if device == "cpu":
        try:
            _model = _model.half()  # fp16
            print("   📌 重排服务: CPU 半精度", file=sys.stderr, flush=True)
        except Exception:
            print("   📌 重排服务: CPU 全精度", file=sys.stderr, flush=True)
    else:
        _model = _model.to(device)
        print(f"   📌 重排服务: {device.upper()} 加速", file=sys.stderr, flush=True)

    _model.eval()
    print("   ✅ 重排服务模型加载完成", file=sys.stderr, flush=True)


def _ensure_loaded():
    """确保模型已加载（并发安全）"""
    global _model
    if _model is not None:
        return _tokenizer, _model
    with _lock:
        if _model is None:
            _load_reranker()
    return _tokenizer, _model


# ═══════════════════════════════════════════════════════════════
# 接口
# ═══════════════════════════════════════════════════════════════

@app.get("/ready")
def ready():
    """健康检查"""
    return {
        "status": "ok",
        "model_loaded": _model is not None,
        "model": RERANK_MODEL,
    }


class RerankRequest(BaseModel):
    query: str
    texts: list[str]


@app.post("/rerank")
def rerank(req: RerankRequest):
    """对 query × texts 批量打分，返回 logits 列表（非 0-1 概率）"""
    if not req.texts:
        return {"scores": []}

    tokenizer, model = _ensure_loaded()
    device = _get_device()

    pairs = [[req.query, t] for t in req.texts]
    with torch.no_grad():
        inputs = tokenizer(pairs, padding=True, truncation=True,
                           max_length=512, return_tensors="pt")
        if device != "cpu":
            inputs = {k: v.to(device) for k, v in inputs.items()}
        scores = model(**inputs, return_dict=True).logits.view(-1).cpu().tolist()

    return {"scores": scores}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
