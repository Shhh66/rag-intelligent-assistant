"""Token 用量追踪器

会话级 Token 统计与成本计算。所有 LLM 调用点统一通过此模块记录用量。

特性：
- 每条调用实时持久化到 token_log.jsonl，Streamlit 重启后历史不丢失
- 三层统计：上一次问答 / 本次会话累计 / 历史总计
- 自动按模型分别统计

用法：
    from token_tracker import get_tracker

    tracker = get_tracker()
    tracker.start_conversation()          # 每轮问答前调用（记录快照）
    response = client.chat.completions.create(...)
    tracker.record(model_name, response.usage, call_site="...")

    # 侧边栏展示
    conv = tracker.get_conversation_diff()    # 当前对话增量
    sess = tracker.get_session_summary()      # 本次会话累计
    hist = tracker.get_all_time_summary()     # 历史总计
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 先触发 config 加载：config 内部会 load_dotenv 把 .env 注入 os.environ。
# 否则本模块若先于 config 被导入（如被 circuit_breaker 间接引入），
# RUNTIME_DATA_DIR 还没进环境变量，下面的路径会退回项目目录 —— 曾因此导致
# 日志分裂到两处（项目根目录 + runtime_data/）。
try:
    import config as _config  # noqa: F401
except Exception:
    pass

# 持久化文件路径：设了 RUNTIME_DATA_DIR 就放进去（Docker 挂载卷持久化），
# 否则用项目目录（原行为，容器重建会丢）
_PERSIST_FILE = Path(
    os.getenv("RUNTIME_DATA_DIR") or Path(__file__).resolve().parent
) / "token_log.jsonl"

# config 不可用时的兜底定价（结构须与 config.MODEL_PRICING 一致，按 Flash 空闲档）
_FALLBACK_PRICING = {
    "input_cache_hit":  {"off_peak": 0.02, "peak": 0.04},
    "input_cache_miss": {"off_peak": 1.0,  "peak": 2.0},
    "output":           {"off_peak": 4.0,  "peak": 8.0},
}

# 北京时间（DeepSeek 按时段计价，用固定偏移换算，不依赖运行机器时区）
_BEIJING_TZ = timezone(timedelta(hours=8))


def is_peak_hour(dt: Optional[datetime] = None) -> bool:
    """是否处于 DeepSeek 高峰时段。

    高峰 = 北京时间 周一至周五 9:00-12:00、14:00-18:00（其余为空闲档，价格减半）。
    """
    try:
        from config import PEAK_HOURS_WEEKDAY
        windows = PEAK_HOURS_WEEKDAY
    except Exception:
        windows = [(9, 12), (14, 18)]

    now = dt.astimezone(_BEIJING_TZ) if dt else datetime.now(_BEIJING_TZ)
    if now.weekday() >= 5:          # 5=周六, 6=周日 → 全天空闲
        return False
    return any(start <= now.hour < end for start, end in windows)


def extract_cache_hit_tokens(usage) -> int:
    """从 usage 中取「缓存命中」的输入 token 数。

    DeepSeek 用 prompt_cache_hit_tokens；OpenAI 风格用 prompt_tokens_details.cached_tokens。
    取不到时返回 0 —— 即全部按「未命中」计价，偏保守，不会低估成本。
    """
    if usage is None:
        return 0
    for attr in ("prompt_cache_hit_tokens", "cache_hit_tokens"):
        v = getattr(usage, attr, None)
        if isinstance(v, int):
            return v
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        v = getattr(details, "cached_tokens", None)
        if isinstance(v, int):
            return v
    if isinstance(usage, dict):          # dict 形态兜底
        v = usage.get("prompt_cache_hit_tokens")
        if isinstance(v, int):
            return v
    return 0


@dataclass
class TokenUsage:
    """单次 LLM 调用的 Token 用量记录"""
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_rmb: float = 0.0
    timestamp: str = ""
    call_site: str = ""
    cache_hit_tokens: int = 0      # 输入中「缓存命中」的 token 数（按低价计）
    is_peak: bool = False          # 是否按高峰价计费（便于审计成本口径）


class TokenTracker:
    """Token 用量追踪器。

    三层数据模型：
    - 会话级（session）：Streamlit 页面加载以来的所有调用，不主动清空
    - 对话级（conversation）：自上次 start_conversation() 以来的增量
    - 历史级（all-time）：从持久化文件加载的累计统计，跨重启保留
    """

    def __init__(self):
        # ── 会话级（内存，页面刷新即清空）──
        self._calls: list[TokenUsage] = []
        self._session_start = datetime.now()
        self._total_input: int = 0
        self._total_output: int = 0
        self._total_cost: float = 0.0
        self._model_stats: dict[str, dict] = {}  # model → {input, output, cost, calls}

        # ── 对话级（start_conversation 时的快照）──
        self._conv_snapshot: dict = {
            "total_input": 0, "total_output": 0, "total_cost": 0.0, "call_count": 0
        }

        # ── 历史级（从文件加载，跨重启保留）──
        self._all_time: dict = {
            "total_input": 0, "total_output": 0, "total_tokens": 0,
            "total_cost": 0.0, "call_count": 0,
        }
        self._load_history()

    # ── 核心记录方法 ────────────────────────────────────────────

    def record(
        self,
        model: str,
        usage,
        call_site: str = "",
        latency_ms: float = None,
        input=None,
        output=None,
        reasoning: str = None,
    ) -> TokenUsage:
        """记录一次 LLM 调用。

        Args:
            model: 模型名称
            usage: OpenAI SDK 返回的 response.usage
            call_site: 调用位置标识
            latency_ms: 本次 LLM 调用耗时（毫秒），仅用于 LangFuse generation 的耗时展示
            input: 已脱敏截断的入参摘要（由 observability.summarize_messages 产出），
                   为 None 时 LangFuse 上该字段留空
            output: 已脱敏截断的出参，为 None 时留空
            reasoning: 推理模型的思维链（已截断），仅写入 metadata，不占 output 字段

        Returns:
            TokenUsage: 本次调用的用量记录
        """
        prompt_tokens = getattr(usage, 'prompt_tokens', 0)
        completion_tokens = getattr(usage, 'completion_tokens', 0)
        total_tokens = getattr(usage, 'total_tokens', prompt_tokens + completion_tokens)
        peak = is_peak_hour()
        cost = self._calculate_cost(model, prompt_tokens, completion_tokens, usage)

        record = TokenUsage(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_rmb=cost,
            timestamp=datetime.now().isoformat(),
            call_site=call_site,
            cache_hit_tokens=extract_cache_hit_tokens(usage),
            is_peak=peak,
        )

        # 会话级累积
        self._calls.append(record)
        self._total_input += prompt_tokens
        self._total_output += completion_tokens
        self._total_cost += cost

        # 按模型统计
        if model not in self._model_stats:
            self._model_stats[model] = {"input": 0, "output": 0, "cost": 0.0, "calls": 0}
        self._model_stats[model]["input"] += prompt_tokens
        self._model_stats[model]["output"] += completion_tokens
        self._model_stats[model]["cost"] += cost
        self._model_stats[model]["calls"] += 1

        # 实时持久化到文件
        self._persist_record(record)

        # 上报 LangFuse generation（降级安全，复用当前 trace_id）
        try:
            from observability import obs_generation
            gmeta = {"call_site": call_site, "cost_rmb": cost}
            if reasoning:
                gmeta["reasoning_content"] = reasoning
            obs_generation(
                trace_id=getattr(self, "_current_trace_id", ""),
                name=call_site or "llm",
                model=model,
                usage=usage,
                input=input,
                output=output,
                metadata=gmeta,
                latency_ms=latency_ms,
            )
        except Exception:
            pass

        logger.debug(
            f"Token 记录 [{call_site}]: {model} "
            f"in={prompt_tokens}(缓存命中 {record.cache_hit_tokens}) "
            f"out={completion_tokens} "
            f"{'高峰' if peak else '空闲'}档 ¥{cost:.6f}"
        )
        return record

    def _calculate_cost(
        self, model: str, prompt_tokens: int, completion_tokens: int, usage=None
    ) -> float:
        """计算单次调用的费用（单位：人民币元）。

        按 DeepSeek 现行定价，两个维度影响单价：
        - **时段**：高峰价 = 空闲价 × 2（北京时间 周一至周五 9-12 点、14-18 点）
        - **输入缓存**：命中价远低于未命中，两者分开计价

        usage 用于取「缓存命中 token 数」；取不到则全部按未命中计（偏保守）。
        """
        try:
            from config import MODEL_PRICING, DEFAULT_PRICING
            pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        except Exception:
            pricing = _FALLBACK_PRICING

        slot = "peak" if is_peak_hour() else "off_peak"

        # 缓存命中数做上下界防御：不应为负，也不应超过输入总数
        hit = extract_cache_hit_tokens(usage)
        hit = min(max(hit, 0), max(prompt_tokens, 0))
        miss = max(prompt_tokens, 0) - hit

        input_cost = (
            miss / 1_000_000 * pricing["input_cache_miss"][slot]
            + hit / 1_000_000 * pricing["input_cache_hit"][slot]
        )
        output_cost = (completion_tokens / 1_000_000) * pricing["output"][slot]
        return input_cost + output_cost

    # ── 对话边界管理 ────────────────────────────────────────────

    def start_conversation(self):
        """标记新一轮问答的开始。

        保存当前会话累计的快照，后续 get_conversation_diff()
        返回自此刻以来的增量统计。
        """
        self._conv_snapshot = {
            "total_input": self._total_input,
            "total_output": self._total_output,
            "total_cost": self._total_cost,
            "call_count": len(self._calls),
        }

    def set_trace_id(self, trace_id: str):
        """设置当前对话的 trace_id，供 record() 上报 LangFuse generation 归并链路。"""
        self._current_trace_id = trace_id

    def get_conversation_diff(self) -> dict:
        """获取当前对话的增量统计（自上次 start_conversation 后）。

        Returns:
            dict: {"total_tokens": int, "total_cost": float, "call_count": int,
                   "total_input": int, "total_output": int}
        """
        snap = self._conv_snapshot
        return {
            "total_input": self._total_input - snap["total_input"],
            "total_output": self._total_output - snap["total_output"],
            "total_tokens": (self._total_input - snap["total_input"])
            + (self._total_output - snap["total_output"]),
            "total_cost": round(self._total_cost - snap["total_cost"], 6),
            "call_count": len(self._calls) - snap["call_count"],
        }

    # ── 三层查询 ────────────────────────────────────────────────

    def get_session_summary(self) -> dict:
        """获取本次 Streamlit 会话的累计统计。

        Returns:
            dict: total_input, total_output, total_tokens, total_cost,
                  call_count, model_stats, session_duration_s, last_call
        """
        duration = (datetime.now() - self._session_start).total_seconds()
        return {
            "total_input": self._total_input,
            "total_output": self._total_output,
            "total_tokens": self._total_input + self._total_output,
            "total_cost": round(self._total_cost, 6),
            "call_count": len(self._calls),
            "model_stats": dict(self._model_stats),
            "session_duration_s": round(duration, 1),
            "last_call": self._to_dict(self._calls[-1]) if self._calls else None,
        }

    def get_all_time_summary(self) -> dict:
        """获取历史总计（含当前会话 + 文件中加载的历史）。

        Returns:
            dict: total_input, total_output, total_tokens, total_cost, call_count
        """
        return {
            "total_input": self._all_time["total_input"] + self._total_input,
            "total_output": self._all_time["total_output"] + self._total_output,
            "total_tokens": (self._all_time["total_tokens"]
                             + self._total_input + self._total_output),
            "total_cost": round(self._all_time["total_cost"] + self._total_cost, 6),
            "call_count": self._all_time["call_count"] + len(self._calls),
        }

    def get_last_usage(self) -> Optional[dict]:
        """获取最近一次调用的 Token 用量。"""
        if not self._calls:
            return None
        return self._to_dict(self._calls[-1])

    def get_call_history(self, limit: int = 50) -> list[dict]:
        """获取最近 N 条调用明细（含文件历史 + 当前会话）。"""
        # 先从文件读取历史，再追加当前会话
        file_records = self._read_recent_from_file(limit)
        session_records = [self._to_dict(c) for c in self._calls[-limit:]]
        combined = file_records + session_records
        return combined[-limit:]

    # ── 持久化 ──────────────────────────────────────────────────

    def _persist_record(self, record: TokenUsage):
        """实时将单条记录追加写入 JSONL 文件。"""
        try:
            data = self._to_dict(record)
            data["session_id"] = self._session_start.strftime("%Y%m%d_%H%M%S")
            with open(_PERSIST_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        except Exception as e:
            logger.warning(f"Token 记录持久化失败: {e}")

    def _load_history(self):
        """从持久化文件加载历史总计（用于跨重启统计）。"""
        if not _PERSIST_FILE.exists():
            return

        try:
            total_input = 0
            total_output = 0
            total_cost = 0.0
            count = 0

            with open(_PERSIST_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        total_input += entry.get("prompt_tokens", 0)
                        total_output += entry.get("completion_tokens", 0)
                        total_cost += entry.get("cost_rmb", 0)
                        count += 1
                    except json.JSONDecodeError:
                        continue

            self._all_time = {
                "total_input": total_input,
                "total_output": total_output,
                "total_tokens": total_input + total_output,
                "total_cost": total_cost,
                "call_count": count,
            }
            logger.info(
                f"从 {_PERSIST_FILE} 加载历史: "
                f"{count} 次调用, {total_input + total_output:,} Token, "
                f"¥{total_cost:.4f}"
            )
        except Exception as e:
            logger.warning(f"加载 Token 历史失败: {e}")

    @staticmethod
    def _read_recent_from_file(limit: int) -> list[dict]:
        """从持久化文件中读取最近 N 条记录。"""
        if not _PERSIST_FILE.exists():
            return []

        records: list[dict] = []
        try:
            with open(_PERSIST_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass
        return records[-limit:]

    # ── 重置 ────────────────────────────────────────────────────

    def reset(self):
        """清空当前会话的内存统计（文件历史不删除）。

        用于用户点击"清空对话"按钮时调用。
        注意：这只会清空内存中的会话累计，持久化文件中的历史记录不会被删除。
        """
        self._calls.clear()
        self._total_input = 0
        self._total_output = 0
        self._total_cost = 0.0
        self._model_stats.clear()
        self._session_start = datetime.now()
        self._conv_snapshot = {
            "total_input": 0, "total_output": 0, "total_cost": 0.0, "call_count": 0
        }
        logger.info("Token 追踪器已重置（文件历史保留）")

    def reset_all(self):
        """清空内存统计并删除持久化文件。慎用。"""
        self.reset()
        try:
            if _PERSIST_FILE.exists():
                _PERSIST_FILE.unlink()
                logger.info(f"已删除持久化文件: {_PERSIST_FILE}")
        except Exception as e:
            logger.warning(f"删除持久化文件失败: {e}")
        self._all_time = {
            "total_input": 0, "total_output": 0, "total_tokens": 0,
            "total_cost": 0.0, "call_count": 0,
        }

    # ── 工具方法 ────────────────────────────────────────────────

    @staticmethod
    def _to_dict(record: TokenUsage) -> dict:
        return {
            "model": record.model,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
            "cost_rmb": round(record.cost_rmb, 6),
            "timestamp": record.timestamp,
            "call_site": record.call_site,
            "cache_hit_tokens": record.cache_hit_tokens,
            "is_peak": record.is_peak,
        }

    @property
    def total_tokens(self) -> int:
        return self._total_input + self._total_output

    @property
    def total_cost(self) -> float:
        return self._total_cost

    @property
    def call_count(self) -> int:
        return len(self._calls)


# ── 全局单例 ──────────────────────────────────────────────────

_tracker: Optional[TokenTracker] = None


def get_tracker() -> TokenTracker:
    """获取全局 TokenTracker 单例。"""
    global _tracker
    if _tracker is None:
        _tracker = TokenTracker()
    return _tracker


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== Token 追踪器自测（含持久化）===\n")

    # 模拟 OpenAI response.usage 对象
    class MockUsage:
        def __init__(self, prompt, completion, total):
            self.prompt_tokens = prompt
            self.completion_tokens = completion
            self.total_tokens = total

    # 清理旧测试文件
    test_file = Path("token_log.jsonl")
    if test_file.exists():
        test_file.unlink()

    # 用临时文件测试（修改全局路径）
    import token_tracker
    token_tracker._PERSIST_FILE = test_file

    tracker = TokenTracker()

    # ── 模拟第1轮对话 ──
    tracker.start_conversation()
    tracker.record("deepseek-flash", MockUsage(500, 200, 700), call_site="retriever.rag_answer")
    tracker.record("deepseek-flash", MockUsage(300, 50, 350), call_site="decision_engine.decide")

    conv1 = tracker.get_conversation_diff()
    print(f"📝 第1轮对话增量: {conv1['total_tokens']} Token, ¥{conv1['total_cost']:.4f}")

    # ── 模拟第2轮对话 ──
    tracker.start_conversation()
    tracker.record("deepseek-flash", MockUsage(1000, 400, 1400), call_site="decision_engine.final_answer")

    conv2 = tracker.get_conversation_diff()
    print(f"📝 第2轮对话增量: {conv2['total_tokens']} Token, ¥{conv2['total_cost']:.4f}")

    # ── 三层统计 ──
    sess = tracker.get_session_summary()
    hist = tracker.get_all_time_summary()

    print(f"\n📊 会话累计（3次调用）: {sess['total_tokens']:,} Token, ¥{sess['total_cost']:.4f}")
    print(f"📊 历史总计（含文件）: {hist['total_tokens']:,} Token, ¥{hist['total_cost']:.4f}")

    # ── 验证文件持久化 ──
    print(f"\n💾 持久化文件存在: {test_file.exists()}")
    if test_file.exists():
        with open(test_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        print(f"💾 文件行数: {len(lines)} (预期 3)")

    # ── 模拟重启：新 TokenTracker 实例应从文件加载历史 ──
    tracker2 = TokenTracker()
    hist2 = tracker2.get_all_time_summary()
    print(f"\n🔄 模拟重启后加载历史: {hist2['call_count']} 次调用, "
          f"{hist2['total_tokens']:,} Token, ¥{hist2['total_cost']:.4f}")

    # 清理
    test_file.unlink()
    print(f"\n🎉 Token 追踪器自测完成！")
