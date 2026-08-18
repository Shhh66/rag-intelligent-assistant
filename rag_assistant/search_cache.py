"""检索结果缓存 —— Redis 分布式缓存，key 含权限维度，TTL 自动过期。

设计要点：
1. 缓存「检索结果（合并去重后的 docs）」，不是「最终答案」——答案依赖 LLM 语义命中率低，
   检索结果（向量 + BM25 + 翻译）是重计算，缓存收益最大。
2. key = md5(query + 排序后 kb_groups)：权限维度进 key 防跨租户串台；md5 缩短超长 query。
3. TTL 用 Redis setex 自动管理，不自己造 LRU 轮子。
4. Document 序列化为 JSON 落缓存，反序列化回独立副本，重排污染不影响缓存。

Redis 不可用时安全降级（get/set 均 try 包裹），绝不阻断主检索链路。
"""

import json
import hashlib
import sys

from langchain_core.documents import Document


def _serialize_docs(docs) -> str:
    """Document 列表序列化为 JSON（落缓存前序列化隔离）。"""
    return json.dumps(
        [{"page_content": d.page_content, "metadata": d.metadata} for d in docs],
        ensure_ascii=False,
    )


def _deserialize_docs(data: str) -> list:
    """JSON 反序列化回 Document 列表（独立副本，可安全重排）。"""
    return [
        Document(page_content=item["page_content"], metadata=item["metadata"])
        for item in json.loads(data)
    ]


class SearchCache:
    """检索结果缓存：Redis 分布式缓存，多租户隔离 + TTL 自动过期。"""

    def __init__(self, ttl: int = 600, host: str = "localhost", port: int = 6379):
        self._ttl = ttl
        self._client = None
        self._available = False
        try:
            import redis
            self._client = redis.Redis(host=host, port=port, decode_responses=True, protocol=2)  # RESP2：兼容 Redis 5.x（redis-py≥5 默认 RESP3 会报 HELLO）
            self._client.ping()
            self._available = True
            print(f"   📦 检索缓存就绪: Redis {host}:{port}", file=sys.stderr, flush=True)
        except Exception as e:
            # redis 库缺失 或 Redis 服务未启动 → 降级关闭缓存，不阻断检索
            print(f"   ⚠️ Redis 不可用，检索缓存降级关闭: {e}", file=sys.stderr, flush=True)

    def _key(self, query: str, kb_groups: list) -> str:
        # 权限维度进 key：不同权限用户缓存隔离，防串台
        # md5 缩短 key，避免超长 query 撑爆 Redis key
        g = ",".join(sorted(kb_groups or []))
        return "search:" + hashlib.md5(f"{query}|{g}".encode("utf-8")).hexdigest()

    def get(self, query: str, kb_groups: list):
        if not self._available:
            return None
        try:
            data = self._client.get(self._key(query, kb_groups))
            return _deserialize_docs(data) if data else None
        except Exception as e:
            print(f"   ⚠️ 缓存读取失败(忽略): {e}", file=sys.stderr, flush=True)
            return None

    def set(self, query: str, kb_groups: list, docs):
        if not self._available or not docs:
            return
        try:
            # setex = SET + EXPIRE，一条命令搞定写入 + TTL
            self._client.setex(self._key(query, kb_groups), self._ttl,
                               _serialize_docs(docs))
        except Exception as e:
            print(f"   ⚠️ 缓存写入失败(忽略): {e}", file=sys.stderr, flush=True)

    def invalidate(self):
        """知识库变更后清空所有 search:* 前缀的 key（scan 避免阻塞）。"""
        if not self._available:
            return
        try:
            for key in self._client.scan_iter("search:*"):
                self._client.delete(key)
        except Exception as e:
            print(f"   ⚠️ 缓存清空失败(忽略): {e}", file=sys.stderr, flush=True)


# 全局单例（懒加载，读取 config 配置）
_cache = None


def get_cache() -> SearchCache:
    global _cache
    if _cache is None:
        from config import SEARCH_CACHE_TTL, REDIS_HOST, REDIS_PORT
        _cache = SearchCache(ttl=SEARCH_CACHE_TTL, host=REDIS_HOST, port=REDIS_PORT)
    return _cache
