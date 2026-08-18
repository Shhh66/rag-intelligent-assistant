"""向量嵌入与存储模块 —— 将文本块向量化并存入 ChromaDB，支持增量增删改"""

import os
import sys
import json
import hashlib
import shutil
from typing import List, Optional
from pathlib import Path
from datetime import datetime, timezone

from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from huggingface_hub import try_to_load_from_cache
from filelock import FileLock, Timeout as LockTimeout
import chromadb

from config import (
    VECTOR_DB_PATH, EMBEDDING_MODEL, TOP_K, HF_ENDPOINT,
    KB_META_FILE, KB_LOCK_FILE, KB_SNAPSHOT_DIR,
    SNAPSHOT_MAX_COUNT, SOFT_DELETE,
    EMBED_SERVER_URL, EMBED_SERVER_TIMEOUT,
    CHROMA_SERVER_URL,
)


# ═══════════════════════════════════════════════════════════════
# 全局缓存
# ═══════════════════════════════════════════════════════════════

_embeddings = None


# ═══════════════════════════════════════════════════════════════
# ChromaDB 客户端工厂（单机 PersistentClient / 多实例 HttpClient）
# ═══════════════════════════════════════════════════════════════

def _get_client():
    """按配置返回 PersistentClient 或 HttpClient（单机 vs 多实例共享）。

    CHROMA_SERVER_URL 空 → PersistentClient（本地目录，单机）
    CHROMA_SERVER_URL 非空 → HttpClient（client-server，多实例共享同一库）
    """
    if CHROMA_SERVER_URL:
        # 例：http://localhost:8000
        url = CHROMA_SERVER_URL.rstrip("/")
        parts = url.replace("http://", "").replace("https://", "").split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 8000
        return chromadb.HttpClient(host=host, port=port)
    return chromadb.PersistentClient(path=VECTOR_DB_PATH)


# ═══════════════════════════════════════════════════════════════
# 嵌入模型（本地加载 / 独立服务 二选一）
# ═══════════════════════════════════════════════════════════════

class HttpEmbeddings:
    """通过独立嵌入服务向量化（走 HTTP）。

    实现 LangChain Chroma 所需的 embedding 接口（鸭子类型）：
      embed_documents(texts) -> list[list[float]]
      embed_query(text)      -> list[float]
    配置了 EMBED_SERVER_URL 时使用，否则回退本地 HuggingFaceEmbeddings。
    """

    def __init__(self, base_url: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def embed_documents(self, texts):
        import httpx
        if not texts:
            return []
        url = f"{self.base_url}/embed"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json={"texts": list(texts)})
            resp.raise_for_status()
            return resp.json()["embeddings"]

    def embed_query(self, text):
        return self.embed_documents([text])[0]

def _model_is_cached() -> bool:
    """检查嵌入模型是否已在本地缓存"""
    model_name = f"sentence-transformers/{EMBEDDING_MODEL}"
    result = try_to_load_from_cache(
        repo_id=model_name,
        filename="model.safetensors",
    )
    return result is not None


def get_embeddings():
    """获取嵌入模型（懒加载，优先本地缓存，否则从镜像下载）

    配置了 EMBED_SERVER_URL 时走独立嵌入服务（HTTP），否则本地加载模型。
    """
    global _embeddings
    if _embeddings is None:
        if EMBED_SERVER_URL:
            # 走独立嵌入服务，无需在本地进程加载 420MB 模型
            _embeddings = HttpEmbeddings(EMBED_SERVER_URL, EMBED_SERVER_TIMEOUT)
            print(f"   ✅ 使用嵌入服务: {EMBED_SERVER_URL}", file=sys.stderr, flush=True)
        else:
            model_name = f"sentence-transformers/{EMBEDDING_MODEL}"
            if _model_is_cached():
                print(f"   ⏳ 加载本地嵌入模型（使用缓存，离线模式）...", file=sys.stderr, flush=True)
                _embeddings = HuggingFaceEmbeddings(
                    model_name=model_name,
                    model_kwargs={"device": "cpu", "local_files_only": True},
                )
            else:
                if "HF_ENDPOINT" not in os.environ:
                    os.environ["HF_ENDPOINT"] = HF_ENDPOINT
                print(f"   ⏳ 下载嵌入模型（镜像: {HF_ENDPOINT}，仅首次需要）...", file=sys.stderr, flush=True)
                _embeddings = HuggingFaceEmbeddings(
                    model_name=model_name,
                    model_kwargs={"device": "cpu"},
                )
            print(f"   ✅ 嵌入模型加载完成: {EMBEDDING_MODEL}", file=sys.stderr, flush=True)
    return _embeddings


# ═══════════════════════════════════════════════════════════════
# 全量建库（保留，增加 db_meta.json 写入）
# ═══════════════════════════════════════════════════════════════

