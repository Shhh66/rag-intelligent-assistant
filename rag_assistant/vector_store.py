"""向量嵌入与存储模块 —— 将文本块向量化并存入 ChromaDB"""

import os
from typing import List
from pathlib import Path

from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings
from langchain_chroma import Chroma
from huggingface_hub import try_to_load_from_cache
from config import VECTOR_DB_PATH, EMBEDDING_MODEL, TOP_K, HF_ENDPOINT


# 全局缓存：嵌入模型只加载一次
_embeddings = None


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
            # 已缓存：离线加载，不走网络
            print(f"   ⏳ 加载本地嵌入模型（使用缓存，离线模式）...")
            _embeddings = HuggingFaceEmbeddings(
                model_name=model_name,
                model_kwargs={"device": "cpu", "local_files_only": True},
            )
        else:
            # 未缓存：从镜像下载
            if "HF_ENDPOINT" not in os.environ:
                os.environ["HF_ENDPOINT"] = HF_ENDPOINT
            print(f"   ⏳ 下载嵌入模型（镜像: {HF_ENDPOINT}，仅首次需要）...")
            _embeddings = HuggingFaceEmbeddings(
                model_name=model_name,
                model_kwargs={"device": "cpu"},
            )
        print(f"   ✅ 嵌入模型加载完成: {EMBEDDING_MODEL}")
    return _embeddings


def build_vector_store(docs: List[Document]) -> Chroma:
    """将文档块向量化并存入 ChromaDB"""
    embeddings = get_embeddings()

    db_path = Path(VECTOR_DB_PATH)
    db_path.mkdir(parents=True, exist_ok=True)

    # 用 ChromaDB 自身 API 清理旧数据（避免文件锁冲突）
    import chromadb
    client = chromadb.PersistentClient(path=str(db_path))
    try:
        client.delete_collection("langchain")
        print("   🗑 已清空旧向量库")
    except Exception:
        pass  # 首次构建，不存在旧集合

    print(f"   📊 正在向量化 {len(docs)} 个文本块...")
    vector_store = Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        persist_directory=str(db_path),
        collection_name="langchain",
    )
    print(f"   ✅ 向量库构建完成！存储位置: {VECTOR_DB_PATH}")
    return vector_store


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


# ===== 自测代码 =====
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

    # 3. 测试检索
    print("\n=== Step 4: 测试检索 ===")
    test_queries = [
        "什么是机器学习？",
        "Python在AI中有什么地位？",
        "RAG是什么？",
    ]
    for q in test_queries:
        print(f"\n🔍 问题: {q}")
        results = search(q, top_k=2)
        for i, doc in enumerate(results, 1):
            print(f"   第{i}名（相似度）: {doc.page_content[:100]}...")

    print("\n🎉 向量库模块全部通过！")
    os.remove(test_file)
