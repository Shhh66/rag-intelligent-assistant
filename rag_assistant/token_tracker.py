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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 持久化文件路径（存放在 rag_assistant 目录下）
_PERSIST_FILE = Path(__file__).resolve().parent / "token_log.jsonl"


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
    ) -> TokenUsage:
        """记录一次 LLM 调用。

        Args:
            model: 模型名称
            usage: OpenAI SDK 返回的 response.usage
            call_site: 调用位置标识

        Returns:
            TokenUsage: 本次调用的用量记录
        """
        prompt_tokens = getattr(usage, 'prompt_tokens', 0)
        completion_tokens = getattr(usage, 'completion_tokens', 0)
        total_tokens = getattr(usage, 'total_tokens', prompt_tokens + completion_tokens)
        cost = self._calculate_cost(model, prompt_tokens, completion_tokens)

        record = TokenUsage(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_rmb=cost,
            timestamp=datetime.now().isoformat(),
            call_site=call_site,
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

        logger.debug(
            f"Token 记录 [{call_site}]: {model} "
            f"in={prompt_tokens} out={completion_tokens} ¥{cost:.6f}"
        )
        return record

    def _calculate_cost(
        self, model: str, prompt_tokens: int, completion_tokens: int
    ) -> float:
        """计算单次调用的费用（单位：人民币元）。"""
        try:
            from config import MODEL_PRICING, DEFAULT_PRICING
            pricing = MODEL_PRICING.get(model, DEFAULT_PRICING)
        except ImportError:
            pricing = {"input": 1.0, "output": 2.0}

        input_cost = (prompt_tokens / 1_000_000) * pricing.get("input", 1.0)
        output_cost = (completion_tokens / 1_000_000) * pricing.get("output", 2.0)
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
    tracker.record("deepseek-v4-flash", MockUsage(500, 200, 700), call_site="retriever.rag_answer")
    tracker.record("deepseek-v4-flash", MockUsage(300, 50, 350), call_site="decision_engine.decide")

    conv1 = tracker.get_conversation_diff()
    print(f"📝 第1轮对话增量: {conv1['total_tokens']} Token, ¥{conv1['total_cost']:.4f}")

    # ── 模拟第2轮对话 ──
    tracker.start_conversation()
    tracker.record("deepseek-chat", MockUsage(1000, 400, 1400), call_site="decision_engine.final_answer")

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