def build_vector_store(docs: List[Document]) -> Chroma:
    """将文档块向量化并存入 ChromaDB（全量重建）"""
    embeddings = get_embeddings()

    db_path = Path(VECTOR_DB_PATH)
    db_path.mkdir(parents=True, exist_ok=True)

    # 用 ChromaDB 自身 API 清理旧数据（避免文件锁冲突）
    # 构建是离线运维操作，始终写本地 PersistentClient（不随 CHROMA_SERVER_URL 走远程）
    client = chromadb.PersistentClient(path=str(db_path))
    try:
        client.delete_collection("langchain")
        print("   🗑 已清空旧向量库", file=sys.stderr, flush=True)
    except Exception:
        pass  # 首次构建，不存在旧集合

    print(f"   📊 正在向量化 {len(docs)} 个文本块...", file=sys.stderr, flush=True)
    vector_store = Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        persist_directory=str(db_path),
        collection_name="langchain",
    )
    print(f"   ✅ 向量库构建完成！存储位置: {VECTOR_DB_PATH}", file=sys.stderr, flush=True)

    # 重建 db_meta.json
    _rebuild_meta(docs)

    return vector_store


def _rebuild_meta(docs: List[Document]):
    """全量建库后从 docs 重建 db_meta.json"""
    from collections import defaultdict

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    documents = {}
    chunk_count_by_source = defaultdict(int)

    for doc in docs:
        source = doc.metadata.get("file_path", doc.metadata.get("source", "unknown"))
        chunk_count_by_source[source] += 1

    for source, count in chunk_count_by_source.items():
        # 全量重建时没有原始文件哈希，用 chunk 内容生成
        documents[source] = {
            "file_hash": "",
            "chunks": count,
            "added_at": now,
            "updated_at": None,
        }

    meta = {
        "embedding_model": EMBEDDING_MODEL,
        "embedding_dim": 384,  # MiniLM-L12-v2 固定 384 维
        "documents": documents,
        "total_chunks": len(docs),
    }
    _save_meta(meta)


def load_vector_store() -> Chroma:
    """加载已有的向量库（支持单机 PersistentClient / 多实例 HttpClient）"""
    embeddings = get_embeddings()

    # client-server 模式：走 Chroma Server，无需本地目录存在
    if CHROMA_SERVER_URL:
        client = _get_client()
        return Chroma(
            client=client,
            embedding_function=embeddings,
            collection_name="langchain",
        )

    # 单机模式：本地目录必须存在
    db_path = Path(VECTOR_DB_PATH)
    if not db_path.exists() or not list(db_path.iterdir()):
        raise FileNotFoundError(f"向量库不存在: {VECTOR_DB_PATH}\n请先上传文档构建知识库。")
    return Chroma(
        persist_directory=str(db_path),
        embedding_function=embeddings,
        collection_name="langchain",
    )


def search(query: str, top_k: int = TOP_K, kb_groups: list = None):
    """在向量库中检索最相似的文档片段（权限通过 kb_groups 参数显式传入）

    kb_groups 语义：
      None（默认）→ 不限权限（管理员/未登录），无过滤
      [...]       → 显式权限过滤（请求级，并发安全，不依赖全局文件）
      []          → 仅 public 文档
    """
    if kb_groups is None:
        # None = 不限权限（管理员 / 未登录）→ 无过滤
        vector_store = load_vector_store()
        return vector_store.similarity_search(query, k=top_k)

    # 非 None（含空列表 []）→ 显式权限过滤
    return search_with_permission(query, kb_groups=kb_groups, top_k=top_k)


def search_with_permission(query: str, kb_groups: list = None,
                           top_k: int = TOP_K):
    """带权限过滤的语义检索（全量召回 → 候选集后置 where 过滤）

    kb_groups=None  → 不限权限（管理员，退化为普通 search）
    kb_groups=[]    → 只能看 public 文档
    kb_groups=['dept_rd'] → dept_rd 组 + public 文档
    """
    if kb_groups is None:
        return search(query, top_k)

    vector_store = load_vector_store()

    # 构建 where 条件，处理 kb_groups 为空的情况
    if kb_groups:
        # 用户可访问的组 + 所有 public 文档
        where_filter = {
            "$or": [
                {"kb_group": {"$in": kb_groups}},
                {"visibility": "public"},
            ]
        }
    else:
        # 无分组权限，只能看 public
        where_filter = {"visibility": "public"}

    print(f"   🔒 权限过滤: kb_groups={kb_groups}, where={where_filter}",
          file=sys.stderr, flush=True)

    results = vector_store.similarity_search(
        query, k=top_k,
        filter=where_filter,
    )
    return results


# ═══════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════

def _compute_hash(file_path: str) -> str:
    """计算文件 MD5（8KB 分块读取，兼容大文件）"""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def _make_chunk_id(file_hash: str, index: int) -> str:
    """生成稳定唯一 chunk ID：文件MD5_分块序号 → 天然幂等"""
    return f"{file_hash}_{index:04d}"


def _get_meta_path() -> Path:
    return Path(KB_META_FILE)


def _load_meta() -> dict:
    """加载 db_meta.json，不存在返回空结构"""
    path = _get_meta_path()
    if not path.exists():
        return {
            "embedding_model": "",
            "embedding_dim": 0,
            "documents": {},
            "total_chunks": 0,
        }
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_meta(meta: dict):
    """原子写入 db_meta.json（先写 .tmp，再 os.replace 原子替换）"""
    path = _get_meta_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _get_lock() -> FileLock:
    """获取全局文件锁"""
    Path(KB_LOCK_FILE).parent.mkdir(parents=True, exist_ok=True)
    return FileLock(KB_LOCK_FILE, timeout=30)


