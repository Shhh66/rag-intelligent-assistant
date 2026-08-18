"""工具调用熔断器 —— CLOSED/OPEN/HALF_OPEN 三态状态机（per-destination）

按「下游目的地」分区熔断：每个下游（DeepSeek / 天气 / 教务 / MCP 通道）
独立一个熔断器，一个下游故障不误伤其他下游（类比 Hystrix command key）。

与 tenacity 重试的关系（关键区分）：
  tenacity 重试 → 管「单次调用的瞬时抖动」（超时/连接闪断），微观
  熔断器       → 管「下游持续故障」（连续 N 次失败），宏观
二者叠加，维度不同，互不冲突。

HALF_OPEN 语义：冷却结束后只放行 half_open_max_probes 次试探，其余拒绝；
试探成功 → CLOSED，试探失败 → 立即回 OPEN。避免「冷却刚结束并发洪峰
全部放行、把刚恢复的下游再次打垮」。
"""

import time
import threading


class CircuitBreakerError(Exception):
    """熔断打开时抛出，表示下游不可用，请求被直接拒绝"""
    pass


class CircuitBreaker:
    """线程安全的熔断器状态机。

    状态转换：
      CLOSED ──连续失败 N 次──▶ OPEN ──冷却 T 秒──▶ HALF_OPEN
        ▲                                                  │
        │                   试探成功                        │试探失败
        └──────────────────────────────────────────────────┘
    """

    def __init__(self, failure_threshold: int = 5, cooldown_seconds: float = 30.0,
                 half_open_max_probes: int = 1):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_probes = half_open_max_probes  # HALF_OPEN 只放 N 次试探
        self.failure_count = 0
        self.state = "CLOSED"  # CLOSED | OPEN | HALF_OPEN
        self.last_failure_time = 0.0
        self.half_open_probes = 0                          # 当前已放行的试探数
        self._lock = threading.Lock()

    def allow_request(self) -> bool:
        """请求前检查是否放行。

        CLOSED → True（正常）
        OPEN 冷却期内 → False（直接拒绝，不调用下游）
        OPEN 冷却结束 → 转 HALF_OPEN，只放行 half_open_max_probes 次试探
        HALF_OPEN → 试探额度内放行，超出拒绝（防止并发放大）
        """
        with self._lock:
            if self.state == "CLOSED":
                return True
            if self.state == "OPEN":
                if time.time() - self.last_failure_time >= self.cooldown_seconds:
                    self.state = "HALF_OPEN"
                    self.half_open_probes = 0
                else:
                    return False
            # HALF_OPEN：只放行 half_open_max_probes 次试探，其余拒绝，直到试探结果复位
            if self.half_open_probes < self.half_open_max_probes:
                self.half_open_probes += 1
                return True
            return False

    def record_success(self):
        """记录一次成功，复位到 CLOSED"""
        with self._lock:
            self.failure_count = 0
            self.state = "CLOSED"
            self.half_open_probes = 0

    def record_failure(self):
        """记录一次失败，累计达到阈值则打开熔断。

        HALF_OPEN 试探失败 → 立即回 OPEN（不等再累计 N 次）。
        """
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.state == "HALF_OPEN" or self.failure_count >= self.failure_threshold:
                self.state = "OPEN"

    def status(self) -> dict:
        """返回当前状态（用于调试/监控）"""
        with self._lock:
            return {"state": self.state, "failure_count": self.failure_count,
                    "half_open_probes": self.half_open_probes}


# ── 下游目的地常量 ──
DESTINATION_MCP_CHANNEL = "mcp_channel"  # MCP 子进程通道可用性（超时/崩溃）
DESTINATION_LLM = "deepseek"             # DeepSeek LLM API
DESTINATION_WEATHER = "openweather"      # OpenWeatherMap
DESTINATION_EDU = "edu"                  # 教务系统

# ── per-destination 熔断器注册表 ──
_breakers = {}


def get_breaker(destination: str = DESTINATION_MCP_CHANNEL) -> CircuitBreaker:
    """按下游目的地取熔断器（不同下游独立熔断，互不误伤）。

    无参时返回 MCP 通道熔断器（向后兼容 mcp_client_manager 旧调用）。
    """
    global _breakers
    if destination not in _breakers:
        from config import CB_FAILURE_THRESHOLD, CB_COOLDOWN_SECONDS
        _breakers[destination] = CircuitBreaker(
            CB_FAILURE_THRESHOLD, CB_COOLDOWN_SECONDS
        )
    return _breakers[destination]


def call_llm_with_cb(client, model, messages, temperature, max_tokens, call_site):
    """统一 LLM 调用入口（主进程常驻）：熔断 + 成功/失败记录。

    client：OpenAI 实例（主进程常驻的 _llm_client，跨请求复用）。
    熔断 OPEN → 抛 CircuitBreakerError，由 unified_agent.chat() 捕获后快速失败，
    返回可读提示，不进入 ReAct 循环反复重试。

    只应在「常驻进程」里调用（主进程 decision_engine / skill_executor）。
    子进程（retriever.py）是 per-request 的，熔断状态无法跨请求累积，不适用。
    """
    breaker = get_breaker(DESTINATION_LLM)
    if not breaker.allow_request():
        raise CircuitBreakerError("DeepSeek 熔断打开，请求快速失败")
    # LLM 级限流：熔断检查之后、实际调用之前（管「频率」，不管「故障」）
    try:
        from rate_limiter import get_llm_limiter, RateLimitError
        if not get_llm_limiter().allow("deepseek"):
            raise RateLimitError("LLM 调用频率超限，请稍后重试")
    except RateLimitError:
        raise
    except Exception:
        pass  # 限流组件异常不影响主链路（rate_limiter 内部已 fail-open 兜底）
    try:
        resp = client.chat.completions.create(
            model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens,
        )
    except Exception:
        breaker.record_failure()
        raise
    breaker.record_success()
    try:
        from token_tracker import get_tracker
        get_tracker().record(model, resp.usage, call_site=call_site)
    except Exception:
        pass
    return resp
