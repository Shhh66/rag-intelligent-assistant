"""会话摘要「压缩后校验」测试（对照「压缩丢关键信息」短板）

覆盖三类场景：
1. 正常输入：校验通过 → 沿用原摘要；校验不通过 → 带缺失项定向重压并采用重压结果
2. 边界输入：开关关闭 / ok=false 但无缺失项 / 校验结果无法解析 / 校验+重压双失败回退硬截断
3. 异常场景：校验调用抛错 / 重压抛错 / 重压返回空

用假 LLM 按 call_site 分派响应，确定性验证、零 API 成本。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
import mcp_unified_agent.unified_agent as ua
from mcp_unified_agent.unified_agent import UnifiedAgent

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


# ── 假 LLM（按 call_site 分派）─────────────────────────────────

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

DEFAULT_SUMMARY = "摘要A：用户在做 RAG 智能体项目"


def _fake_llm(**replies):
    """replies: call_site → 响应文本 / Exception。未指定的 call_site 回默认摘要。"""
    def _f(client, model, messages, temperature, max_tokens, call_site, **kw):
        _calls.append({"call_site": call_site, "max_tokens": max_tokens,
                       "prompt": messages[0]["content"]})
        r = replies.get(call_site)
        if isinstance(r, Exception):
            raise r
        return _Resp(DEFAULT_SUMMARY if r is None else r)
    return _f


def _new_agent():
    a = UnifiedAgent.__new__(UnifiedAgent)
    a._history = []
    a._session_summary = ""
    a._llm_client = None
    a.model = "test-model"
    a._reflection = None
    return a


def _fill(agent, rounds, prefix="q"):
    for i in range(rounds):
        agent._history.append({"role": "user", "content": f"{prefix}{i}"})
        agent._history.append({"role": "assistant", "content": f"{prefix}{i}-答"})


def _sites():
    return [c["call_site"] for c in _calls]


OK_JSON = '{"ok": true, "missing": []}'
BAD_JSON = '{"ok": false, "missing": ["用户要求全程只用中文回答"]}'


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_verify_pass():
    print("\n【正常 1】校验通过 → 沿用原摘要，不触发重压")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(**{"memory.verify": OK_JSON})
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("新问题", "新回答")

    check(_sites() == ["memory.compress", "memory.verify"],
          f"调用序列 压缩→校验（实际 {_sites()}）")
    check(a._session_summary == DEFAULT_SUMMARY, "摘要为压缩结果")
    check(len(a._history) == 10, f"窗口裁到 10 条（实际 {len(a._history)}）")


def test_verify_fail_triggers_retry():
    print("\n【正常 2】校验不通过 → 定向重压并采用重压结果")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(**{
        "memory.compress": "摘要A：用户在做 RAG 项目",
        "memory.verify": BAD_JSON,
        "memory.compress_retry": "摘要B：用户在做 RAG 项目，要求全程只用中文回答",
    })
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("新问题", "新回答")

    check(_sites() == ["memory.compress", "memory.verify", "memory.compress_retry"],
          f"调用序列 压缩→校验→重压（实际 {_sites()}）")
    check(a._session_summary.startswith("摘要B"), "采用重压结果")
    check("只用中文回答" in a._session_summary, "缺失的关键约束被补回")
    retry = [c for c in _calls if c["call_site"] == "memory.compress_retry"][0]
    check("只用中文回答" in retry["prompt"], "重压 Prompt 中显式喂入了缺失项")
    check(retry["max_tokens"] == 2000, "重压复用压缩的 max_tokens（推理模型需给足）")
    check(len(a._history) == 10, "窗口仍正常裁剪")


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_boundary_verify_disabled():
    print("\n【边界 1】校验开关关闭 → 只压缩，行为与改造前一致")
    _calls.clear()
    orig = config.SESSION_SUMMARY_VERIFY_ENABLED
    config.SESSION_SUMMARY_VERIFY_ENABLED = False
    try:
        ua.call_llm_with_cb = _fake_llm(**{"memory.verify": BAD_JSON})
        a = _new_agent()
        _fill(a, 10)
        a._record_conversation("q", "a")
        check(_sites() == ["memory.compress"], f"只调压缩（实际 {_sites()}）")
        check(a._session_summary == DEFAULT_SUMMARY, "摘要为压缩结果，未走校验")
    finally:
        config.SESSION_SUMMARY_VERIFY_ENABLED = orig


def test_boundary_fail_without_missing():
    print("\n【边界 2】ok=false 但缺失项为空 → 无据可依，不重压")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(**{"memory.verify": '{"ok": false, "missing": []}'})
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("q", "a")
    check(_sites() == ["memory.compress", "memory.verify"],
          f"未触发重压（实际 {_sites()}）")
    check(a._session_summary == DEFAULT_SUMMARY, "沿用原摘要")


def test_boundary_verify_unparsable():
    print("\n【边界 3】校验结果无法解析 / 非字典 → 跳过校验，沿用原摘要")
    for bad in ("这不是 JSON", "", "[1, 2, 3]", '{"ok": '):
        _calls.clear()
        ua.call_llm_with_cb = _fake_llm(**{"memory.verify": bad})
        a = _new_agent()
        _fill(a, 10)
        a._record_conversation("q", "a")
        ok = (_sites() == ["memory.compress", "memory.verify"]
              and a._session_summary == DEFAULT_SUMMARY)
        check(ok, f"校验返回 {bad[:14]!r} → 跳过校验不阻断")


def test_boundary_truncate_fallback():
    print("\n【边界 4】校验未过 + 重压失败 + 开关要求截断 → 回退硬截断")
    _calls.clear()
    orig = config.SESSION_SUMMARY_VERIFY_FALLBACK_TRUNCATE
    config.SESSION_SUMMARY_VERIFY_FALLBACK_TRUNCATE = True
    try:
        ua.call_llm_with_cb = _fake_llm(**{
            "memory.verify": BAD_JSON,
            "memory.compress_retry": "",
        })
        a = _new_agent()
        _fill(a, 10)
        a._record_conversation("q", "a")
        check(a._session_summary == "", "摘要未写入（回退硬截断）")
        check(len(a._history) == 20, f"硬截断保留 20 条（实际 {len(a._history)}）")
    finally:
        config.SESSION_SUMMARY_VERIFY_FALLBACK_TRUNCATE = orig


def test_boundary_keep_summary_by_default():
    print("\n【边界 5】重压失败但未要求截断（默认）→ 仍用原摘要，优于硬截断")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(**{
        "memory.verify": BAD_JSON,
        "memory.compress_retry": "",
    })
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("q", "a")
    check(a._session_summary == DEFAULT_SUMMARY, "沿用原摘要（未丢全部早期上下文）")
    check(len(a._history) == 10, f"窗口正常裁剪（实际 {len(a._history)}）")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_verify_raises():
    print("\n【异常 1】校验调用抛错 → 跳过校验，不阻断主流程")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(
        **{"memory.verify": RuntimeError("模拟校验故障")})
    a = _new_agent()
    _fill(a, 10)
    try:
        a._record_conversation("q", "a")
        ok = True
    except Exception as e:
        ok = False
        print(f"      抛出异常: {e}")
    check(ok, "主流程未抛异常")
    check(a._session_summary == DEFAULT_SUMMARY, "校验失败不作为压缩失败的依据")
    check(_sites() == ["memory.compress", "memory.verify"],
          f"未触发重压（实际 {_sites()}）")


def test_exception_retry_raises():
    print("\n【异常 2】重压调用抛错 → 沿用原摘要，不阻断")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(**{
        "memory.verify": BAD_JSON,
        "memory.compress_retry": RuntimeError("模拟重压故障"),
    })
    a = _new_agent()
    _fill(a, 10)
    try:
        a._record_conversation("q", "a")
        ok = True
    except Exception as e:
        ok = False
        print(f"      抛出异常: {e}")
    check(ok, "主流程未抛异常")
    check(a._session_summary == DEFAULT_SUMMARY, "沿用原摘要")
    check(len(a._history) == 10, "窗口正常裁剪")


def test_exception_compress_still_fails():
    print("\n【异常 3】压缩本身失败 → 不进入校验，直接回退硬截断（回归原行为）")
    _calls.clear()
    ua.call_llm_with_cb = _fake_llm(
        **{"memory.compress": RuntimeError("模拟压缩故障")})
    a = _new_agent()
    _fill(a, 10)
    a._record_conversation("q", "a")
    check(_sites() == ["memory.compress"], f"只调了压缩（实际 {_sites()}）")
    check(a._session_summary == "", "摘要未写入")
    check(len(a._history) == 20, f"回退硬截断（实际 {len(a._history)}）")


def main():
    print("🧪 会话摘要「压缩后校验」测试")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_verify_pass()
    test_verify_fail_triggers_retry()

    print("\n─── 二、边界输入 ───")
    test_boundary_verify_disabled()
    test_boundary_fail_without_missing()
    test_boundary_verify_unparsable()
    test_boundary_truncate_fallback()
    test_boundary_keep_summary_by_default()

    print("\n─── 三、异常场景 ───")
    test_exception_verify_raises()
    test_exception_retry_raises()
    test_exception_compress_still_fails()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
