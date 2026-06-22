"""反思记忆

留存工具选择历史，为后续相似查询提供选型参考。

存储方式：内存 deque + 词级 Jaccard 相似度匹配。
容量控制：FIFO，超过 max_entries 时淘汰最早记录。
"""

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class ReflectionEntry:
    """单条工具选择反思记录"""
    timestamp: datetime = field(default_factory=datetime.now)
    query: str = ""
    selected_tool: str = ""
    success: bool = True
    result_preview: str = ""       # 结果前 200 字符
    latency_ms: float = 0.0


class ReflectionMemory:
    """反思记忆：留存工具选择历史，为后续相似查询提供选型参考。

    存储方式：内存列表 + 简单关键词匹配召回。
    容量控制：FIFO，超过 max_entries 时淘汰最早记录。
    """

    def __init__(self, max_entries: int = 50):
        self._entries: deque[ReflectionEntry] = deque(maxlen=max_entries)
        self.max_entries = max_entries

    def record(self, entry: ReflectionEntry) -> None:
        """记录一次工具选择。

        如果超过 max_entries，deque 自动移除最早的记录。
        """
        if not entry.timestamp:
            entry.timestamp = datetime.now()
        self._entries.append(entry)
        logger.debug(f"反思记忆记录: {entry.query[:40]} → {entry.selected_tool}")

    def get_relevant_hints(self, query: str, limit: int = 5) -> list[str]:
        """根据查询召回相关的历史工具选择记录，格式化为 Prompt 提示。

        匹配策略（简单高效）：
        1. 对 query 和每条记录的 query 做词级 Jaccard 相似度
        2. 选择相似度 > 0.15 的记录（或 top-N）
        3. 格式化为：f"[参考] 历史问题「{q}」→ 选择了「{tool}」(成功/失败, {latency}ms)"

        如果无匹配记录，返回空列表。
        """
        if not self._entries:
            return []

        # 中文分词：简单按字符 bigram 切分（兼顾效果和性能）
        def to_ngrams(text: str, n: int = 2) -> set:
            text = text.lower().strip()
            if not text:
                return set()
            # 字符级 n-gram
            return {text[i:i + n] for i in range(len(text) - n + 1)}

        query_ngrams = to_ngrams(query)

        scored = []
        for entry in self._entries:
            entry_ngrams = to_ngrams(entry.query)
            if not query_ngrams or not entry_ngrams:
                continue

            intersection = query_ngrams & entry_ngrams
            union = query_ngrams | entry_ngrams
            score = len(intersection) / len(union) if union else 0
            scored.append((score, entry))

        # 按相似度降序排列
        scored.sort(key=lambda x: x[0], reverse=True)

        hints = []
        for score, entry in scored[:limit]:
            if score < 0.15:  # 相似度太低，跳过
                break
            status = "成功" if entry.success else "失败"
            hints.append(
                f"[参考] 历史问题「{entry.query[:60]}」→ "
                f"使用工具「{entry.selected_tool}」"
                f"({status}, {entry.latency_ms:.0f}ms)"
            )

        if hints:
            logger.debug(f"反思提示: {len(hints)} 条匹配记录")
        return hints

    def clear(self) -> None:
        """清空全部反思记录。"""
        self._entries.clear()
        logger.info("反思记忆已清空")

    @property
    def size(self) -> int:
        return len(self._entries)
