"""Prompt 模板

包含 ReAct 决策、Skill 匹配、最终回答三套 Prompt 模板。
"""

# ── ReAct 决策 Prompt ──────────────────────────────────────────

DECISION_SYSTEM_PROMPT = """你是一个使用 ReAct 模式推理的智能助手决策引擎。

## 可用技能（优先使用）
{skills_description}

## 可用工具
{tools_description}

## 推理规则
1. **优先匹配技能**：如果用户意图与某个技能匹配，直接调用技能（输出 SKILL 格式）
2. **无匹配时推理**：无技能匹配时，使用 Thought → Action → Observation 逐步推理
3. **多工具执行模式**：
   - parallel：工具/技能间互相独立，可同时执行（如同时查天气和知识库）
   - serial：后续步骤依赖前面结果（如先检索再生成回答）
4. **参数推断**：如果用户没有明确提供所有参数值，根据上下文合理推断
5. **已执行工具的处理**：如果"工具选择历史"中显示本轮已有工具成功执行并返回结果，你必须使用 direct_answer 汇总这些结果来回答用户，**不要再调用工具**。
6. **参数格式**：工具参数必须用 ```json 代码块输出，不要使用普通文本。

## 工具选择历史（供参考）
{reflection_hints}

## 对话历史
{history}

## 用户当前问题
{user_query}

## 你的决策

使用技能时：
SKILL: <技能名>
参数: ```json
{{"参数名": "参数值"}}
```

直接推理时（严格遵守格式）：
Thought: <你的推理过程>
Action: <工具名>
参数: ```json
{{"参数名": "参数值"}}
```

直接回答时：
{{"action":"direct_answer","response":"你的回答内容"}}

请输出你的决策："""


def build_decision_prompt(
    user_input: str,
    history: list[dict],
    tools_description: str,
    reflection_hints: list[str],
    skills_description: str = "",
) -> str:
    """构建 ReAct 决策 Prompt。

    Args:
        skills_description: 可用 Skill 的描述文本（Top3候选），
                           为空时表示无匹配 Skill
    """
    # 格式化对话历史
    if history:
        history_lines = []
        for msg in history[-10:]:
            role = "用户" if msg["role"] == "user" else "助手"
            content = str(msg.get("content", ""))[:200]
            history_lines.append(f"[{role}]: {content}")
        history_text = "\n".join(history_lines)
    else:
        history_text = "（无对话历史）"

    # 格式化反思提示
    if reflection_hints:
        hints_text = "\n".join(reflection_hints)
        has_executed = any("本轮已执行" in h for h in reflection_hints)
        if has_executed:
            hints_text += (
                "\n\n⚠️ 上述工具已在本轮执行完毕并返回结果，"
                "请使用 direct_answer 汇总结果回答用户，不要再调用工具。"
            )
    else:
        hints_text = "（无历史工具选择记录）"

    return DECISION_SYSTEM_PROMPT.format(
        skills_description=skills_description or "（无匹配技能）",
        tools_description=tools_description,
        history=history_text,
        user_query=user_input,
        reflection_hints=hints_text,
    )


# ── Skill 确认 + 参数提取 Prompt ─────────────────────────────

SKILL_RECOGNITION_PROMPT = """你是一个技能匹配助手。根据用户的问题和候选技能列表，判断最佳匹配并提取参数。

## 候选技能
{skills_description}

## 对话历史
{history}

## 用户问题
{user_query}

## 任务
1. 判断哪个技能最匹配用户意图（如果都不匹配，输出 "none"）
2. 如果匹配，从用户输入和对话历史中提取技能所需的参数值
3. 参数值必须用 ```json 代码块输出

## 输出格式
匹配时：
SKILL: <技能名>
置信度: <0.0-1.0>
参数: ```json
{{"参数名": "参数值"}}
```

无匹配时：
SKILL: none
原因: <简短说明>"""


def build_skill_recognition_prompt(
    user_input: str,
    history: list[dict],
    skills_description: str,
) -> str:
    """构建 Skill 确认 + 参数提取 Prompt。"""
    if history:
        history_lines = []
        for msg in history[-6:]:
            role = "用户" if msg["role"] == "user" else "助手"
            history_lines.append(f"[{role}]: {str(msg.get('content', ''))[:150]}")
        history_text = "\n".join(history_lines)
    else:
        history_text = "（无对话历史）"

    return SKILL_RECOGNITION_PROMPT.format(
        skills_description=skills_description,
        history=history_text,
        user_query=user_input,
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
    result_lines = []
    for i, r in enumerate(tool_results, 1):
        tool_name = r.get("tool_name", "未知工具")
        args = r.get("arguments", {})
        result_text = str(r.get("result", ""))[:600]
        is_error = r.get("is_error", False)
        status = "失败" if is_error else "成功"

        result_lines.append(f"[{i}] 工具: {tool_name} ({status})")
        result_lines.append(f"    参数: {args}")
        result_lines.append(f"    结果: {result_text}")
        result_lines.append("")

    tool_results_text = "\n".join(result_lines)

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
