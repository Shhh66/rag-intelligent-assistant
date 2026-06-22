"""工具向量预筛选

用 Embedding 相似度预筛选候选工具，降低注入 LLM 的上下文长度。

原理：
1. 初始化时对所有工具描述做 Embedding 并缓存
2. 每次用户查询时，对查询做 Embedding
3. 计算余弦相似度，取 top-N 个工具
4. 仅将 top-N 工具的描述注入 Prompt

优势：当工具数量增长到 20+ 时，避免 Prompt 过长导致推理质量下降和成本增加。
"""

import logging
import sys

import numpy as np

from .tool_registry import ToolMeta

logger = logging.getLogger(__name__)


class ToolVectorFilter:
    """用 Embedding 相似度预筛选候选工具。

    复用 vector_store.py 中的 get_embeddings() 获取同一个嵌入模型实例，
    避免重复加载模型。
    """

    def __init__(
        self,
        embedding_model_name: str = "paraphrase-multilingual-MiniLM-L12-v2",
        top_n: int = 5,
    ):
        self.embedding_model_name = embedding_model_name
        self.top_n = top_n
        self._tool_names: list[str] = []
        self._tool_vectors: np.ndarray | None = None

    def build_index(self, tools: list[ToolMeta]) -> None:
        """对所有工具描述做 Embedding，构建工具向量索引。

        嵌入内容 = f"{tool.name}: {tool.description}"
        """
        if not tools:
            logger.warning("工具列表为空，跳过向量索引构建")
            return

        try:
            # 复用 vector_store.py 中的全局嵌入模型
            sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parent.parent))
            from vector_store import get_embeddings

            embeddings = get_embeddings()
            texts = [f"{t.name}: {t.description}" for t in tools]
            vectors = embeddings.embed_documents(texts)

            self._tool_vectors = np.array(vectors, dtype=np.float32)
            self._tool_names = [t.name for t in tools]

            logger.info(
                f"工具向量索引构建完成: {len(self._tool_names)} 个工具, "
                f"向量维度: {self._tool_vectors.shape}"
            )
        except Exception as e:
            logger.error(f"工具向量索引构建失败: {e}")
            self._tool_vectors = None
            self._tool_names = []
            raise  # 重新抛出，让调用方正确处理 _vector_filter_ready

    def filter(self, query: str) -> list[str]:
        """返回与用户查询最相关的 top-N 个工具名称。

        相似度计算：余弦相似度。
        如果工具总数 <= top_n，直接返回全部。
        """
        if self._tool_vectors is None or not self._tool_names:
            return self._tool_names

        # 工具数不超过 top_n，全部返回
        if len(self._tool_names) <= self.top_n:
            return list(self._tool_names)

        try:
            from vector_store import get_embeddings

            embeddings = get_embeddings()
            query_vec = np.array(embeddings.embed_query(query), dtype=np.float32)

            # 余弦相似度：归一化后点积
            tool_norms = np.linalg.norm(self._tool_vectors, axis=1)
            query_norm = np.linalg.norm(query_vec)

            if query_norm == 0:
                return list(self._tool_names)[:self.top_n]

            similarities = np.dot(self._tool_vectors, query_vec) / (tool_norms * query_norm)
            top_indices = np.argsort(similarities)[-self.top_n:][::-1]

            result = [self._tool_names[i] for i in top_indices]
            logger.debug(f"向量预筛选: {result}")
            return result

        except Exception as e:
            logger.warning(f"向量预筛选失败，返回全部工具: {e}")
            return list(self._tool_names)

    @property
    def is_ready(self) -> bool:
        """检查索引是否已构建。"""
        return self._tool_vectors is not None and len(self._tool_names) > 0
