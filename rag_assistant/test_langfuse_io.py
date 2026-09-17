"""P0 验证：LangFuse generation 的入参/出参上报（假 LLM，零 API 成本，确定性）。

覆盖：
  1. summarize_messages —— 逐条截断 / 保留 role / 长文本标注省略量
  2. extract_output     —— 正文与 reasoning_content 分离，各自截断
  3. record() 透传      —— obs_generation 确实收到 input/output/reasoning
  4. call_llm_with_cb   —— 端到端：假 OpenAI client，验证收口点传值
  5. 总开关关闭         —— 不采即不传（input/output 留空，非「取消截断」）

运行：python test_langfuse_io.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import config
import observability
import token_tracker
from observability import summarize_messages, extract_output

PASS = []


def _check(cond, label):
    assert cond, f"❌ 失败: {label}"
    PASS.append(label)


# ══════════════════════════════════════════════
# 1. summarize_messages
# ══════════════════════════════════════════════
msgs = [
    {"role": "system", "content": "你是一个智能助手。" + "很长的基座提示词和【历史摘要】" * 100},
    {"role": "user", "content": "明天周五，我有课吗"},
    {"role": "assistant", "content": "x" * 60},
]
out = summarize_messages(msgs)

_check(out is not None and len(out) == 3, "返回逐条摘要，条数与入参一致")
_check([m["role"] for m in out] == ["system", "user", "assistant"], "role 全部保留")
_check(out[0]["content"].startswith("你是一个智能助手。"), "system 截断后仍露出开头角色定义")
_check("(+" in out[0]["content"] and len(out[0]["content"]) < len(msgs[0]["content"]),
       "system 超长被截断并标注省略量")
_check(out[1]["content"] == "明天周五，我有课吗", "短文本原样保留，不加标注")
_check(summarize_messages([]) is None, "空 messages 返回 None（不产生空 payload）")

# ══════════════════════════════════════════════
# 2. extract_output
# ══════════════════════════════════════════════
class _Msg:
    def __init__(self, content, reasoning=None):
        self.content = content
        if reasoning is not None:
            self.reasoning_content = reasoning


class _Usage:
    prompt_tokens, completion_tokens, total_tokens = 10, 5, 15


class _Resp:
    def __init__(self, msg):
        self.choices = [type("_C", (), {"message": msg})()]
        self.usage = _Usage()          # call_llm_with_cb 会读 resp.usage


content, reasoning = extract_output(_Resp(_Msg("明天没有课", "让我想想……" * 100)))
_check(content == "明天没有课", "正文原样取出")
_check(reasoning is not None and "(+" in reasoning, "reasoning_content 单独取出并截断")
_check(len(content) < len(reasoning), "reasoning 不占 output 字段（各自独立）")

c2, r2 = extract_output(_Resp(_Msg("只有正文")))
_check(c2 == "只有正文" and r2 is None, "无 reasoning_content 时返回 None（非空串）")

c3, r3 = extract_output(object())
_check(c3 is None and r3 is None, "回包结构异常时安全返回 (None, None)")

# ══════════════════════════════════════════════
# 3. record() 透传
# ══════════════════════════════════════════════
captured = []
_orig_gen = observability.obs_generation
observability.obs_generation = lambda **kw: captured.append(kw)


try:
    tk = token_tracker.get_tracker()
    tk._persist_record = lambda rec: None          # 不落盘
    tk.record("deepseek-flash", _Usage(), call_site="test.io",
              input=[{"role": "user", "content": "hi"}],
              output="ok", reasoning="think")
finally:
    observability.obs_generation = _orig_gen

_check(len(captured) == 1, "obs_generation 被调用一次")
kw = captured[0]
_check(kw["input"] == [{"role": "user", "content": "hi"}], "input 透传到 generation")
_check(kw["output"] == "ok", "output 透传到 generation")
_check(kw["metadata"]["reasoning_content"] == "think", "reasoning 进 metadata（不占 output）")
_check(kw["metadata"]["call_site"] == "test.io" and "cost_rmb" in kw["metadata"],
       "原有 metadata 字段未丢失")

# ══════════════════════════════════════════════
# 4 & 5. call_llm_with_cb 端到端（开关开 / 关）
# ══════════════════════════════════════════════
from mcp_unified_agent.circuit_breaker import call_llm_with_cb


class _FakeCompletions:
    def create(self, **kw):
        return _Resp(_Msg("明天没有课", "让我想想……" * 100))


class _FakeClient:
    class chat:
        completions = _FakeCompletions()


recorded = {}


class _FakeTracker:
    def record(self, model, usage, **kw):
        recorded.update({"model": model, **kw})


_orig_get_tracker = token_tracker.get_tracker
messages = [{"role": "user", "content": "明天有课吗"}]

# — 4. 开关开启 —
try:
    token_tracker.get_tracker = lambda: _FakeTracker()
    call_llm_with_cb(_FakeClient(), "deepseek-flash", messages, 0.3, 1000,
                     "decision_engine.decide")
finally:
    token_tracker.get_tracker = _orig_get_tracker

_check(recorded.get("input") == [{"role": "user", "content": "明天有课吗"}],
       "[开关开] 收口点把 messages 传给 record")
_check(recorded.get("output") == "明天没有课", "[开关开] 收口点把回包正文传给 record")
_check(recorded.get("reasoning") and "(+" in recorded["reasoning"],
       "[开关开] 收口点把 reasoning_content 传给 record 并截断")
_check(recorded.get("call_site") == "decision_engine.decide", "[开关开] call_site 不变")

# — 5. 开关关闭 —
recorded.clear()
_orig_flag = config.LANGFUSE_CAPTURE_IO
try:
    config.LANGFUSE_CAPTURE_IO = False
    token_tracker.get_tracker = lambda: _FakeTracker()
    call_llm_with_cb(_FakeClient(), "deepseek-flash", messages, 0.3, 1000,
                     "decision_engine.decide")
finally:
    token_tracker.get_tracker = _orig_get_tracker
    config.LANGFUSE_CAPTURE_IO = _orig_flag

_check("input" not in recorded and "output" not in recorded and "reasoning" not in recorded,
       "[开关关] 完全不采集（连截断副本都不产生）")
_check(recorded.get("model") == "deepseek-flash" and "latency_ms" in recorded,
       "[开关关] 用量记录本身不受影响（model/latency 照常）")

print("\n".join(f"✅ {p}" for p in PASS))
print(f"\n🎉 全部通过（{len(PASS)} 项断言，零 API 成本）")
