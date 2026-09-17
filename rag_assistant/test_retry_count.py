"""验证工具调用的真实重试次数被记录（假 MCP session，零 API 成本、确定性）。

背景：ReAct 主路径此前恒记 retry_count=0 —— call_tool 内部的退避重试次数
只在日志里用过，没回传给调用方。本脚本验证修复后：

  1. 一次成功              → attempts=1（重试 0 次）
  2. 重试 2 次后成功       → attempts=3（重试 2 次）
  3. 重试耗尽仍失败        → meta 仍被写入，attempts=上限
  4. 熔断拒绝              → attempts=0（未发起调用）
  5. 不传 meta             → 行为与改造前一致（零破坏）
  6. _execute_one 端到端   → 真实重试次数确实落进工具审计

运行：python test_retry_count.py
"""

import asyncio
import os
import sys
import time
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding="utf-8")

import config
import tool_audit
from mcp_unified_agent import mcp_client_manager as mcm
from mcp_unified_agent.circuit_breaker import CircuitBreakerError, get_breaker
from mcp_unified_agent.decision_engine import ToolDecision
from mcp_unified_agent.scheduler import Scheduler

PASS = []


def _check(cond, label):
    assert cond, f"❌ 失败: {label}"
    PASS.append(label)


# 加速：退避基数设 0，避免测试真等待
_SAVED_CFG = (config.TOOL_RETRY_ENABLED, config.TOOL_RETRY_MAX,
              config.TOOL_RETRY_BACKOFF_BASE)
config.TOOL_RETRY_ENABLED = True
config.TOOL_RETRY_MAX = 3
config.TOOL_RETRY_BACKOFF_BASE = 0


def _reset_breaker():
    """熔断器是全局单例，逐条用例前复位，避免跨用例污染。"""
    b = get_breaker()
    b.state = "CLOSED"
    b.failure_count = 0
    b.half_open_probes = 0


class _FakeResult:
    def __init__(self):
        self.isError = False
        self.content = [types.SimpleNamespace(text="晴 25℃")]


class _FakeMCPSession:
    """前 fail_times 次抛超时（触发重试），之后成功。"""

    def __init__(self, fail_times=0):
        self.calls = 0
        self.fail_times = fail_times

    async def call_tool(self, name, args):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise asyncio.TimeoutError()
        return _FakeResult()


def _make(fail_times=0):
    return mcm.MCPSession(session=_FakeMCPSession(fail_times), call_timeout=5.0)


async def _call(fail_times, with_meta=True):
    m = _make(fail_times)
    meta = {}
    err = None
    try:
        if with_meta:
            await m.call_tool("query_weather", {"city": "北京"}, meta=meta)
        else:
            await m.call_tool("query_weather", {"city": "北京"})
    except Exception as e:      # noqa: BLE001 —— 测试需覆盖各类异常
        err = e
    return meta, err


# ── 1~3. call_tool 回传真实尝试次数 ──
_reset_breaker()
meta, err = asyncio.run(_call(0))
_check(err is None and meta.get("attempts") == 1, "一次成功 → attempts=1（未重试）")

_reset_breaker()
meta, err = asyncio.run(_call(2))
_check(err is None and meta.get("attempts") == 3, "重试 2 次后成功 → attempts=3")

_reset_breaker()
meta, err = asyncio.run(_call(99))
_check(isinstance(err, mcm.ToolCallTimeoutError),
       "重试耗尽 → 抛出超时异常（行为不变）")
_check(meta.get("attempts") == 3, "重试耗尽仍失败时 meta 仍被写入（attempts=上限 3）")

# ── 4. 熔断拒绝：根本没发起调用 ──
_reset_breaker()
_b = get_breaker()
_b.state = "OPEN"
_b.last_failure_time = time.time()      # 冷却未结束
meta, err = asyncio.run(_call(0))
_check(isinstance(err, CircuitBreakerError), "熔断打开时抛熔断异常（行为不变）")
_check(meta.get("attempts") == 0, "熔断拒绝 → attempts=0（未发起任何调用）")

