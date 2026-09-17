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

# ══════════════════════════════════════════════
# 6. P1：trace 顶层入参/出参 + 工具 span 的 output
# ══════════════════════════════════════════════
class _FakeSpan:
    def end(self):
        pass


class _FakeTrace:
    def __init__(self, sink):
        self._sink = sink

    def update(self, **kw):
        self._sink["updates"].append(kw)
        return self

    def span(self, **kw):
        self._sink["spans"].append(kw)
        return _FakeSpan()


class _FakeLFClient:
    def __init__(self):
        self.sink = {"updates": [], "spans": []}

    def trace(self, **kw):
        return _FakeTrace(self.sink)


_orig_client = observability._get_client
_orig_flag = config.LANGFUSE_CAPTURE_IO

fake = _FakeLFClient()
observability._get_client = lambda: fake
try:
    observability.update_trace_io("a" * 32, input="明天周五，我有课吗", output="x" * 2500)
    observability.update_trace_io("b" * 32, input="", output=None)
    with observability.obs_span("工具:demo", trace_id="a" * 32,
                                input={"city": "北京"},
                                output=observability.truncate_io("y" * 800)):
        pass
finally:
    observability._get_client = _orig_client

updates = fake.sink["updates"]
_check(updates[0]["input"] == "明天周五，我有课吗", "trace 顶层写入 input=用户问题")
_check(updates[0]["output"].startswith("x" * 100) and "(+" in updates[0]["output"],
       "trace 顶层 output 超长被截断并标注")
_check(len(updates[0]["output"]) < 2100, "截断后长度受控（2500 字未全量上报）")
_check(updates[1]["input"] is None and updates[1]["output"] is None,
       "空入参/出参写 None，不产生空串噪音")

spans = fake.sink["spans"]
_check(spans[0]["input"] == {"city": "北京"} and spans[0]["output"].endswith(")"),
       "工具 span 同时带上入参与出参，output 已截断")

_check(observability.truncate_io("短文本") == "短文本", "truncate_io 短文本原样返回")
_check(observability.truncate_io(None) is None and observability.truncate_io("") is None,
       "truncate_io 空值返回 None")

fake2 = _FakeLFClient()
observability._get_client = lambda: fake2
try:
    config.LANGFUSE_CAPTURE_IO = False
    observability.update_trace_io("c" * 32, input="q", output="a")
finally:
    observability._get_client = _orig_client
    config.LANGFUSE_CAPTURE_IO = _orig_flag
_check(not fake2.sink["updates"], "[开关关] trace 顶层入参/出参同样不写")

# ══════════════════════════════════════════════
# 7. P2：span 嵌套（ReAct 轮次分组）+ memory 耗时
# ══════════════════════════════════════════════
class _SpanNode:
    """假 span：既作为节点被记录，也能创建子节点，用于断言父子关系。"""

    def __init__(self, name):
        self.name = name
        self.children = []

    def span(self, **kw):
        child = _SpanNode(kw.get("name"))
        self.children.append(child)
        return child

    def generation(self, **kw):
        child = _SpanNode(f"<gen>{kw.get('name')}</gen>")
        self.children.append(child)
        return child

    def end(self):
        pass

    def update(self, **kw):
        pass


class _TraceNode(_SpanNode):
    def __init__(self):
        super().__init__("<trace>")


class _NestClient:
    def __init__(self):
        self.root = _TraceNode()

    def trace(self, **kw):
        return self.root


nest = _NestClient()
observability._get_client = lambda: nest
try:
    with observability.obs_span("ReAct 第 1 轮", trace_id="a" * 32):
        with observability.obs_span("工具:demo", trace_id="a" * 32):
            pass
        observability.obs_generation(trace_id="a" * 32, name="decide", model="m")
    # 退出轮次作用域后再上报 → 应回到 trace 根，而非留在轮次里
    observability.obs_generation(trace_id="a" * 32, name="orphan", model="m")
    left_in_scope = observability._current_span.get()
finally:
    observability._get_client = _orig_client

_root_names = [c.name for c in nest.root.children]
_check(_root_names == ["ReAct 第 1 轮", "<gen>orphan</gen>"],
       "trace 根下只有轮次 span 与轮次外节点，顺序正确")
_round1 = nest.root.children[0]
_child_names = [c.name for c in _round1.children]
_check("工具:demo" in _child_names, "轮次内的工具 span 自动成为子节点")
_check("<gen>decide</gen>" in _child_names, "轮次内的 LLM generation 自动成为子节点")
_check("<gen>orphan</gen>" not in _child_names, "退出轮次后的节点未误挂进上一轮")
_check(left_in_scope is None, "退出后 _current_span 复位为 None（不泄漏到下一次请求）")

# 嵌套与 LangFuse 不可用时的降级互不干扰
observability._get_client = lambda: None
try:
    with observability.obs_span("no-op 轮次", trace_id="a" * 32) as _noop:
        observability.obs_generation(trace_id="a" * 32, name="x", model="m")
    _noop_after = observability._current_span.get()
finally:
    observability._get_client = _orig_client
_check(_noop is None and _noop_after is None, "LangFuse 不可用时嵌套路径仍是纯 no-op")

# memory 三处记账均补上耗时（源码级防回归）
import inspect as _inspect
import long_term_memory as _ltm
_src = _inspect.getsource(_ltm)
_check(_src.count("latency_ms=(time.monotonic() - _t0) * 1000") == 3,
       "memory 的 extract / extract_summary / summarize 三处均补上 latency_ms")

print("\n".join(f"✅ {p}" for p in PASS))
print(f"\n🎉 全部通过（{len(PASS)} 项断言，零 API 成本）")
