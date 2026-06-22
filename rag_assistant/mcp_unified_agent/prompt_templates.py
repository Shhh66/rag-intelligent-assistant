"""Prompt 模板

包含 LLM 决策和最终回答的 Prompt 模板常量及构建函数。
"""

# ── 决策系统 Prompt ───────────────────────────────────────────

DECISION_SYSTEM_PROMPT = """你是一个智能助手决策引擎。根据用户的问题和可用的工具列表，你需要自主决策如何回答。

## 可用工具
{tools_description}

## 决策规则
1. **直接回答**：如果用户的问题与可用工具的功能完全无关（如闲聊、常识问答、编程问题），直接给出回答，不要强行调用工具。
2. **调用工具**：如果问题可以通过可用工具解决，必须输出工具调用决策。
3. **多工具执行模式**：
   - parallel：工具之间互相独立，可以同时执行（如同时查天气和知识库）
   - serial：后一个工具依赖前一个工具的结果（如先检索知识库，再根据结果决定是否深入查询）
4. **参数推断**：如果用户没有明确提供所有参数值，根据上下文合理推断。

## 工具选择历史（供参考）
{reflection_hints}

## 对话历史
{history}

## 用户当前问题
{user_query}

## 你的决策（严格遵守 JSON 格式，不要输出其他内容）

直接回答时：
{{"action":"direct_answer","response":"你的回答内容"}}

调用工具时（支持单工具或多工具）：
{{"action":"call_tools","execution_mode":"serial或parallel","tools":[{{"tool_name":"工具名","arguments":{{"参数":"值"}},"reason":"选择理由"}}]}}

请输出你的决策 JSON："""


def build_decision_prompt(
    user_input: str,
    history: list[dict],
    tools_description: str,
    reflection_hints: list[str],
) -> str:
    """构建完整的决策 Prompt。"""
    # 格式化对话历史
    if history:
        history_lines = []
        for msg in history[-10:]:  # 只取最近 10 条
            role = "用户" if msg["role"] == "user" else "助手"
            content = str(msg.get("content", ""))[:200]  # 截断长内容
            history_lines.append(f"[{role}]: {content}")
        history_text = "\n".join(history_lines)
    else:
        history_text = "（无对话历史）"

    # 格式化反思提示
    if reflection_hints:
        hints_text = "\n".join(reflection_hints)
    else:
        hints_text = "（无历史工具选择记录）"

    return DECISION_SYSTEM_PROMPT.format(
        tools_description=tools_description,
        history=history_text,
        user_query=user_input,
        reflection_hints=hints_text,
    )


# ── 最终回答 Prompt ───────────────────────────────────────────

FINAL_ANSWER_SYSTEM = """你是一个智能助手。现在需要基于以下工具执行结果，回答用户的原始问题。

## 工具调用结果
{tool_results}

## 对话历史
{history}

## 回答规则
1. 优先基于工具返回的信息回答
2. 如果工具调用失败或返回错误，如实告知用户
3. 如果工具结果不足以完整回答问题，补充你自己的知识，并明确说明哪些来自工具、哪些来自模型自身
4. 回答要清晰、有条理，必要时使用列表或分段

## 用户原始问题
{user_query}

## 你的回答"""


def build_final_answer_prompt(
    user_input: str,
    history: list[dict],
    tool_results: list[dict],
) -> str:
    """构建最终回答的 Prompt。"""
    # 格式化工具结果
    result_lines = []
    for i, r in enumerate(tool_results, 1):
        tool_name = r.get("tool_name", "未知工具")
        args = r.get("arguments", {})
        result_text = str(r.get("result", ""))[:600]  # 截断超长结果
        is_error = r.get("is_error", False)
        status = "失败" if is_error else "成功"

        result_lines.append(f"[{i}] 工具: {tool_name} ({status})")
        result_lines.append(f"    参数: {args}")
        result_lines.append(f"    结果: {result_text}")
        result_lines.append("")

    tool_results_text = "\n".join(result_lines)

    # 格式化对话历史
    if history:
        history_lines = []
        for msg in history[-6:]:
            role = "用户" if msg["role"] == "user" else "助手"
            history_lines.append(f"[{role}]: {str(msg.get('content', ''))[:150]}")
        history_text = "\n".join(history_lines)
    else:
        history_text = "（无对话历史）"

    return FINAL_ANSWER_SYSTEM.format(
        tool_results=tool_results_text,
        history=history_text,
        user_query=user_input,
    )
