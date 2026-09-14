"""Prompt 模板

包含 ReAct 决策（五阶段：Plan→Thought→Action→Observation→Evaluation→Decision）、
Skill 匹配、最终回答三套 Prompt 模板。
"""

# ── ReAct 决策 Prompt（五阶段） ─────────────────────────────────

# 首轮 Prompt：强制输出规划
DECISION_PROMPT_TURN0 = """你是一个使用 ReAct 模式推理的智能助手决策引擎。

## 可用技能（优先使用）
{skills_description}

## 可用工具
{tools_description}

## 推理规则
1. **首轮必须输出 Plan**：先规划完成用户问题需要几步、每步做什么，再执行
2. **逐步推理**：使用 Plan → Thought → Action 逐步推理，不要跳步
3. **多工具执行模式**：
   - parallel：工具间互相独立，可同时执行（如同时查天气和知识库）
   - serial：后续步骤依赖前面结果（如先检索再生成回答）
4. **参数推断**：如果用户没有明确提供所有参数值，根据上下文合理推断
5. **参数格式**：工具参数必须用 ```json 代码块输出，不要使用普通文本。

## 工具选择历史（供参考）
{reflection_hints}

## 对话历史
{history}

## 用户当前问题
{user_query}

## 你的决策（首轮，必须包含 Plan，严格遵守 JSON 格式）

你必须输出一个合法的 JSON 对象，格式如下：

调用工具时：
```json
{{
  "plan": "1. [步骤1] 2. [步骤2] 3. [步骤3]",
  "thought": "<你的推理过程>",
  "action": "call_tools",
  "tools": [{{"tool_name": "<工具名>", "arguments": {{"参数名": "参数值"}}}}],
  "execution_mode": "serial"
}}
```

直接回答时：
```json
{{
  "plan": "无需工具调用",
  "thought": "<你的推理过程>",
  "action": "direct_answer",
  "direct_response": "<你的回答内容>"
}}
```

请输出你的决策 JSON："""

# 后续轮次 Prompt：包含评估+决策
DECISION_PROMPT_TURN_N = """你是一个使用 ReAct 模式推理的智能助手决策引擎。

## 可用技能（优先使用）
{skills_description}

## 可用工具
{tools_description}

## 当前规划（首轮制定）
{current_plan}

## 推理规则
1. **先评估再行动**：每轮必须先输出 Evaluation（评估当前进展）和 Decision（决定下一步）
2. **Decision 含义**：
   - continue：计划未完成，继续执行下一步
   - answer：信息已足够，使用 direct_answer 汇总回答
   - abort：遇到无法解决的问题，强制终止并说明原因
3. **已执行工具的处理**：如果"工具选择历史"中显示本轮已有工具成功执行并返回结果，你必须使用 direct_answer 汇总这些结果来回答用户，**不要再调用工具**。

## 工具选择历史（供参考）
{reflection_hints}

## 对话历史
{history}

## 用户当前问题
{user_query}

## 你的决策（第 {turn} 轮，严格遵守 JSON 格式）

你必须输出一个合法的 JSON 对象，格式如下：

继续推理时：
```json
{{
  "evaluation": "<已完成X步，剩余Y步，当前信息是否足够>",
  "decision": "continue",
  "thought": "<你的推理过程>",
  "action": "call_tools",
  "tools": [{{"tool_name": "<工具名>", "arguments": {{"参数名": "参数值"}}}}],
  "execution_mode": "serial"
}}
```

汇总回答时（信息已足够）：
```json
{{
  "evaluation": "<所有必要信息已获取>",
  "decision": "answer",
  "thought": "<你的推理过程>",
  "action": "direct_answer",
  "direct_response": "<你的回答内容>"
}}
```

强制终止时（遇到无法解决的问题）：
```json
{{
  "evaluation": "<说明无法继续的原因>",
  "decision": "abort",
  "thought": "<你的推理过程>",
  "action": "direct_answer",
  "direct_response": "抱歉，无法完成请求：[具体原因]"
}}
```

请输出你的决策 JSON："""


def build_decision_prompt(
    user_input: str,
    history: list[dict],
    tools_description: str,
    reflection_hints: list[str],
    skills_description: str = "",
    turn: int = 0,
    current_plan: str = "",
) -> str:
    """构建 ReAct 决策 Prompt（五阶段版本）。

    Args:
        skills_description: 可用 Skill 的描述文本（Top3候选），
                           为空时表示无匹配 Skill
        turn: 当前轮次（0=首轮，>0=后续轮次）
        current_plan: 当前已有的规划内容（后续轮次注入）
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
        # 长期记忆护栏：降低记忆污染导致的幻觉
        if any("长期记忆" in h for h in reflection_hints):
            hints_text += (
                "\n\n💡 「长期记忆」是跨会话沉淀的用户背景，"
                "仅在与当前问题相关时参考，无关内容请直接忽略，不要生搬硬套。"
            )
    else:
        hints_text = "（无历史工具选择记录）"

    # 根据轮次选择 Prompt 模板
    if turn == 0:
        return DECISION_PROMPT_TURN0.format(
            skills_description=skills_description or "（无匹配技能）",
            tools_description=tools_description,
            history=history_text,
            user_query=user_input,
            reflection_hints=hints_text,
        )
    else:
        return DECISION_PROMPT_TURN_N.format(
            skills_description=skills_description or "（无匹配技能）",
            tools_description=tools_description,
            current_plan=current_plan or "（无规划记录）",
            turn=turn,
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
1. 判断哪个技能最匹配用户意图（如果都不匹配，skill_name 输出 "none"）
2. 如果匹配，从用户输入和对话历史中提取技能所需的参数值填入 args

## 输出格式
必须输出一个合法的 JSON 对象：

匹配时：
{{"skill_name": "<技能名>", "confidence": 0.92, "args": {{"参数名": "参数值"}}, "reason": "<匹配说明>"}}

无匹配时：
{{"skill_name": "none", "confidence": 0.0, "args": {{}}, "reason": "<不匹配原因>"}}"""


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
5. **重要**：工具结果中可能已包含来源标注（如文件名、页码），你必须在回答中**原样保留**这些出处信息。回答末尾列出参考来源，格式：
   > 📚 参考来源：
   > - 文件名，第X页

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
        result_text = str(r.get("result", ""))
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


