"""成本控制：简化模型网关（轻量 MVP）

职责：
1. 模型路由：按 call_site 路由模型——简单任务（意图判断/参数提取/技能匹配）
   用便宜小模型，强推理（ReAct 决策/最终回答）用大模型。
2. 单任务 Token 预算：跟踪单次任务累计 Token，超预算强制终止返回中间结果。

设计原则（零破坏）：
- 不修改已有函数签名；仅在 call_llm_with_cb 里新增一行路由调用、
  unified_agent._pipeline 循环里新增一段预算检查。
- MODEL_ROUTE 为空 / TASK_TOKEN_BUDGET=0 时，行为与改造前完全一致。

说明：当前 `deepseek-flash` 既是唯一在用模型、也是性价比最优的一档
（V4.1-Flash 在并发 2500 与图像理解上均优于 v4-pro 的 500 / 不支持，价格还更低），
因此 `MODEL_ROUTE` 默认留空、不做路由 —— 「强推理切 v4-pro」没有必要。

路由机制保留：将来接入第二个模型（其他厂商 / 本地模型 / 更便宜的小模型）时，
填 `MODEL_ROUTE` 即生效，无需改代码。

当前真正在起作用的成本控制是「单任务 Token 预算」——防多轮 ReAct 循环把 token 打爆。

用法：
    from model_gateway import route_model, check_budget
    model = route_model("decision_engine.match_skill", "deepseek-flash")
    exceeded = check_budget(8000)
"""

import logging

logger = logging.getLogger(__name__)


def route_model(call_site: str, default_model: str) -> str:
    """按 call_site 路由模型。

    Args:
        call_site: 调用点标识（如 "decision_engine.match_skill"）
        default_model: 调用方传入的默认模型

    Returns:
        路由后的模型名。未命中路由表 / 未配置 → 返回 default_model（行为不变）。
    """
    try:
        import config
        route = getattr(config, "MODEL_ROUTE", {}) or {}
    except Exception:
        route = {}
    return route.get(call_site, default_model)


def check_budget(budget: int) -> bool:
    """检查单任务 Token 是否超预算。

    Args:
        budget: 单任务 Token 预算（<=0 视为不限）

    Returns:
        True=超预算（应强制终止）；False=未超。
    """
    if budget <= 0:
        return False
    try:
        from token_tracker import get_tracker
        used = get_tracker().get_conversation_diff()["total_tokens"]
    except Exception:
        return False
    return used >= budget


# ===== 自测 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== 模型网关 MVP 演示 ===\n")

    # 1. 模型路由：未配置 → 默认模型
    print("[1] 模型路由（未配置路由表 → 用默认模型）:")
    print(f"    match_skill → {route_model('decision_engine.match_skill', 'deepseek-flash')}")

    # 2. 模拟配置路由（仅演示机制：当前无第二个模型可路由，用占位名示意）
    import config
    config.MODEL_ROUTE = {
        "decision_engine.match_skill": "cheap-model-A",   # 示例：简单任务 → 某个便宜模型
        "judge.evaluate": "cheap-model-A",                # 示例：简单判断 → 某个便宜模型
    }
    print("\n[2] 配置路由后（演示机制：命中路由表用映射值，未命中回退默认）:")
    print(f"    match_skill → {route_model('decision_engine.match_skill', 'deepseek-flash')}")
    print(f"    judge       → {route_model('judge.evaluate', 'deepseek-flash')}")
    print(f"    decide      → {route_model('decision_engine.decide', 'deepseek-flash')}（未配置 → 回退默认）")

    # 3. token 预算边界
    print("\n[3] Token 预算边界:")
    print(f"    check_budget(0)  = {check_budget(0)}（0=不限 → False）")
    print(f"    check_budget(-1) = {check_budget(-1)}（<=0 → False）")

    # 4. token 预算：模拟累计超预算（临时文件避免污染 token_log.jsonl）
    print("\n[4] Token 预算检查（模拟累计 6000 token）:")
    from pathlib import Path
    import token_tracker as _tt
    _orig = _tt._PERSIST_FILE
    _tt._PERSIST_FILE = Path("_test_token_log.jsonl")
    try:
        from token_tracker import get_tracker
        tracker = get_tracker()
        tracker.start_conversation()

        class MockUsage:
            def __init__(self, p, c, t):
                self.prompt_tokens = p
                self.completion_tokens = c
                self.total_tokens = t

        tracker.record("deepseek-flash", MockUsage(5000, 1000, 6000), call_site="test")
        used = tracker.get_conversation_diff()["total_tokens"]
        print(f"    累计 token = {used}")
        print(f"    check_budget(4000) = {check_budget(4000)}（超 4000 → 应 True）")
        print(f"    check_budget(8000) = {check_budget(8000)}（未超 8000 → 应 False）")
    finally:
        _tt._PERSIST_FILE = _orig
        Path("_test_token_log.jsonl").unlink(missing_ok=True)

    print("\n✅ 模型网关 MVP 演示完成")