def _validate_model() -> None:
    """校验当前嵌入模型与建库时一致，不一致则报错"""
    meta = _load_meta()
    saved_model = meta.get("embedding_model", "")
    if saved_model and saved_model != EMBEDDING_MODEL:
        raise RuntimeError(
            f"❌ 嵌入模型已变更！\n"
            f"   建库时: {saved_model}\n"
            f"   当前:   {EMBEDDING_MODEL}\n"
            f"   请执行全量重建: python kb_manager.py clear && python kb_manager.py add-dir uploaded_docs/"
        )


def _normalize_path(file_path: str) -> str:
    """将文件路径标准化为相对路径（正斜杠）"""
    try:
        rel = os.path.relpath(file_path)
    except ValueError:
        # Windows 不同盘符时 fallback 用绝对路径
        rel = file_path
    return rel.replace("\\", "/")


# ═══════════════════════════════════════════════════════════════
# 快照备份与回退
# ═══════════════════════════════════════════════════════════════

def _backup_snapshot(operation: str) -> str:
    """破坏性操作前自动创建快照

    Returns: 快照时间戳
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_dir = Path(KB_SNAPSHOT_DIR)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    # 备份 db_meta.json
    meta_path = _get_meta_path()
    if meta_path.exists():
        shutil.copy2(meta_path, snapshot_dir / f"db_meta.{timestamp}.bak")

    # 备份当前所有 chunk ID（从 Chroma 查询）
    client = _get_client()
    try:
        col = client.get_collection("langchain")
        all_data = col.get()
        snapshot = {
            "operation": operation,
            "timestamp": timestamp,
            "chunk_count": len(all_data["ids"]),
            "db_meta_backup": f"db_meta.{timestamp}.bak",
        }
        with open(snapshot_dir / f"snapshot_{timestamp}.json", "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
    except Exception:
        pass  # 集合不存在时跳过

    # 清理超出数量的旧快照（连带 chunk 备份一起清）
    snapshots = sorted(snapshot_dir.glob("snapshot_*.json"), reverse=True)
    for old in snapshots[SNAPSHOT_MAX_COUNT:]:
        ts = old.stem.replace("snapshot_", "")
        old.unlink()
        for prefix in ["db_meta.", "chunks_"]:
            old_file = snapshot_dir / f"{prefix}{ts}.bak" if prefix == "db_meta." else snapshot_dir / f"{prefix}{ts}.json"
            # Also try without .bak suffix for db_meta
            if not old_file.exists() and prefix == "db_meta.":
                old_file = snapshot_dir / f"{prefix}{ts}"
            if old_file.exists():
                old_file.unlink()

    return timestamp


def _backup_chunks(operation: str, col_get_result: dict,
                   timestamp: str = None) -> str:
    """删除操作前备份即将被删的 chunk 数据（精准备份，不拷全库）

    保存 ChromaDB col.get() 返回的完整数据，回退时可精确恢复。
    timestamp 应与 _backup_snapshot() 返回值一致，保证回退时能匹配。
    Returns: 备份文件名（不含扩展名），如 "chunks_20260705_143000"
    """
    if not col_get_result or not col_get_result.get("ids"):
        return ""

    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    snapshot_dir = Path(KB_SNAPSHOT_DIR)
    snapshot_dir.mkdir(parents=True, exist_ok=True)

    backup_data = {
        "operation": operation,
        "timestamp": timestamp,
        "ids": col_get_result["ids"],
        "documents": col_get_result.get("documents", []),
        "metadatas": col_get_result.get("metadatas", []),
        "embeddings": col_get_result.get("embeddings"),  # 可能为 None（get 时未 include）
    }
    backup_path = snapshot_dir / f"chunks_{timestamp}.json"
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(backup_data, f, ensure_ascii=False)

    print(f"   💾 已备份 {len(backup_data['ids'])} 个 chunk → {backup_path.name}",
          file=sys.stderr, flush=True)
    return f"chunks_{timestamp}"


def _restore_chunks(backup_name: str) -> int:
    """从备份文件恢复 chunk 到 ChromaDB

    Returns: 恢复的 chunk 数量，-1 表示失败
    """
    snapshot_dir = Path(KB_SNAPSHOT_DIR)
    backup_path = snapshot_dir / f"{backup_name}.json"
    if not backup_path.exists():
        return -1

    with open(backup_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    ids = data.get("ids", [])
    if not ids:
        return 0

    client = _get_client()
    try:
        col = client.get_collection("langchain")
    except Exception:
        col = client.create_collection("langchain")

    # 先清除可能残留的同 ID chunk（幂等）
    try:
        col.delete(ids=ids)
    except Exception:
        pass

    # 写入备份数据
    col.add(
        ids=ids,
        documents=data.get("documents"),
        metadatas=data.get("metadatas"),
        embeddings=data.get("embeddings"),
    )
    print(f"   ♻ 已恢复 {len(ids)} 个 chunk: {backup_name}",
          file=sys.stderr, flush=True)
    return len(ids)


def rollback(timestamp: str = None) -> dict:
    """回退到指定快照状态（默认最近一次）

    Returns: {"restored_documents": int, "note": str}
    """
    snapshot_dir = Path(KB_SNAPSHOT_DIR)
    if not snapshot_dir.exists():
        return {"restored_documents": 0, "note": "没有可用快照"}

    # 查找快照
    snapshots = sorted(snapshot_dir.glob("snapshot_*.json"), reverse=True)
    if not snapshots:
        return {"restored_documents": 0, "note": "没有可用快照"}

    if timestamp:
        target = snapshot_dir / f"snapshot_{timestamp}.json"
        if not target.exists():
            available = [s.stem.replace("snapshot_", "") for s in snapshots]
            return {"restored_documents": 0, "note": f"快照 {timestamp} 不存在。可用: {available}"}
    else:
        target = snapshots[0]
        timestamp = target.stem.replace("snapshot_", "")

    # 读取快照信息
    with open(target, "r", encoding="utf-8") as f:
        snapshot = json.load(f)

    # 查找对应的 chunk 备份
    chunk_backups = sorted(
        snapshot_dir.glob(f"chunks_{timestamp}*.json") or
        snapshot_dir.glob("chunks_*.json"),
        reverse=True,
    )
    # 优先精确匹配时间戳，否则取最近一次
    exact_chunk = snapshot_dir / f"chunks_{timestamp}.json"
    if exact_chunk.exists():
        chunk_backup_name = f"chunks_{timestamp}"
    elif chunk_backups:
        # 取不晚于目标快照的最近一次 chunk 备份
        recent = [c for c in chunk_backups
                  if c.stem.replace("chunks_", "") <= timestamp]
        chunk_backup_name = recent[0].stem if recent else (
            chunk_backups[0].stem if chunk_backups else ""
        )
    else:
        chunk_backup_name = ""

    # 恢复 db_meta.json
    bak_path = snapshot_dir / snapshot.get("db_meta_backup", "")
    if not bak_path.exists():
        return {"restored_documents": 0, "note": "快照备份文件缺失"}

    shutil.copy2(bak_path, _get_meta_path())
    meta = _load_meta()
    doc_count = len(meta.get("documents", {}))

    # 恢复 chunk 数据
    chunks_restored = 0
    if chunk_backup_name:
        chunks_restored = _restore_chunks(chunk_backup_name)

    return {
        "restored_documents": doc_count,
        "chunks_restored": chunks_restored,
        "timestamp": timestamp,
        "note": f"已恢复到 {timestamp} 的快照状态。"
                + (f" 恢复了 {chunks_restored} 个 chunk。" if chunks_restored else ""),
    }


def list_snapshots() -> list:
    """列出所有可用快照"""
    snapshot_dir = Path(KB_SNAPSHOT_DIR)
    if not snapshot_dir.exists():
        return []
    results = []
    for s in sorted(snapshot_dir.glob("snapshot_*.json"), reverse=True):
        with open(s, "r", encoding="utf-8") as f:
            data = json.load(f)
        results.append({
            "timestamp": data["timestamp"],
            "operation": data["operation"],
            "chunk_count": data["chunk_count"],
        })
    return results


# ═══════════════════════════════════════════════════════════════
# 增量 CRUD 操作
# ═══════════════════════════════════════════════════════════════

def add_document(file_path: str, skip_duplicate: bool = True,
                 kb_group: str = None, visibility: str = None) -> dict:
    """增量添加单个文档到向量库

    安全流程：锁 → 校验模型 → 去重 → 解析切块 → 批量写入 → 回滚异常 → 更新meta → 释放锁

    Returns: {"file_path": str, "chunks_added": int, "skipped": bool}
    """
    from config import KB_DEFAULT_GROUP, KB_DEFAULT_VISIBILITY

    if kb_group is None:
        kb_group = KB_DEFAULT_GROUP
    if visibility is None:
        visibility = KB_DEFAULT_VISIBILITY

    lock = _get_lock()
    try:
        lock.acquire()
        return _add_document_locked(file_path, skip_duplicate, kb_group, visibility)
    except LockTimeout:
        return {"file_path": _normalize_path(file_path), "chunks_added": 0, "skipped": False,
                "error": "知识库正在操作中，请稍后再试"}
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _add_document_locked(file_path: str, skip_duplicate: bool,
                          kb_group: str, visibility: str) -> dict:
    """持有锁的执行体"""
    # 延迟导入，避免循环依赖
    from document_loader import load_file, PdfEncryptedError, ScannedPdfError
    from text_splitter import split_documents

    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return {"file_path": _normalize_path(file_path), "chunks_added": 0, "skipped": False,
                "error": f"文件不存在: {file_path}"}

    rel_path = _normalize_path(file_path)
    file_hash = _compute_hash(abs_path)

    # 校验嵌入模型版本
    try:
        _validate_model()
    except RuntimeError as e:
        return {"file_path": rel_path, "chunks_added": 0, "skipped": False, "error": str(e)}

    # 去重检测（路径 + 哈希双重校验）
    if skip_duplicate:
        meta = _load_meta()
        existing = meta.get("documents", {}).get(rel_path)
        if existing and existing.get("file_hash") == file_hash:
            print(f"   ⏭ 文档已存在（路径+哈希一致），跳过: {rel_path}", file=sys.stderr, flush=True)
            return {"file_path": rel_path, "chunks_added": 0, "skipped": True}

        # 路径相同但哈希不同 → 自动走更新
        if existing and existing.get("file_hash") != file_hash:
            print(f"   🔄 文档内容已变更，自动更新: {rel_path}", file=sys.stderr, flush=True)
            return _update_document_locked(abs_path, rel_path, file_hash, existing.get("file_hash", ""))

    # 解析 → 切块
    try:
        docs = load_file(abs_path)
        if not docs:
            return {"file_path": rel_path, "chunks_added": 0, "skipped": False,
                    "error": "文档解析后无内容"}
    except PdfEncryptedError:
        return {"file_path": rel_path, "chunks_added": 0, "skipped": False,
                "error": "PDF 已加密，请解密后重新上传"}
    except ScannedPdfError:
        return {"file_path": rel_path, "chunks_added": 0, "skipped": False,
                "error": "检测为扫描件/图片PDF，建议先用 OCR 工具转换"}

    # 注入 metadata
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    for doc in docs:
        doc.metadata["file_path"] = rel_path
        doc.metadata["file_hash"] = file_hash
        doc.metadata["added_at"] = now
        doc.metadata["kb_group"] = kb_group
        doc.metadata["visibility"] = visibility

    chunks = split_documents(docs)
    if not chunks:
        return {"file_path": rel_path, "chunks_added": 0, "skipped": False,
                "error": "文档分块后无内容"}

    # 注入 metadata 到每个 chunk（含权限字段）
    for chunk in chunks:
        chunk.metadata["file_path"] = rel_path
        chunk.metadata["file_hash"] = file_hash
        chunk.metadata["kb_group"] = kb_group
        chunk.metadata["visibility"] = visibility
        if "added_at" not in chunk.metadata:
            chunk.metadata["added_at"] = now

    embeddings = get_embeddings()

    # 批量写入 Chroma（使用稳定 ID，天然幂等）
    client = _get_client()
    try:
        col = client.get_collection("langchain")
    except Exception:
        col = client.create_collection("langchain")

    chunk_ids = [_make_chunk_id(file_hash, i) for i in range(len(chunks))]
    chunk_texts = [c.page_content for c in chunks]
    chunk_metadatas = [c.metadata for c in chunks]

    try:
        embeddings_list = embeddings.embed_documents(chunk_texts)

        # 批量写入
        col.add(
            ids=chunk_ids,
            documents=chunk_texts,
            metadatas=chunk_metadatas,
            embeddings=embeddings_list,
        )
    except Exception as e:
        # 回滚：清理已写入的 chunk（用稳定 ID 可以精准删除）
        try:
            col.delete(ids=chunk_ids)
        except Exception:
            pass
        return {"file_path": rel_path, "chunks_added": 0, "skipped": False,
                "error": f"向量写入失败，已回滚: {e}"}

    # 更新 db_meta.json
    meta = _load_meta()
    if meta.get("embedding_model") != EMBEDDING_MODEL:
        meta["embedding_model"] = EMBEDDING_MODEL
        meta["embedding_dim"] = 384

    old_doc = meta.get("documents", {}).get(rel_path, {})
    meta.setdefault("documents", {})[rel_path] = {
        "file_hash": file_hash,
        "chunks": len(chunks),
        "added_at": old_doc.get("added_at", now),
        "updated_at": now if old_doc else None,
        "kb_group": kb_group,
        "visibility": visibility,
    }
    meta["total_chunks"] = sum(d["chunks"] for d in meta["documents"].values())
    _save_meta(meta)

    print(f"   ✅ 增量添加完成: {rel_path} → {len(chunks)} chunks", file=sys.stderr, flush=True)
    return {"file_path": rel_path, "chunks_added": len(chunks), "skipped": False}


def remove_document(file_path: str) -> dict:
    """按 file_path 删除文档的所有 chunks（含历史数据兜底）

    Returns: {"file_path": str, "chunks_removed": int}
    """
    lock = _get_lock()
    try:
        lock.acquire()
        return _remove_document_locked(file_path)
    except LockTimeout:
        return {"file_path": _normalize_path(file_path), "chunks_removed": 0,
                "error": "知识库正在操作中，请稍后再试"}
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _remove_document_locked(file_path: str) -> dict:
    """持有锁的删除执行体"""
    rel_path = _normalize_path(file_path)

    # 快照备份（db_meta.json + 快照索引）
    snapshot_ts = _backup_snapshot(f"remove: {rel_path}")

    client = _get_client()
    try:
        col = client.get_collection("langchain")
    except Exception:
        return {"file_path": rel_path, "chunks_removed": 0}

    # 1. 优先按精准的相对路径匹配
    results = col.get(where={"file_path": rel_path})

    # 2. 没匹配到 → 兜底按纯文件名匹配老数据（未 migrate 的历史库）
    if not results['ids']:
        basename = os.path.basename(rel_path)
        results = col.get(where={"source": basename})
        if results['ids']:
            print(f"   ⚠️ 未找到 file_path 匹配，兜底按 source='{basename}' 删除老数据", file=sys.stderr, flush=True)

    ids = results['ids']
    if ids:
        # 删前精准备份：只备份即将被删的 chunk（共用快照时间戳保证回退可匹配）
        _backup_chunks(f"remove: {rel_path}", results, snapshot_ts)
        col.delete(ids=ids)
        print(f"   🗑 已删除 {len(ids)} 个 chunk: {rel_path}", file=sys.stderr, flush=True)

    # 更新 db_meta.json
    meta = _load_meta()
    removed = meta.get("documents", {}).pop(rel_path, None)
    meta["total_chunks"] = sum(d["chunks"] for d in meta["documents"].values())
    _save_meta(meta)

    return {"file_path": rel_path, "chunks_removed": len(ids)}


def update_document(file_path: str) -> dict:
    """安全更新文档：先写新 → 后删旧

    Returns: {"file_path": str, "chunks_removed": int, "chunks_added": int}
    """
    lock = _get_lock()
    try:
        lock.acquire()
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            return {"file_path": _normalize_path(file_path), "chunks_removed": 0, "chunks_added": 0,
                    "error": f"文件不存在: {file_path}"}

        rel_path = _normalize_path(file_path)
        file_hash = _compute_hash(abs_path)

        meta = _load_meta()
        old_hash = meta.get("documents", {}).get(rel_path, {}).get("file_hash", "")

        return _update_document_locked(abs_path, rel_path, file_hash, old_hash)
    except LockTimeout:
        return {"file_path": _normalize_path(file_path), "chunks_removed": 0, "chunks_added": 0,
                "error": "知识库正在操作中，请稍后再试"}
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _update_document_locked(abs_path: str, rel_path: str, new_hash: str, old_hash: str) -> dict:
    """持有锁的更新执行体"""
    from document_loader import load_file, PdfEncryptedError, ScannedPdfError
    from text_splitter import split_documents

    # 快照备份（共用时间戳，保证回退时 chunk 备份可匹配）
    snapshot_ts = _backup_snapshot(f"update: {rel_path}")

    # 1. 解析新版本 chunks
    try:
        docs = load_file(abs_path)
        if not docs:
            return {"file_path": rel_path, "chunks_removed": 0, "chunks_added": 0,
                    "error": "文档解析后无内容"}
    except PdfEncryptedError:
        return {"file_path": rel_path, "chunks_removed": 0, "chunks_added": 0,
                "error": "PDF 已加密，请解密后重新上传"}
    except ScannedPdfError:
        return {"file_path": rel_path, "chunks_removed": 0, "chunks_added": 0,
                "error": "检测为扫描件/图片PDF，建议先用 OCR 工具转换"}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    # 保留旧权限（从 db_meta.json 读取），否则用默认值
    from config import KB_DEFAULT_GROUP, KB_DEFAULT_VISIBILITY
    meta_before = _load_meta()
    old_doc_meta = meta_before.get("documents", {}).get(rel_path, {})
    kb_group = old_doc_meta.get("kb_group", KB_DEFAULT_GROUP)
    visibility = old_doc_meta.get("visibility", KB_DEFAULT_VISIBILITY)

    for doc in docs:
        doc.metadata["file_path"] = rel_path
        doc.metadata["file_hash"] = new_hash
        doc.metadata["added_at"] = now
        doc.metadata["kb_group"] = kb_group
        doc.metadata["visibility"] = visibility

    chunks = split_documents(docs)
    if not chunks:
        return {"file_path": rel_path, "chunks_removed": 0, "chunks_added": 0,
                "error": "文档分块后无内容"}

    for chunk in chunks:
        chunk.metadata["file_path"] = rel_path
        chunk.metadata["file_hash"] = new_hash
        chunk.metadata["kb_group"] = kb_group
        chunk.metadata["visibility"] = visibility
        if "added_at" not in chunk.metadata:
            chunk.metadata["added_at"] = now

    # 2. 先写入新版本（用稳定 ID 天然幂等）
    embeddings = get_embeddings()
    client = _get_client()
    try:
        col = client.get_collection("langchain")
    except Exception:
        col = client.create_collection("langchain")

    chunk_ids = [_make_chunk_id(new_hash, i) for i in range(len(chunks))]
    chunk_texts = [c.page_content for c in chunks]
    chunk_metadatas = [c.metadata for c in chunks]

    try:
        embeddings_list = embeddings.embed_documents(chunk_texts)
        col.add(
            ids=chunk_ids,
            documents=chunk_texts,
            metadatas=chunk_metadatas,
            embeddings=embeddings_list,
        )
    except Exception as e:
        # 写入失败 → 清理新 chunk，不删旧
        try:
            col.delete(ids=chunk_ids)
        except Exception:
            pass
        return {"file_path": rel_path, "chunks_removed": 0, "chunks_added": 0,
                "error": f"新版本写入失败，旧版本数据保留: {e}"}

    # 3. 写入成功 → 备份并删除旧版本 chunks
    chunks_removed = 0
    if old_hash:
        old_results = col.get(
            where={"file_path": rel_path},
            include=["documents", "metadatas", "embeddings"],
        )
        old_ids_to_delete = [id_ for id_ in old_results['ids'] if id_ not in chunk_ids]
        if old_ids_to_delete:
            # 精准备份旧版本数据（共用快照时间戳）
            _backup_chunks(f"update: {rel_path}", old_results, snapshot_ts)
            col.delete(ids=old_ids_to_delete)
            chunks_removed = len(old_ids_to_delete)

    # 4. 更新 db_meta.json
    meta = _load_meta()
    old_doc = meta.get("documents", {}).get(rel_path, {})
    meta.setdefault("documents", {})[rel_path] = {
        "file_hash": new_hash,
        "chunks": len(chunks),
        "added_at": old_doc.get("added_at", now),
        "updated_at": now,
        "kb_group": kb_group,
        "visibility": visibility,
    }
    meta["total_chunks"] = sum(d["chunks"] for d in meta["documents"].values())
    _save_meta(meta)

    print(f"   ✅ 安全更新完成: {rel_path} (删 {chunks_removed} 旧 + 加 {len(chunks)} 新)",
          file=sys.stderr, flush=True)
    return {"file_path": rel_path, "chunks_removed": chunks_removed, "chunks_added": len(chunks)}


def list_documents() -> list:
    """列出库中所有文档（直接读 db_meta.json）"""
    meta = _load_meta()
    docs = meta.get("documents", {})
    return [
        {
            "file_path": path,
            "chunks": info.get("chunks", 0),
            "file_hash": info.get("file_hash", ""),
            "added_at": info.get("added_at", ""),
            "updated_at": info.get("updated_at"),
            "kb_group": info.get("kb_group", ""),
            "visibility": info.get("visibility", ""),
        }
        for path, info in docs.items()
    ]


def is_duplicate(file_path: str) -> bool:
    """路径 + 哈希双重校验"""
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        return False
    rel_path = _normalize_path(file_path)
    file_hash = _compute_hash(abs_path)
    meta = _load_meta()
    existing = meta.get("documents", {}).get(rel_path)
    return existing is not None and existing.get("file_hash") == file_hash


def get_status() -> dict:
    """增强版状态：文档数 + chunk 总数 + 嵌入模型 + 存储路径"""
    meta = _load_meta()
    return {
        "ready": len(meta.get("documents", {})) > 0,
        "document_count": len(meta.get("documents", {})),
        "total_chunks": meta.get("total_chunks", 0),
        "embedding_model": meta.get("embedding_model", EMBEDDING_MODEL),
        "embedding_dim": meta.get("embedding_dim", 384),
        "db_path": str(Path(VECTOR_DB_PATH).resolve()),
        "meta_path": str(_get_meta_path().resolve()),
    }


def repair() -> dict:
    """双源一致性校验：Chroma ⇔ db_meta.json 双向校准

    1. Chroma 有、meta 没有 → 从 Chroma 重建 meta 条目
    2. Meta 有、Chroma 没有 → 从 meta 移除已不存在条目
    3. 校验文档 chunk 数量一致性

    Returns: {"added_to_meta": int, "removed_from_meta": int, "chunks_fixed": int}
    """
    from collections import defaultdict

    client = _get_client()
    try:
        col = client.get_collection("langchain")
        all_data = col.get()
    except Exception:
        return {"added_to_meta": 0, "removed_from_meta": 0, "chunks_fixed": 0,
                "note": "向量库集合不存在"}

    meta = _load_meta()
    meta_docs = meta.get("documents", {})

    # 统计 Chroma 中各文档的 chunk 数
    chroma_counts = defaultdict(int)
    for meta_dict in all_data.get("metadatas", []):
        fp = meta_dict.get("file_path") or meta_dict.get("source", "unknown")
        chroma_counts[fp] += 1

    added = 0
    removed = 0
    fixed = 0

    # Chroma 有、meta 没有 → 补全
    for fp, count in chroma_counts.items():
        if fp not in meta_docs:
            meta_docs[fp] = {
                "file_hash": "",
                "chunks": count,
                "added_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
                "updated_at": None,
            }
            added += 1
            print(f"   ➕ 从 Chroma 补全 meta: {fp} ({count} chunks)", file=sys.stderr, flush=True)

    # Meta 有、Chroma 没有 → 移除
    to_remove = [fp for fp in meta_docs if fp not in chroma_counts]
    for fp in to_remove:
        del meta_docs[fp]
        removed += 1
        print(f"   ➖ 从 meta 移除已不存在条目: {fp}", file=sys.stderr, flush=True)

    # 校验 chunk 数量一致性
    for fp, info in meta_docs.items():
        actual = chroma_counts.get(fp, 0)
        if info.get("chunks") != actual and actual > 0:
            info["chunks"] = actual
            fixed += 1
            print(f"   🔧 修正 chunk 数量: {fp} ({info.get('chunks')} → {actual})",
                  file=sys.stderr, flush=True)

    # 保存修复结果
    meta["total_chunks"] = sum(d["chunks"] for d in meta_docs.values())
    meta["embedding_model"] = meta.get("embedding_model") or EMBEDDING_MODEL
    meta["embedding_dim"] = meta.get("embedding_dim") or 384
    _save_meta(meta)

    result = {"added_to_meta": added, "removed_from_meta": removed, "chunks_fixed": fixed}
    print(f"   ✅ repair 完成: {result}", file=sys.stderr, flush=True)
    return result


def migrate() -> dict:
    """迁移老数据：补全 file_path/file_hash/added_at 元数据到 Chroma chunks

    适用场景：旧版本 chunk 只有 source（纯文件名）字段，补齐增量管理所需字段。

    Returns: {"migrated_chunks": int, "documents_found": int}
    """
    client = _get_client()
    try:
        col = client.get_collection("langchain")
        all_data = col.get()
    except Exception:
        return {"migrated_chunks": 0, "documents_found": 0, "note": "向量库集合不存在"}

    if not all_data["ids"]:
        return {"migrated_chunks": 0, "documents_found": 0, "note": "向量库为空"}

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    updated_ids = []
    updated_metadatas = []
    updated_documents = []
    seen_sources = set()

    for i, meta_dict in enumerate(all_data.get("metadatas", [])):
        needs_update = False

        # 补全 file_path（从 source 字段推算）
        if not meta_dict.get("file_path"):
            source = meta_dict.get("source", "")
            if source:
                meta_dict["file_path"] = f"uploaded_docs/{source}"
                seen_sources.add(source)
                needs_update = True

        # 补全 file_hash（老数据无此字段，留空）
        if "file_hash" not in meta_dict:
            meta_dict["file_hash"] = ""
            needs_update = True

        # 补全 added_at
        if "added_at" not in meta_dict:
            meta_dict["added_at"] = now
            needs_update = True

        if needs_update:
            updated_ids.append(all_data["ids"][i])
            updated_metadatas.append(meta_dict)
            updated_documents.append(all_data.get("documents", [""])[i] if i < len(all_data.get("documents", [])) else "")

    if updated_ids:
        col.update(ids=updated_ids, metadatas=updated_metadatas)
        print(f"   🔄 迁移完成: {len(updated_ids)} chunks / {len(seen_sources)} 文档",
              file=sys.stderr, flush=True)

    return {"migrated_chunks": len(updated_ids), "documents_found": len(seen_sources)}


# ═══════════════════════════════════════════════════════════════
# 权限管理
# ═══════════════════════════════════════════════════════════════

def update_doc_permission(file_path: str, kb_group: str = None,
                          visibility: str = None) -> dict:
    """批量更新某文档所有 chunk 的权限元数据

    适用场景：文档移动分组、修改可见性等级。
    注意：ChromaDB 的 col.update() 不支持回滚，异常场景通过 repair 校准。

    Returns: {"file_path": str, "updated_chunks": int, "error": str|None}
    """
    client = _get_client()
    try:
        col = client.get_collection("langchain")
    except Exception:
        return {"file_path": file_path, "updated_chunks": 0,
                "error": "向量库集合不存在"}

    # 优先按 file_path 匹配
    results = col.get(where={"file_path": file_path})

    # 没匹配到 → 兜底按 source（纯文件名）匹配（兼容未 migrate 的老数据）
    if not results['ids']:
        basename = os.path.basename(file_path)
        results = col.get(where={"source": basename})

    ids = results['ids']
    if not ids:
        return {"file_path": file_path, "updated_chunks": 0,
                "error": "未找到该文档的 chunk"}

    metadatas = []
    for meta in results['metadatas']:
        if kb_group is not None:
            meta['kb_group'] = kb_group
        if visibility is not None:
            meta['visibility'] = visibility
        metadatas.append(meta)

    col.update(ids=ids, metadatas=metadatas)
    print(f"   🔒 权限更新: {file_path} → {len(ids)} chunks "
          f"(kb_group={kb_group}, visibility={visibility})",
          file=sys.stderr, flush=True)

    # 同步更新 db_meta.json（用于分组页文档计数）
    rel_path = _normalize_path(file_path)
    meta = _load_meta()
    if rel_path in meta.get("documents", {}):
        if kb_group is not None:
            meta["documents"][rel_path]["kb_group"] = kb_group
        if visibility is not None:
            meta["documents"][rel_path]["visibility"] = visibility
        _save_meta(meta)

    return {"file_path": file_path, "updated_chunks": len(ids)}


# ═══════════════════════════════════════════════════════════════
# 自测代码
# ═══════════════════════════════════════════════════════════════
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    from document_loader import load_file
    from text_splitter import split_documents
    import os

    # 1. 准备测试文档
    test_file = "test_vector_sample.txt"
    with open(test_file, "w", encoding="utf-8") as f:
        f.write(
            "人工智能是计算机科学的一个分支，旨在创造能够模拟人类智能的系统。\n\n"
            "机器学习是AI的核心方法之一，通过数据训练模型来完成任务。\n\n"
            "深度学习使用多层神经网络，在图像识别和自然语言处理中取得了突破。\n\n"
            "Python是最流行的AI编程语言，拥有丰富的机器学习和深度学习库。\n\n"
            "自然语言处理（NLP）让计算机理解和生成人类语言。\n\n"
            "RAG（检索增强生成）结合了信息检索和文本生成，能有效减少大模型幻觉。\n\n"
            "大语言模型（LLM）如GPT-4、DeepSeek等，在海量文本上训练，展现出强大的语言能力。\n\n"
            "Agent是指能自主感知环境、制定计划并执行行动的智能体系统。"
        )

    # 2. 加载 → 分块 → 向量化
    print("=== Step 1: 加载文档 ===")
    docs = load_file(test_file)
    print(f"   文档页数: {len(docs)}")

    print("\n=== Step 2: 文本分块 ===")
    chunks = split_documents(docs)
    print(f"   文本块数: {len(chunks)}")

    print("\n=== Step 3: 构建向量库 ===")
    build_vector_store(chunks)

    print("\n=== Step 4: 增量管理测试 ===")
    print(f"   状态: {get_status()}")
    print(f"   文档清单: {list_documents()}")
    print(f"   去重检测: {is_duplicate(test_file)}")