# ── 5. 不传 meta：零破坏 ──
_reset_breaker()
meta, err = asyncio.run(_call(0, with_meta=False))
_check(err is None, "不传 meta 时调用照常成功（零破坏）")

# ── 6. _execute_one 端到端：真实重试次数落进审计 ──
_reset_breaker()
captured = []
_orig_log = tool_audit.log_tool_call
tool_audit.log_tool_call = lambda **kw: captured.append(kw)


class _FakeRegistry:
    def get(self, name):
        return object()

    def validate(self, name, args):
        return True, ""


s = Scheduler.__new__(Scheduler)      # 绕过 __init__（只需本次路径用到的属性）
s.trace_id = "t-e2e"
s.user_id = "u1"
s.kb_groups = None
s.permissions = None
s.tool_perm_enabled = False           # 跳过权限校验，聚焦重试计数
s.tool_perm_strict = False
s.default_timeout = 5.0
s.mcp_client = _make(2)               # 前 2 次超时 → 第 3 次成功
s.registry = _FakeRegistry()

try:
    out = asyncio.run(s._execute_one(
        ToolDecision(tool_name="query_weather", arguments={"city": "北京"})
    ))
finally:
    tool_audit.log_tool_call = _orig_log

_check(out["is_error"] is False, "端到端：工具最终调用成功")
_check(bool(captured), "端到端：审计被写出")
_check(captured[-1].get("retry_count") == 2,
       f"端到端：审计记录真实重试次数 2（实际 {captured[-1].get('retry_count')!r}）")
_check(captured[-1].get("success") is True, "端到端：审计同时记录成功状态")

# 无重试时记 0（而非漏字段）
_reset_breaker()
captured.clear()
s.mcp_client = _make(0)
tool_audit.log_tool_call = lambda **kw: captured.append(kw)
try:
    asyncio.run(s._execute_one(
        ToolDecision(tool_name="query_weather", arguments={"city": "北京"})
    ))
finally:
    tool_audit.log_tool_call = _orig_log
_check(captured[-1].get("retry_count") == 0, "一次成功时审计记 retry_count=0")

# ── 7. Skill 路径：内层退避重试也要计入 ──
_reset_breaker()
from mcp_unified_agent.skill_executor import SkillExecutor

captured.clear()
ex = SkillExecutor(
    mcp_session=_make(2),      # call_tool 内部重试 2 次 → attempts=3
    step_timeout=5.0,
    trace_id="t-skill",
    registry=None,             # None → 跳过工具级权限校验
    permissions=None,
    user_id="u1",
)
tool_audit.log_tool_call = lambda **kw: captured.append(kw)
try:
    out = asyncio.run(ex._execute_step(
        {"tool": "query_weather", "args": {"city": "北京"},
         "retryable": True, "critical": True},
        {}, 0,
    ))
finally:
    tool_audit.log_tool_call = _orig_log

_check(out["is_error"] is False, "Skill 路径：工具调用成功")
_check(bool(captured), "Skill 路径：审计被写出")
_check(captured[-1].get("retry_count") == 2,
       f"Skill 路径：内层退避重试被计入（期望 2，实际 {captured[-1].get('retry_count')!r}）")

# 8. Skill 失败路径：_error_result 记录传入的重试次数（此前硬编码 0）
captured.clear()
tool_audit.log_tool_call = lambda **kw: captured.append(kw)
try:
    ex._error_result("query_weather", {}, True, "boom", retry_count=3)
finally:
    tool_audit.log_tool_call = _orig_log
_check(captured[-1].get("retry_count") == 3,
       "Skill 失败路径：_error_result 如实记录重试次数（此前恒 0）")
_check(captured[-1].get("success") is False, "Skill 失败路径：成功标志为 False")

# 恢复配置
(config.TOOL_RETRY_ENABLED, config.TOOL_RETRY_MAX,
 config.TOOL_RETRY_BACKOFF_BASE) = _SAVED_CFG
_reset_breaker()

print("\n".join(f"✅ {p}" for p in PASS))
print(f"\n🎉 全部通过（{len(PASS)} 项断言，零 API 成本）")
