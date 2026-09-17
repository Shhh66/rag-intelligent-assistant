"""审计 user_id 字段验证

背景：`user_id` 原是为「短→长沉淀」按用户聚合而引入的前置改动；沉淀功能因
**收益不成立**已整体移除（详见 技术文档/长期记忆.md 第十节），但该字段作为
**审计归属**独立保留——没有它，审计日志只有 trace_id，追溯不到「谁发起的调用」。

覆盖三类场景：
1. 正常输入：工具调用 / 决策记录都带 user_id
2. 边界输入：不传 user_id（默认空串，结构一致）/ 旧调用点不传不破坏 / 审计关闭
3. 异常场景：写入失败静默不抛出
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
import tool_audit

_TMP = tempfile.mkdtemp()
AUDIT = os.path.join(_TMP, "audit.jsonl")
config.TOOL_AUDIT_PATH = AUDIT
config.TOOL_AUDIT_ENABLED = True

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


def _reset():
    if os.path.exists(AUDIT):
        os.remove(AUDIT)


def _recs():
    if not os.path.exists(AUDIT):
        return []
    return [json.loads(l) for l in open(AUDIT, encoding="utf-8") if l.strip()]


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_tool_call_user_id():
    print("\n【正常 1】工具调用记录带 user_id")
    _reset()
    tool_audit.log_tool_call("t1", "query_weather", {"city": "北京"}, "晴", 10.0,
                             True, user_id="alice")
    r = _recs()[0]
    check(r.get("user_id") == "alice", f"user_id=alice（实际 {r.get('user_id')!r}）")
    check(r.get("tool_name") == "query_weather" and r.get("success") is True,
          "其余字段不受影响")


def test_decision_user_id():
    print("\n【正常 2】决策记录带 user_id")
    _reset()
    tool_audit.log_decision("t1", 0, "call_tools", thought="t", user_id="bob")
    r = _recs()[0]
    check(r.get("user_id") == "bob", f"user_id=bob（实际 {r.get('user_id')!r}）")
    check(r.get("type") == "decision", "type=decision 未受影响")


def test_same_trace_attribution():
    print("\n【正常 3】同一 trace 的多条记录可归到同一用户")
    _reset()
    tool_audit.log_decision("trace-x", 0, "call_tools", user_id="alice")
    tool_audit.log_tool_call("trace-x", "query_weather", {}, "晴", 5.0, True,
                             user_id="alice")
    tool_audit.log_tool_call("trace-x", "search_knowledge_base", {}, "片段", 8.0,
                             True, user_id="alice")
    recs = _recs()
    same = [r for r in recs if r.get("trace_id") == "trace-x"]
    check(len(same) == 3, f"同 trace 3 条（实际 {len(same)}）")
    check(all(r.get("user_id") == "alice" for r in same),
          "三条都能归属到 alice（trace_id + user_id 双维度可查）")


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_boundary_default_empty():
    print("\n【边界 1】不传 user_id → 字段存在但为空串（schema 一致）")
    _reset()
    tool_audit.log_tool_call("t1", "query_weather", {}, "x", 1.0, True)
    r = _recs()[0]
    check("user_id" in r, "字段存在（下游可按统一 schema 解析，无需判空键）")
    check(r["user_id"] == "", f"值为空串（实际 {r['user_id']!r}）")


def test_boundary_legacy_compat():
    print("\n【边界 2】旧调用点（位置参数 + 关键字）不破坏")
    _reset()
    # 模拟既有调用风格：只传原有参数
    tool_audit.log_tool_call("t1", "ask_knowledge_base", {"query": "x"},
                             "结果", 20.0, False, retry_count=2, error="超时")
    r = _recs()[0]
    check(r["retry_count"] == 2 and r["error"] == "超时",
          "原有参数（retry_count/error）语义不变")


def test_boundary_audit_disabled():
    print("\n【边界 3】审计未启用 → 不写任何记录")
    _reset()
    saved = config.TOOL_AUDIT_ENABLED
    config.TOOL_AUDIT_ENABLED = False
    try:
        tool_audit.log_tool_call("t1", "query_weather", {}, "x", 1.0, True,
                                 user_id="alice")
        check(_recs() == [], "零写入")
    finally:
        config.TOOL_AUDIT_ENABLED = saved


def test_boundary_sanitize_unaffected():
    print("\n【边界 4】入参脱敏不受 user_id 改动影响")
    _reset()
    tool_audit.log_tool_call("t1", "query_weather",
                             {"city": "北京", "api_key": "sk-secret"},
                             "晴", 1.0, True, user_id="alice")
    r = _recs()[0]
    check(r["arguments"].get("api_key") == "***", "敏感键仍被掩码")
    check(r["arguments"].get("city") == "北京", "普通参数原样保留")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_write_failure():
    print("\n【异常 1】路径不可写 → 静默不抛出（不阻断主链路）")
    saved = config.TOOL_AUDIT_PATH
    config.TOOL_AUDIT_PATH = os.path.join(_TMP, "no_such_dir", "deep", "a.jsonl")
    try:
        try:
            tool_audit.log_tool_call("t1", "query_weather", {}, "x", 1.0, True,
                                     user_id="alice")
            tool_audit.log_decision("t1", 0, "call_tools", user_id="alice")
            ok = True
        except Exception as e:
            ok = False
            print(f"      抛出异常: {e}")
        check(ok, "写入失败静默吞掉")
    finally:
        config.TOOL_AUDIT_PATH = saved


def test_exception_weird_user_id():
    print("\n【异常 2】user_id 为 None / 非字符串 → 不抛错")
    _reset()
    try:
        tool_audit.log_tool_call("t1", "query_weather", {}, "x", 1.0, True,
                                 user_id=None)
        tool_audit.log_decision("t1", 0, "call_tools", user_id=None)
        ok = True
    except Exception as e:
        ok = False
        print(f"      抛出异常: {e}")
    check(ok, "None 安全处理")
    if _recs():
        check(_recs()[0].get("user_id") == "", "None 归一化为空串")


def main():
    print("🧪 审计 user_id 字段验证")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_tool_call_user_id()
    test_decision_user_id()
    test_same_trace_attribution()

    print("\n─── 二、边界输入 ───")
    test_boundary_default_empty()
    test_boundary_legacy_compat()
    test_boundary_audit_disabled()
    test_boundary_sanitize_unaffected()

    print("\n─── 三、异常场景 ───")
    test_exception_write_failure()
    test_exception_weird_user_id()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
