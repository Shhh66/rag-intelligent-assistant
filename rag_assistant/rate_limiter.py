"""Redis 令牌桶限流器 —— 两层限流共用（用户请求级 + LLM 调用级）。

设计要点：
1. 令牌桶算法：容量 capacity 允许突发，速率 refill 每秒平滑补充。
2. Lua 脚本原子操作：令牌补充 + 扣减 + 时间戳更新，一条命令防竞态。
3. fail-open 降级：Redis 不可用时 allow() 恒返回 True（放行），
   因为限流是「软保护」（防滥用/控成本），Redis 挂了不应阻断主链路
   ——与熔断器相反（熔断是 fail-closed：下游故障必须快速失败）。
4. key 维度隔离：用户级按 username，LLM 级按下游名，天然互不干扰。
"""

import sys
import time

_RATE_LIMIT_LUA = """
local key = KEYS[1]
local ts_key = KEYS[2]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])      -- 每秒补充令牌数
local now = tonumber(ARGV[3])
local tokens = tonumber(redis.call('get', key))
if tokens == nil then tokens = capacity end
local last = tonumber(redis.call('get', ts_key))
if last == nil then last = now end
tokens = math.min(capacity, tokens + (now - last) * refill)  -- 按经过时间补令牌，封顶
redis.call('set', ts_key, now)
if tokens >= 1 then
    redis.call('set', key, tokens - 1)
    return 1
end
redis.call('set', key, tokens)
return 0
"""


class RateLimitError(Exception):
    """限流超限时抛出（LLM 级由上层 unified_agent.chat() 捕获快速失败）。"""
    pass


class RedisTokenBucket:
    """令牌桶：容量允许突发、速率平滑补充；Lua 原子操作防竞态。

    fail-open：Redis 不可用时 allow() 恒返回 True（放行）。
    """

    def __init__(self, key_prefix, capacity, refill_per_sec, host="localhost", port=6379):
        self._key_prefix = key_prefix
        self._capacity = capacity
        self._refill = refill_per_sec
        self._client = None
        self._available = False
        try:
            import redis
            self._client = redis.Redis(host=host, port=port, decode_responses=True, protocol=2)  # RESP2：兼容 Redis 5.x（redis-py≥5 默认 RESP3 会报 HELLO）
            self._client.ping()
            self._available = True
            print(f"   🚦 限流器就绪: Redis {host}:{port}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"   ⚠️ Redis 不可用，限流降级关闭（fail-open 放行）: {e}",
                  file=sys.stderr, flush=True)

    def allow(self, identifier: str) -> bool:
        """取 1 个令牌，返回是否放行。Redis 不可用 → True（fail-open）。"""
        if not self._available:
            return True
        key = f"{self._key_prefix}:{identifier}"
        ts_key = key + ":ts"
        try:
            result = self._client.eval(
                _RATE_LIMIT_LUA, 2, key, ts_key,
                self._capacity, self._refill, time.time(),
            )
            return result == 1
        except Exception as e:
            print(f"   ⚠️ 限流检查失败(fail-open 放行): {e}", file=sys.stderr, flush=True)
            return True


# ── 全局单例（懒加载，读取 config）──

_user_limiter = None
_llm_limiter = None


def get_user_limiter() -> RedisTokenBucket:
    """用户级限流器：每个登录用户独立配额（rate:user:{username}）。"""
    global _user_limiter
    if _user_limiter is None:
        from config import (USER_RATE_LIMIT_CAPACITY, USER_RATE_LIMIT_REFILL,
                            REDIS_HOST, REDIS_PORT)
        _user_limiter = RedisTokenBucket(
            "rate:user", USER_RATE_LIMIT_CAPACITY, USER_RATE_LIMIT_REFILL,
            host=REDIS_HOST, port=REDIS_PORT)
    return _user_limiter


def get_llm_limiter() -> RedisTokenBucket:
    """LLM 级限流器：对下游模型调用频率（rate:llm:deepseek）。

    注意：当前为全局桶（全系统共用），存在「单用户刷爆桶、卡住其他用户」的
    资源抢占风险（P3 快速落地版）。P4 升级为按用户维度（deepseek:{username}）。
    """
    global _llm_limiter
    if _llm_limiter is None:
        from config import (LLM_RATE_LIMIT_CAPACITY, LLM_RATE_LIMIT_REFILL,
                            REDIS_HOST, REDIS_PORT)
        _llm_limiter = RedisTokenBucket(
            "rate:llm", LLM_RATE_LIMIT_CAPACITY, LLM_RATE_LIMIT_REFILL,
            host=REDIS_HOST, port=REDIS_PORT)
    return _llm_limiter