# ── 会话摘要压缩 Prompt（早期对话 → 摘要，滚动更新）────────────

SESSION_SUMMARY_PROMPT = """你是一个对话摘要压缩器。把下面的对话压缩成简洁摘要。

## 已有摘要（如有，需与新内容合并；无则忽略）
{existing_summary}

## 待压缩的对话
{dialogue}

## 压缩要求
1. **必须保留**：用户的核心诉求与目标、已确认的结论/决策/约束（如"只用中文"）、未完成的待办、关键实体（项目名、专有名词、数字）
2. **可以丢弃**：寒暄、重复确认、失败尝试的中间过程
3. 输出长度控制在约 {target_tokens} token
4. 用陈述句写摘要，直接输出摘要正文，不要 JSON、不要解释、不要加标题

## 摘要："""


def build_session_summary_prompt(
    existing_summary: str,
    dialogue: str,
    target_tokens: int = 800,
) -> str:
    """构建会话摘要压缩 Prompt（滚动压缩：旧摘要参与合并）。"""
    return SESSION_SUMMARY_PROMPT.format(
        existing_summary=existing_summary or "（无，这是首次压缩）",
        dialogue=dialogue,
        target_tokens=target_tokens,
    )


# ── 摘要校验 Prompt（压缩后比对原文，找出丢失的关键信息）─────────

SUMMARY_VERIFY_PROMPT = """你是一个摘要质量校验器。判断下面的【摘要】是否完整保留了【原始对话】中的关键信息。

## 原始对话
{dialogue}

## 摘要
{summary}

## 关键信息定义（缺少任一即算不完整）
- 用户的核心诉求与目标
- 已确认的结论 / 决策 / 约束（如"只用中文回答"、"排除某部门文档"）
- 未完成的待办与进行中的任务
- 关键实体（项目名、专有名词、数字）

## 输出格式
只输出 JSON，不要解释：
{{"ok": true 或 false, "missing": ["缺失的关键信息1", "缺失的关键信息2"]}}
- ok=true：关键信息均已保留（missing 给空数组）
- ok=false：有遗漏，missing 逐条列出【摘要中丢失、但原文里有】的关键信息（每条一句话）

## JSON："""


def build_summary_verify_prompt(dialogue: str, summary: str) -> str:
    """构建摘要校验 Prompt（比对原文与摘要，找出遗漏的关键信息）。"""
    return SUMMARY_VERIFY_PROMPT.format(dialogue=dialogue, summary=summary)


# ── 摘要定向重压 Prompt（校验发现遗漏后，带着缺失项重压一次）────

SUMMARY_RETRY_PROMPT = """你是一个对话摘要压缩器。上一次的摘要遗漏了关键信息，请重新压缩并补全。

## 已有摘要（需与新内容合并）
{existing_summary}

## 待压缩的对话
{dialogue}

## ⚠️ 上一次遗漏的关键信息（必须补进新摘要）
{missing}

## 压缩要求
1. **必须保留**：用户的核心诉求与目标、已确认的结论/决策/约束、未完成的待办、关键实体（项目名、专有名词、数字）
2. **必须补全**：上面列出的「上一次遗漏的关键信息」
3. **可以丢弃**：寒暄、重复确认、失败尝试的中间过程
4. 输出长度控制在约 {target_tokens} token
5. 用陈述句写摘要，直接输出摘要正文，不要 JSON、不要解释、不要加标题

## 摘要："""


def build_summary_retry_prompt(
    existing_summary: str,
    dialogue: str,
    missing: list,
    target_tokens: int = 800,
) -> str:
    """构建定向重压 Prompt（把校验发现的缺失项显式喂回，要求补全）。"""
    return SUMMARY_RETRY_PROMPT.format(
        existing_summary=existing_summary or "（无，这是首次压缩）",
        dialogue=dialogue,
        missing="\n".join(f"- {m}" for m in (missing or [])) or "（无）",
        target_tokens=target_tokens,
    )


# ── 长期记忆抽取 Prompt（从会话摘要中提长期有效信息）──────────

MEMORY_EXTRACT_PROMPT = """你是一个长期记忆抽取器。从下面的会话摘要中，只抽取【长期有效】的用户信息。

## 会话摘要
{summary}

## 抽取口径
✅ **保留**：
- 用户偏好（专业方向、习惯、明确的好恶）
- 固定参数 / 固定约束（如"总是用中文回答"、"排除某部门文档"）
- 业务规则（领域内的固定规则）
- 高频工具选择经验

❌ **丢弃**：
- 本次临时任务（一次性的具体问题）
- 中间思考过程
- 一次性问答

## 输出格式
按 JSON 数组输出，每项 {{"mem_type":"profile|entity|conclusion","content":"一句话事实","confidence":0~1}}
- profile = 用户画像（身份/专业/固定偏好/固定约束）
- entity = 项目/关注的技术
- conclusion = 已确认的结论

没有值得长期记住的内容就输出 []。只输出 JSON，不要解释。

## JSON："""


def build_memory_extract_prompt(summary: str) -> str:
    """构建从会话摘要抽取长期记忆的 Prompt。"""
    return MEMORY_EXTRACT_PROMPT.format(summary=summary)
