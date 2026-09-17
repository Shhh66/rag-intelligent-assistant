"""工具鉴权严格模式验证

背景：`permissions=None` 原表示「不限权限」，但调用方**忘传身份**也是 None——
两种语义混在一起，等于「忘传 = 全放行」。`TOOL_PERMISSION_STRICT=True` 时
把 None 按「空权限集」处理，受控工具全部拒绝（fail-closed）。

同时验证两条路径**读同一套开关**：原先 `skill_executor` 只判 `permissions is not None`，
不读 `TOOL_PERMISSION_ENABLED`——关掉总开关后 Skill 路径仍在鉴权（与承诺不符）。

覆盖场景：
1. 正常：有权限 → 放行
2. 边界：strict=False + None → 跳过（零破坏，行为与改造前一致）
3. 边界：strict=True + None → 校验，受控工具拒绝 / 公开工具放行
4. 边界：总开关关 → ReAct + Skill 两路径都跳过
5. 对称性：scheduler 与 skill_executor 行为一致（过去正是在此处漏收一条路径）
"""

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

logging.disable(logging.CRITICAL)

import config
from mcp_unified_agent.scheduler import Scheduler
from mcp_unified_agent.skill_executor import SkillExecutor
from mcp_unified_agent.tool_registry import ToolRegistry, ToolMeta

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


def _registry():
    r = ToolRegistry()
    r.load([
        ToolMeta(name="query_weather", description="", input_schema={},
                 required_perms=["*"]),
        ToolMeta(name="clear_memory", description="", input_schema={},
                 required_perms=["manage_users"]),
    ])
    return r


def _scheduler(permissions):
    return Scheduler(None, _registry(), permissions=permissions)


def _executor(permissions):
    return SkillExecutor(None, registry=_registry(), permissions=permissions)


def main():
    print("\n[1] 正常路径：已传身份且有权限 → 放行")
    config.TOOL_PERMISSION_ENABLED = True
    config.TOOL_PERMISSION_STRICT = True
    s = _scheduler(["manage_users"])
    check(s._perm_check_active() is True, "permissions 非 None 时执行校验")
    check(s._check_tool_permission("clear_memory") is None, "持有 manage_users → 放行")
    check(_scheduler(["search_all"])._check_tool_permission("clear_memory") == "manage_users",
          "不持有 → 返回缺失权限名")

    print("\n[2] 边界：strict=False + permissions=None → 跳过（零破坏）")
    config.TOOL_PERMISSION_STRICT = False
    check(_scheduler(None)._perm_check_active() is False,
          "ReAct 路径跳过校验（直连调试/未登录行为不变）")
    check(_executor(None)._perm_check_active() is False, "Skill 路径同样跳过")

    print("\n[3] 边界：strict=True + permissions=None → 按空权限集校验")
    config.TOOL_PERMISSION_STRICT = True
    s_none = _scheduler(None)
    check(s_none._perm_check_active() is True, "ReAct 路径执行校验（fail-closed）")
    check(s_none._check_tool_permission("clear_memory") == "manage_users",
          "None 视作空权限集 → 受控工具被拒")
    check(s_none._check_tool_permission("query_weather") is None,
          "公开工具（['*']）不受影响 → 放行")

    print("\n[4] 边界：总开关关闭 → 两条路径都不校验")
    config.TOOL_PERMISSION_ENABLED = False
    check(_scheduler(None)._perm_check_active() is False, "ReAct 路径跳过")
    check(_executor(None)._perm_check_active() is False,
          "Skill 路径跳过（修复原先不读总开关的不一致）")
    check(_scheduler(["search_all"])._perm_check_active() is False,
          "已传身份也不校验（总开关优先）")

    print("\n[5] 对称性：两路径在四种组合下结论一致")
    combos = [(True, True, None), (True, False, None), (True, True, []), (False, True, None)]
    same = True
    for enabled, strict, perms in combos:
        config.TOOL_PERMISSION_ENABLED = enabled
        config.TOOL_PERMISSION_STRICT = strict
        a = _scheduler(perms)._perm_check_active()
        b = _executor(perms)._perm_check_active()
        if a != b:
            same = False
            print(f"   ⚠️ 不一致 enabled={enabled} strict={strict} perms={perms}: "
                  f"scheduler={a} skill={b}")
    check(same, "四种配置组合下 scheduler 与 skill_executor 结论完全一致")

    print(f"\n{'='*50}\n通过 {_passed} / 失败 {_failed}\n{'='*50}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
