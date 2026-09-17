"""工具失败后的「重新规划 or 降级」引导验证

背景：原护栏只判断「本轮有没有执行过工具」，命中就给一句
"不要再调用工具，请用 direct_answer 汇总"——**把工具失败也推向了直接回答**，
等于掐断了失败后换个工具重规划的路径。本测试验证改为按成功/失败分别引导。

覆盖三类场景：
1. 正常输入：全成功 / 全失败 / 部分成功 → 三种不同的引导文案
2. 边界输入：Skill 失败识别 / 结果文本含"失败"字样不误判 / 无已执行提示 / hints 为空
3. 异常场景：hints 含畸形条目不抛错；首轮与后续轮模板都生效

纯字符串构造，零 API 成本。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from mcp_unified_agent.prompt_templates import (
    build_decision_prompt, _EXECUTED_STATUS_RE,
)

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


def prompt(hints, turn=1):
    return build_decision_prompt(
        user_input="帮我查一下",
        history=[],
        tools_description="- query_weather: 查天气",
        reflection_hints=hints,
        turn=turn,
    )


# 与 unified_agent 生成格式一致
def ok_hint(name, text="结果内容"):
    return f"[本轮已执行] 工具「{name}」→ 成功: {text}"


def fail_hint(name, text="工具调用超时 (180.0s)"):
    return f"[本轮已执行] 工具「{name}」→ 失败: {text}"


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_all_success():
    print("\n【正常 1】工具全成功 → 引导汇总，且不提示换工具")
    p = prompt([ok_hint("query_weather"), ok_hint("search_knowledge_base")])
    check("成功" in p and "direct_answer" in p, "提示汇总（direct_answer）")
    check("不要再重复调用同样的工具" in p, "提示不要重复调用")
    check("换用其他工具" not in p, "未出现「换用其他工具」（成功时不该引导重规划）")


def test_all_failed():
    print("\n【正常 2】工具全失败 → 引导「换工具重规划」或「降级回答」")
    p = prompt([fail_hint("query_weather")])
    check("全部失败" in p, "明确说明全部失败（动态引导）")
    check("不要原样重试同一个工具" in p, "禁止原样重试")
    check("换用其他能达成目标的工具重新尝试" in p, "给出「重新规划」路径")
    check("direct_answer" in p and "如实说明失败原因" in p, "给出「降级回答」路径")
    # 基座模板的静态规则也要按成败分流，不能只留"成功→不要再调用工具"
    check("**工具全部失败** → 不要原样重试同一个工具" in p,
          "基座规则的失败分支已补上（不再是只讲成功的一刀切）")


def test_partial_success():
    print("\n【正常 3】部分成功 → 两种路径都提示，且报出数量")
    p = prompt([ok_hint("query_weather"), fail_hint("search_knowledge_base")])
    check("1 个成功、1 个失败" in p, "报出成功/失败数量")
    check("已成功的结果可直接使用" in p, "说明成功结果可用")
    check("失败的那些不要原样重试" in p, "失败项禁止重试")
    check("换用其他工具补齐" in p and "direct_answer" in p, "两条路径都给出")


def test_skill_failure_recognized():
    print("\n【正常 4】Skill 失败提示同样被识别（不只是「工具」前缀）")
    p = prompt(["[本轮已执行] Skill「综合查询」→ 失败: 参数校验失败"])
    check("全部失败" in p, "Skill 失败也走「全失败」引导")
    check("换用其他能达成目标的工具重新尝试" in p, "给出重规划路径")


def test_mixed_tool_and_skill():
    print("\n【正常 5】工具成功 + Skill 失败 → 部分成功分支")
    p = prompt([ok_hint("query_weather"),
                "[本轮已执行] Skill「深度检索」→ 失败: 超时"])
    check("1 个成功、1 个失败" in p, "工具与 Skill 混合计数正确")


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_boundary_fake_status_in_result():
    print("\n【边界 1】工具结果文本里含「失败」字样，但状态是成功 → 不误判")
    p = prompt([ok_hint("search_knowledge_base", "该实验失败的原因有三点…")])
    check("成功" in p and "不要再重复调用同样的工具" in p,
          "仍按「成功」引导（正则锚定在 」→ 之后）")
    check("上述工具调用**全部失败**" not in p,
          "未被结果文本里的「失败」二字误判（动态引导仍走成功分支）")


def test_boundary_no_executed_hint():
    print("\n【边界 2】无「本轮已执行」提示 → 不追加任何执行引导")
    p = prompt(["[长期记忆·画像] 用户在做 6G 项目"])
    check("本轮已执行" not in p.replace("[长期记忆·画像]", ""), "无执行引导")
    check("不要再重复调用" not in p and "换用其他工具" not in p, "两种引导都不出现")
    check("长期记忆" in p, "长期记忆护栏仍独立生效（回归）")


def test_boundary_empty_hints():
    print("\n【边界 3】hints 为空 → 走 else 分支文案，不报错")
    p = prompt([])
    check("（无历史工具选择记录）" in p or "无历史工具" in p, "走空提示分支")


def test_boundary_only_long_term_memory():
    print("\n【边界 4】只有长期记忆、没有执行记录 → 不误导为「已执行」")
    p = prompt(["[长期记忆·项目] 用户在做 MCP 智能体项目"])
    check("上述工具" not in p, "不会错误声明「上述工具已执行」")


def test_boundary_regex_robustness():
    print("\n【边界 5】正则对格式变体的鲁棒性")
    cases = [
        ("[本轮已执行] 工具「x」→ 成功: y", ["成功"]),
        ("[本轮已执行] 工具「x」→成功: y", ["成功"]),      # 无空格
        ("[本轮已执行] Skill「s」→ 失败: y", ["失败"]),
        ("前缀 [本轮已执行] 工具「a-b_c」→ 失败: y", ["失败"]),  # 工具名含符号
        ("[本轮已执行] 工具「x」→ 未知: y", []),            # 状态异常 → 不匹配
    ]
    for text, expect in cases:
        got = _EXECUTED_STATUS_RE.findall(text)
        check(got == expect, f"{text[:34]!r} → {got}")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_malformed_hints():
    print("\n【异常 1】畸形 hint 条目 → 不抛错，且不误追加执行引导")
    weird = ["[本轮已执行]", "→ 成功: x", "[本轮已执行] 工具无状态标记",
             None, 123, ""]
    try:
        p = prompt([str(h) for h in weird])
        ok = True
    except Exception as e:
        ok, p = False, str(e)
        print(f"      抛出异常: {e}")
    check(ok, "未抛异常")
    check("换用其他工具" not in p and "不要再重复调用" not in p,
          "无有效状态记录 → 不追加任何执行引导")


def test_exception_degenerate_name_still_parses():
    print("\n【异常 2】空工具名等退化条目 → 能被解析但不崩（状态可识别即可）")
    p = prompt(["[本轮已执行] 工具「」→ 失败: x"])
    check("全部失败" in p, "退化条目按「失败」计入，走全失败引导，不崩")


def test_both_turn_templates():
    print("\n【异常 3】首轮（turn=0）与后续轮（turn>0）模板都带上引导")
    for t in (0, 1):
        p = prompt([fail_hint("query_weather")], turn=t)
        check("换用其他能达成目标的工具重新尝试" in p,
              f"turn={t} 模板含重规划引导")


def main():
    print("🧪 工具失败「重新规划 or 降级」引导验证")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_all_success()
    test_all_failed()
    test_partial_success()
    test_skill_failure_recognized()
    test_mixed_tool_and_skill()

    print("\n─── 二、边界输入 ───")
    test_boundary_fake_status_in_result()
    test_boundary_no_executed_hint()
    test_boundary_empty_hints()
    test_boundary_only_long_term_memory()
    test_boundary_regex_robustness()

    print("\n─── 三、异常场景 ───")
    test_exception_malformed_hints()
    test_exception_degenerate_name_still_parses()
    test_both_turn_templates()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
