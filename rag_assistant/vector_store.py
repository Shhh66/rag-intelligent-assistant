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
)


# ═══════════════════════════════════════════════════════════════
# 全局缓存
# ═══════════════════════════════════════════════════════════════

_embeddings = None


# ═══════════════════════════════════════════════════════════════
# 嵌入模型（保持不变）
# ═══════════════════════════════════════════════════════════════

def _model_is_cached() -> bool:
    """检查嵌入模型是否已在本地缓存"""
    model_name = f"sentence-transformers/{EMBEDDING_MODEL}"
    result = try_to_load_from_cache(
        repo_id=model_name,
        filename="model.safetensors",
    )
    return result is not None


def get_embeddings():
    """获取嵌入模型（懒加载，优先本地缓存，否则从镜像下载）"""
    global _embeddings
    if _embeddings is None:
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
    """加载已有的向量库"""
    embeddings = get_embeddings()
    db_path = Path(VECTOR_DB_PATH)
    if not db_path.exists() or not list(db_path.iterdir()):
        raise FileNotFoundError(f"向量库不存在: {VECTOR_DB_PATH}\n请先上传文档构建知识库。")
    return Chroma(
        persist_directory=str(db_path),
        embedding_function=embeddings,
        collection_name="langchain",
    )


def search(query: str, top_k: int = TOP_K):
    """在向量库中检索与 query 最相似的文档片段"""
    vector_store = load_vector_store()
    results = vector_store.similarity_search(query, k=top_k)
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
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
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

    # 清理超出数量的旧快照
    snapshots = sorted(snapshot_dir.glob("snapshot_*.json"), reverse=True)
    for old in snapshots[SNAPSHOT_MAX_COUNT:]:
        old.unlink()
        old_bak = snapshot_dir / old.name.replace("snapshot_", "db_meta.").replace(".json", ".bak")
        if old_bak.exists():
            old_bak.unlink()

    return timestamp


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

    # 恢复 db_meta.json
    bak_path = snapshot_dir / snapshot.get("db_meta_backup", "")
    if bak_path.exists():
        shutil.copy2(bak_path, _get_meta_path())
        meta = _load_meta()
        doc_count = len(meta.get("documents", {}))
        return {
            "restored_documents": doc_count,
            "timestamp": timestamp,
            "note": f"已恢复到 {timestamp} 的快照状态。注意：向量库中的 chunk 无法回退（ChromaDB 不支持），建议用 kb_manager.py repair 校验一致性。",
        }
    else:
        return {"restored_documents": 0, "note": "快照备份文件缺失"}


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

def add_document(file_path: str, skip_duplicate: bool = True) -> dict:
    """增量添加单个文档到向量库

    安全流程：锁 → 校验模型 → 去重 → 解析切块 → 批量写入 → 回滚异常 → 更新meta → 释放锁

    Returns: {"file_path": str, "chunks_added": int, "skipped": bool}
    """
    lock = _get_lock()
    try:
        lock.acquire()
        return _add_document_locked(file_path, skip_duplicate)
    except LockTimeout:
        return {"file_path": _normalize_path(file_path), "chunks_added": 0, "skipped": False,
                "error": "知识库正在操作中，请稍后再试"}
    finally:
        try:
            lock.release()
        except Exception:
            pass


def _add_document_locked(file_path: str, skip_duplicate: bool) -> dict:
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

    chunks = split_documents(docs)
    if not chunks:
        return {"file_path": rel_path, "chunks_added": 0, "skipped": False,
                "error": "文档分块后无内容"}

    # 注入 metadata 到每个 chunk
    for chunk in chunks:
        chunk.metadata["file_path"] = rel_path
        chunk.metadata["file_hash"] = file_hash
        if "added_at" not in chunk.metadata:
            chunk.metadata["added_at"] = now

    embeddings = get_embeddings()

    # 批量写入 Chroma（使用稳定 ID，天然幂等）
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
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

    # 快照备份
    _backup_snapshot(f"remove: {rel_path}")

    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
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

    # 快照备份
    _backup_snapshot(f"update: {rel_path}")

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
    for doc in docs:
        doc.metadata["file_path"] = rel_path
        doc.metadata["file_hash"] = new_hash
        doc.metadata["added_at"] = now

    chunks = split_documents(docs)
    if not chunks:
        return {"file_path": rel_path, "chunks_removed": 0, "chunks_added": 0,
                "error": "文档分块后无内容"}

    for chunk in chunks:
        chunk.metadata["file_path"] = rel_path
        chunk.metadata["file_hash"] = new_hash
        if "added_at" not in chunk.metadata:
            chunk.metadata["added_at"] = now

    # 2. 先写入新版本（用稳定 ID 天然幂等）
    embeddings = get_embeddings()
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
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

    # 3. 写入成功 → 删除旧版本 chunks
    chunks_removed = 0
    if old_hash:
        old_ids = col.get(where={"file_path": rel_path})
        # 过滤掉新写入的 ID
        old_ids_to_delete = [id_ for id_ in old_ids['ids'] if id_ not in chunk_ids]
        if old_ids_to_delete:
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

    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
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
    client = chromadb.PersistentClient(path=VECTOR_DB_PATH)
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
