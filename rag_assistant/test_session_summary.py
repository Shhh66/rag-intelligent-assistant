"""会话摘要压缩测试脚本（模块 9.3）

覆盖三类场景：
1. 正常输入：历史超阈值 → 压缩成摘要、裁剪窗口、滚动合并旧摘要
2. 边界输入：恰好等于阈值 / 仅超一条 / KEEP_RECENT 边界 / 摘要为空白
3. 异常场景：LLM 抛错 / 返回空串 / 开关关闭 → 回退硬截断，不抛异常

用假 LLM 替换 call_llm_with_cb，确定性验证、零 API 成本。
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import mcp_unified_agent.unified_agent as ua
from mcp_unified_agent.unified_agent import UnifiedAgent
from mcp_unified_agent.decision_engine import DecisionEngine

ua.HAS_LONG_MEMORY = False  # 隔离长期记忆，只测会话摘要

BASE_SYS = "你是一个使用 ReAct 模式推理的智能助手决策引擎。"
FAKE_SUMMARY = "用户在做 RAG 项目，要求全程用中文回答，正在调研混合检索。"

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"   ✅ {msg}")
    else:
        _failed += 1
        print(f"   ❌ {msg}")


# ── 假 LLM ────────────────────────────────────────────────────

class _Msg:
    def __init__(self, c):
        self.content = c


class _Choice:
    def __init__(self, c):
        self.message = _Msg(c)


class _Resp:
    def __init__(self, c):
        self.choices = [_Choice(c)]


_calls = []


def _fake_llm(content=FAKE_SUMMARY, exc=None):
    def _f(client, model, messages, temperature, max_tokens, call_site, **kw):
        _calls.append({
            "call_site": call_site,
            "max_tokens": max_tokens,
            "prompt": messages[0]["content"],
            "system": None,
        })
        if exc:
            raise exc
        return _Resp(content)
    return _f


def _new_agent():
    """绕过 __init__（避免真实 LLM/MCP 初始化），只装配测试所需属性。"""
    a = UnifiedAgent.__new__(UnifiedAgent)
    a._history = []
    a._session_summary = ""
    a._llm_client = None
    a.model = "test-model"
    a._reflection = None
    return a


def _fill(agent, rounds, prefix="q"):
    """填充 rounds 轮对话（每轮 2 条消息）。"""
    for i in range(rounds):
        agent._history.append({"role": "user", "content": f"{prefix}{i}"})
        agent._history.append({"role": "assistant", "content": f"{prefix}{i}-答"})


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_normal_compress():
    print("\n【正常 1】历史超阈值 → 压缩成摘要并裁剪窗口")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm()
    a = _new_agent()
    _fill(a, 10)                       # 20 条 = 阈值
    a._record_conversation("新问题", "新回答")   # → 22 条，触发压缩

    check(a._session_summary == FAKE_SUMMARY, f"摘要已写入（{a._session_summary[:20]}…）")
    check(len(a._history) == 10, f"历史裁剪到 KEEP_RECENT=10（实际 {len(a._history)}）")
    check(bool(_calls) and _calls[-1]["call_site"] == "memory.compress",
          "调用点标记为 memory.compress（可被 token 记账归因）")
    check(_calls[-1]["max_tokens"] == 2000, "max_tokens=2000（推理模型需给足）")
    check("q0" not in str(a._history), "最早期对话已从窗口移除（其信息进入摘要）")


def test_rolling_compress():
    print("\n【正常 2】滚动压缩 → 旧摘要参与下一轮压缩")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm("第一版摘要：用户在做 RAG 项目")
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("q", "a")
    first = a._session_summary

    ua.call_llm_with_cb = _fake_llm("第二版摘要：用户在做 RAG 项目，且要求中文回答")
    _fill(a, 10, prefix="r")
    a._record_conversation("q2", "a2")

    check(first in _calls[-1]["prompt"], "第二次压缩的 Prompt 含第一次摘要（滚动合并）")
    check(a._session_summary.startswith("第二版摘要"), "摘要已更新为新版本")
    check(len(a._history) == 10, "窗口再次回落到 10 条（长度恒定，不膨胀）")


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_boundary_exact_threshold():
    print("\n【边界 1】恰好等于阈值（20 条）→ 不触发压缩")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm()
    a = _new_agent()
    _fill(a, 9)                        # 18 条
    a._record_conversation("q", "a")   # → 20 条，不大于阈值
    check(len(a._history) == 20, f"保持 20 条不裁剪（实际 {len(a._history)}）")
    check(a._session_summary == "", "未触发压缩，摘要仍为空")
    check(not _calls, "未调用 LLM（无多余成本）")


def test_boundary_one_over():
    print("\n【边界 2】仅超出一条（22 条）→ 触发压缩")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm()
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("q", "a")   # → 22 条
    check(a._session_summary != "", "触发压缩，摘要已写入")
    check(len(_calls) == 1, f"恰好调用 1 次 LLM（实际 {len(_calls)}）")


def test_boundary_keep_recent_config():
    print("\n【边界 3】KEEP_RECENT 可配置（改为 4）")
    _calls.clear()
    import config
    orig = config.SESSION_SUMMARY_KEEP_RECENT
    config.SESSION_SUMMARY_KEEP_RECENT = 4
    try:
        ua.call_llm_with_cb = _fake_llm()
        a = _new_agent()
        _fill(a, 10)
        a._record_conversation("q", "a")
        check(len(a._history) == 4, f"按配置保留 4 条（实际 {len(a._history)}）")
    finally:
        config.SESSION_SUMMARY_KEEP_RECENT = orig


def test_boundary_no_compressible():
    print("\n【边界 4】历史不足 KEEP_RECENT → 无可压缩部分，直接返回 False")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm()
    a = _new_agent()
    _fill(a, 3)                        # 6 条 < KEEP_RECENT(10)
    ok = a._compress_history()
    check(ok is False, "返回 False（无可压缩内容）")
    check(not _calls, "未调用 LLM")
    check(len(a._history) == 6, "历史未被改动")


def test_boundary_blank_summary():
    print("\n【边界 5】摘要为纯空白 → system 只保留基础提示词")
    de = DecisionEngine.__new__(DecisionEngine)
    de.session_summary = "   \n  "
    check(de._build_system_content() == BASE_SYS, "空白摘要被 strip，system 不变")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_llm_error():
    print("\n【异常 1】LLM 抛异常 → 回退硬截断，不抛出")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(exc=RuntimeError("模拟 LLM 故障"))
    a = _new_agent()
    _fill(a, 10)
    try:
        a._record_conversation("q", "a")   # 不应抛异常
        ok = True
    except Exception as e:
        ok = False
        print(f"      抛出异常: {e}")
    check(ok, "主流程未抛异常（降级安全）")
    check(a._session_summary == "", "摘要未被写入（失败不留脏数据）")
    check(len(a._history) == 20, f"回退硬截断保留 20 条（实际 {len(a._history)}）")


def test_exception_empty_response():
    print("\n【异常 2】LLM 返回空串 → 回退硬截断")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm("")
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("q", "a")
    check(a._session_summary == "", "空响应不写入摘要")
    check(len(a._history) == 20, f"回退硬截断保留 20 条（实际 {len(a._history)}）")


def test_exception_disabled():
    print("\n【异常 3】开关关闭 → 完全走原硬截断，且不调 LLM")
    _calls.clear()
    import config
    orig = config.SESSION_SUMMARY_ENABLED
    config.SESSION_SUMMARY_ENABLED = False
    try:
        ua.call_llm_with_cb = _fake_llm()
        a = _new_agent()
        _fill(a, 10)
        a._record_conversation("q", "a")
        check(a._session_summary == "", "未生成摘要")
        check(len(a._history) == 20, f"硬截断保留 20 条（实际 {len(a._history)}）")
        check(not _calls, "LLM 零调用（行为与改造前一致）")
    finally:
        config.SESSION_SUMMARY_ENABLED = orig


def test_exception_config_missing():
    print("\n【异常 4】配置项缺失 → 安全降级不崩溃")
    _calls.clear()
    import config
    saved = config.SESSION_SUMMARY_ENABLED
    del config.SESSION_SUMMARY_ENABLED
    try:
        ua.call_llm_with_cb = _fake_llm()
        a = _new_agent()
        _fill(a, 10)
        a._record_conversation("q", "a")
        check(a._session_summary != "", "缺配置时按默认 True 正常压缩")
    finally:
        config.SESSION_SUMMARY_ENABLED = saved


# ══════════════════════════════════════════════════════════════
# 四、system 注入（单条 system，两块结构）
# ══════════════════════════════════════════════════════════════

def test_system_injection():
    print("\n【注入】单条 system、两块结构")
    de = DecisionEngine.__new__(DecisionEngine)
    de.session_summary = ""
    check(de._build_system_content() == BASE_SYS, "无摘要 → 仅基础提示词（行为不变）")

    de.session_summary = "用户在做一个 RAG 项目"
    c = de._build_system_content()
    check(c.startswith(BASE_SYS), "第一块：基础系统提示词在最前")
    check("【历史摘要】：用户在做一个 RAG 项目" in c, "第二块：【历史摘要】段落已拼接")
    check(c.count("【历史摘要】") == 1, "只有一段摘要（未新增多条 system）")


def test_clear_memory():
    print("\n【注入】clear_memory 同时清空摘要")
    a = _new_agent()
    a._session_summary = "旧摘要"
    a._history = [{"role": "user", "content": "q"}]
    a.clear_memory()
    check(a._session_summary == "", "摘要已清空")
    check(a._history == [], "历史已清空")


def main():
    print("🧪 会话摘要压缩测试（模块 9.3）")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_normal_compress()
    test_rolling_compress()

    print("\n─── 二、边界输入 ───")
    test_boundary_exact_threshold()
    test_boundary_one_over()
    test_boundary_keep_recent_config()
    test_boundary_no_compressible()
    test_boundary_blank_summary()

    print("\n─── 三、异常场景 ───")
    test_exception_llm_error()
    test_exception_empty_response()
    test_exception_disabled()
    test_exception_config_missing()

    print("\n─── 四、system 注入 ───")
    test_system_injection()
    test_clear_memory()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
