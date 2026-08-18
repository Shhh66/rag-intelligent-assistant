"""嵌入模型独立服务 —— 将嵌入从 RAG 主进程拆出，支持多实例共享

启动：uvicorn embed_server:app --port 8001
接口：
  GET  /ready   → {"status": "ok", "model_loaded": true/false}
  POST /embed   → {"texts": [...]} → {"embeddings": [[...], ...]}

设计要点：
  1. 批处理接口（一次嵌入整批 chunk，避免单条 HTTP 往返开销）
  2. 懒加载 + 线程锁（首次请求加载 420MB 模型，后续复用）
  3. /ready 供健康检查/就绪探针使用（模型加载完成才对外服务）
"""

import sys
import os
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from config import EMBEDDING_MODEL, HF_ENDPOINT


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时后台预加载模型，避免 /ready 永远等不到 model_loaded=true"""
    print("   🚀 嵌入服务启动，后台预加载模型...", file=sys.stderr, flush=True)
    threading.Thread(target=_ensure_loaded, daemon=True).start()
    yield


app = FastAPI(title="RAG Embedding Service", lifespan=lifespan)

# ═══════════════════════════════════════════════════════════════
# 全局单例（懒加载 + 线程安全）
# ═══════════════════════════════════════════════════════════════

_embedder = None
_loading_lock = threading.Lock()
_loading = False


def _load_embedder():
    """加载嵌入模型（复用 vector_store 的加载策略：优先本地缓存，否则镜像下载）"""
    global _embedder
    from langchain_huggingface import HuggingFaceEmbeddings
    from huggingface_hub import try_to_load_from_cache

    model_name = f"sentence-transformers/{EMBEDDING_MODEL}"
    cached = try_to_load_from_cache(
        repo_id=model_name, filename="model.safetensors"
    )

    if cached:
        print(f"   ⏳ 嵌入服务加载本地模型（离线）...", file=sys.stderr, flush=True)
        _embedder = HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs={"device": "cpu", "local_files_only": True},
        )
    else:
        if "HF_ENDPOINT" not in os.environ:
            os.environ["HF_ENDPOINT"] = HF_ENDPOINT
        print(f"   ⏳ 嵌入服务下载模型（镜像: {HF_ENDPOINT}）...", file=sys.stderr, flush=True)
        _embedder = HuggingFaceEmbeddings(
            model_name=model_name,
            model_kwargs={"device": "cpu"},
        )
    print(f"   ✅ 嵌入服务模型加载完成: {EMBEDDING_MODEL}", file=sys.stderr, flush=True)


def _ensure_loaded():
    """确保模型已加载（并发安全：同一时刻只加载一次）"""
    global _embedder, _loading
    if _embedder is not None:
        return _embedder
    with _loading_lock:
        if _embedder is None:
            _loading = True
            _load_embedder()
            _loading = False
    return _embedder


# ═══════════════════════════════════════════════════════════════
# 接口
# ═══════════════════════════════════════════════════════════════

@app.get("/ready")
def ready():
    """健康检查：供 Docker healthcheck / K8s readinessProbe 使用"""
    return {
        "status": "ok",
        "model_loaded": _embedder is not None,
        "model": EMBEDDING_MODEL,
    }


class EmbedRequest(BaseModel):
    texts: list[str]


@app.post("/embed")
def embed(req: EmbedRequest):
    """批处理嵌入：一次嵌入整批文本，返回对应向量列表"""
    if not req.texts:
        return {"embeddings": []}
    embedder = _ensure_loaded()
    embeddings = embedder.embed_documents(req.texts)
    return {"embeddings": embeddings}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
