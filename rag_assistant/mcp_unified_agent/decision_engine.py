"""LLM 决策引擎

构建 Prompt → 调用 LLM → 解析结构化 JSON 决策。

负责将工具元数据、对话历史、长期记忆上下文注入 Prompt，
然后解析 LLM 返回的 JSON 决策为 AgentDecision 对象。

输出约束：采用 Structured Outputs（response_format=json_schema），
强制 LLM 输出符合 REACT_DECISION_SCHEMA 的 JSON。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from openai import OpenAI

from .prompt_templates import (
    build_decision_prompt,
    build_final_answer_prompt,
    build_skill_recognition_prompt,
)
from .tool_registry import ToolMeta
from .skill_registry import SkillRegistry
from .circuit_breaker import call_llm_with_cb, CircuitBreakerError

logger = logging.getLogger(__name__)


# ── Structured Outputs Schema ─────────────────────────────────
# 强制 LLM 输出符合此 Schema 的 JSON，消除正则解析的不确定性

REACT_DECISION_SCHEMA = {
    "name": "react_decision",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": "任务规划（首轮必填），如：1. 查天气 2. 回答"
            },
            "thought": {
                "type": "string",
                "description": "推理过程，说明为什么选择这个行动"
            },
            "evaluation": {
                "type": "string",
                "description": "评估当前进展（后续轮次必填），如：天气已查到，信息足够"
            },
            "decision": {
                "type": "string",
                "enum": ["continue", "answer", "abort"],
                "description": "下一步决策：continue=继续, answer=汇总回答, abort=终止"
            },
            "action": {
                "type": "string",
                "enum": ["call_tools", "direct_answer"],
                "description": "行动类型：call_tools=调用工具, direct_answer=直接回答"
            },
            "tools": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool_name": {"type": "string", "description": "工具名称"},
                        "arguments": {"type": "object", "description": "工具参数"}
                    },
                    "required": ["tool_name", "arguments"],
                    "additionalProperties": False
                },
                "description": "工具列表（action=call_tools 时必填）"
            },
            "execution_mode": {
                "type": "string",
                "enum": ["serial", "parallel"],
                "description": "执行模式：serial=串行, parallel=并行"
            },
            "direct_response": {
                "type": "string",
                "description": "直接回答内容（action=direct_answer 时必填）"
            }
        },
        "required": ["action"],
        "additionalProperties": False
    }
}

# response_format 参数（传给 OpenAI API）
# 优先 json_schema（Structured Outputs），不支持时降级 json_object
REACT_RESPONSE_FORMAT_STRICT = {
    "type": "json_schema",
    "json_schema": REACT_DECISION_SCHEMA,
}
REACT_RESPONSE_FORMAT_FALLBACK = {
    "type": "json_object",
}

# Skill 匹配的 Structured Outputs Schema
# 与主决策一致：json_schema 优先，DeepSeek 不支持时降级 json_object
SKILL_MATCH_SCHEMA = {
    "name": "skill_match",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "skill_name": {
                "type": "string",
                "description": "匹配的技能名，无匹配时为 'none'",
            },
            "confidence": {
                "type": "number",
                "description": "置信度 0.0-1.0",
            },
            "args": {
                "type": "object",
                "description": "从用户输入和对话历史中提取的技能参数",
            },
            "reason": {
                "type": "string",
                "description": "匹配或不匹配的简要说明",
            },
        },
        "required": ["skill_name", "confidence", "args", "reason"],
        "additionalProperties": False,
    },
}

SKILL_MATCH_RESPONSE_FORMAT_STRICT = {
    "type": "json_schema",
    "json_schema": SKILL_MATCH_SCHEMA,
}
SKILL_MATCH_RESPONSE_FORMAT_FALLBACK = {
    "type": "json_object",
}

# ── json_schema（Structured Outputs）支持性探测缓存 ──────────────
# DeepSeek 等 API 不支持 json_schema，首次请求必返 400。
# 用模块级标记记住探测结果，避免每次调用都白发一个注定失败的请求
# （原先每轮 ReAct 决策 / 每次 Skill 匹配都要多一次 400 往返）。
# None = 尚未探测；True = 支持；False = 不支持（之后直接用 json_object）。
_JSON_SCHEMA_SUPPORTED: bool | None = None


def _is_json_schema_unavailable(err_msg: str) -> bool:
    """判断异常信息是否为「json_schema 类型当前不可用」。"""
    return "response_format" in err_msg and "unavailable" in err_msg


def _mark_json_schema_unsupported() -> None:
    """首次探测到不支持后置位；后续调用直接走 fallback，不再空试一次。"""
    global _JSON_SCHEMA_SUPPORTED
    if _JSON_SCHEMA_SUPPORTED is not False:
        _JSON_SCHEMA_SUPPORTED = False
        logger.info("已记录：当前 API 不支持 json_schema，后续直接使用 json_object")


def _pick_response_format(strict: dict, fallback: dict) -> dict:
    """按探测结果选 response_format：已知不支持时直接用 fallback。"""
    return fallback if _JSON_SCHEMA_SUPPORTED is False else strict


@dataclass
class ToolDecision:
    """LLM 对单个工具的调用决策"""
    tool_name: str
    arguments: dict
    reason: str = ""


@dataclass
class AgentDecision:
    """LLM 的完整决策（基于 ReAct 范式自研五阶段）

    五阶段：Plan → Thought → Action → Observation → Evaluation → Decision
    - plan: 规划内容（首轮输出）
    - thought: 推理过程（说明为什么选择这个行动）
    - evaluation: 评估内容（后续轮次：当前进展、信息是否足够）
    - decision: continue / answer / abort（后续轮次：下一步决策）
    """
    action: Literal["direct_answer", "call_tools", "call_skill"]
    tools: list[ToolDecision] = field(default_factory=list)
    execution_mode: str = "serial"
    direct_response: str | None = None
    skill_name: str = ""       # call_skill 时的 Skill 名
    skill_args: dict = field(default_factory=dict)  # call_skill 时的参数
    plan: str = ""             # 规划内容（首轮）
    thought: str = ""          # 推理过程
    evaluation: str = ""       # 评估内容（后续轮次）
    decision: str = "continue"  # continue / answer / abort


@dataclass
class SkillMatchResult:
    """Skill 匹配 + 参数提取的完整结果"""
    skill_name: str
    confidence: float
    args: dict
    raw_response: str = ""


class DecisionEngine:
    """决策引擎：构建 Prompt → 调用 LLM → 解析结构化 JSON 决策。

    负责注入工具元数据、对话历史、长期记忆上下文到 Prompt，
    然后解析 LLM 返回的 JSON 决策为 AgentDecision 对象。
    """

    def __init__(self, llm_client: OpenAI, model: str = "llama-3.3-70b-versatile"):
        self.client = llm_client
        self.model = model
        self.session_summary: str = ""   # 会话摘要（由 UnifiedAgent 每请求注入；独立使用时为空）

    # ── 主决策 ────────────────────────────────────────────────

    def decide(
        self,
        user_input: str,
        history: list[dict],
        tools: list[ToolMeta],
        reflection_hints: list[str],
    ) -> AgentDecision:
        """调用 LLM 做决策。

        步骤：
        1. 构建决策 Prompt（注入：用户问题 + 历史 + 工具列表 + 反思提示）
        2. 调用 Groq LLM（temperature=0.1）
        3. 从 JSON 响应中解析 AgentDecision
        4. 解析失败时降级为 direct_answer
        """
        # 构建工具描述文本
        from .tool_registry import ToolRegistry
        temp_registry = ToolRegistry()
        temp_registry.load(tools)  # load() 兼容 MCP Tool 和 ToolMeta
        tools_description = temp_registry.format_for_prompt()

        # 构建 Prompt
        prompt = build_decision_prompt(
            user_input=user_input,
            history=history,
            tools_description=tools_description,
            reflection_hints=reflection_hints,
        )

        # 组装消息
        messages = [
            {"role": "system", "content": "你是一个精确的决策引擎，只输出 JSON。"},
        ]
        # 将历史作为上下文注入 user 消息中（prompt 已包含历史文本）
        messages.append({"role": "user", "content": prompt})

        try:
            response = call_llm_with_cb(
                self.client, self.model, messages,
                temperature=0.1, max_tokens=4000, call_site="decision_engine.decide",
            )
            raw_text = response.choices[0].message.content or ""
            logger.debug(f"LLM 决策原始输出: {raw_text[:300]}")
            return self._parse_decision(raw_text)

        except CircuitBreakerError:
            raise  # 熔断打开：快速失败，冒泡到 unified_agent 入口
        except Exception as e:
            logger.error(f"LLM 决策调用失败: {e}")
            # 降级：直接回答
            return AgentDecision(
                action="direct_answer",
                direct_response=f"抱歉，决策引擎出现错误: {e}",
            )

    def final_answer(
        self,
        user_input: str,
        history: list[dict],
        tool_results: list[dict],
    ) -> str:
        """在工具调用完成后，汇总结果生成最终回答。

        Prompt 包含：
        - 原始用户问题
        - 对话历史
        - 每条工具调用的名称、参数、返回结果
        - 指示：基于工具结果回答，标注来源
        """
        prompt = build_final_answer_prompt(
            user_input=user_input,
            history=history,
            tool_results=tool_results,
        )

        messages = [
            {"role": "system", "content": "你是一个严谨的智能助手，基于工具结果回答用户问题。"},
            {"role": "user", "content": prompt},
        ]

        try:
            response = call_llm_with_cb(
                self.client, self.model, messages,
                temperature=0.3, max_tokens=4000, call_site="decision_engine.final_answer",
            )
            content = response.choices[0].message.content or "（无回答）"
            return content
        except CircuitBreakerError:
            raise
        except Exception as e:
            logger.error(f"最终回答生成失败: {e}")
            return f"抱歉，生成最终回答时出现错误: {e}"

    # ── JSON 解析（含容错）────────────────────────────────────

    def _parse_decision(self, raw_text: str) -> AgentDecision:
        """从 LLM 文本中提取 JSON 并解析为 AgentDecision。

        容错策略：
        1. 提取 ```json ... ``` 代码块或裸 JSON
        2. json.loads 解析
        3. 校验必要字段（action, tools/response）
        4. 失败时返回 direct_answer（以原始文本为回答内容）
        """
        if not raw_text:
            return AgentDecision(action="direct_answer",
                                 direct_response="（LLM 未返回内容）")

        # 策略 1: 提取 ```json ... ``` 代码块
        match = re.search(r'```json\s*(.*?)\s*```', raw_text, re.DOTALL)
        if match:
            json_str = match.group(1)
        else:
            # 策略 2: 提取第一个 { 到最后一个 } 之间的内容
            start = raw_text.find('{')
            end = raw_text.rfind('}')
            if start >= 0 and end > start:
                json_str = raw_text[start:end + 1]
            else:
                # 策略 3: 无法解析，降级为直接回答
                return AgentDecision(
                    action="direct_answer",
                    direct_response=raw_text.strip(),
                )

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"JSON 解析失败: {e}")
            return AgentDecision(
                action="direct_answer",
                direct_response=raw_text.strip(),
            )

        # 校验 action 字段
        action = data.get("action", "")
        if action not in ("direct_answer", "call_tools"):
            return AgentDecision(
                action="direct_answer",
                direct_response=raw_text.strip(),
            )

        # 直接回答
        if action == "direct_answer":
            return AgentDecision(
                action="direct_answer",
                direct_response=data.get("response", raw_text.strip()),
            )

        # 工具调用
        tools = []
        raw_tools = data.get("tools", [])
        if isinstance(raw_tools, list):
            for t in raw_tools:
                if isinstance(t, dict) and "tool_name" in t:
                    tools.append(ToolDecision(
                        tool_name=t["tool_name"],
                        arguments=t.get("arguments", {}),
                        reason=t.get("reason", ""),
                    ))

        execution_mode = data.get("execution_mode", "serial")
        if execution_mode not in ("serial", "parallel"):
            execution_mode = "serial"

        return AgentDecision(
            action="call_tools",
            tools=tools,
            execution_mode=execution_mode,
        )

    # ── Skill 确认 + 参数提取 ──────────────────────────────────

    def match_skill(
        self,
        user_input: str,
        history: list[dict],
        candidate_skills: list[dict],
    ) -> SkillMatchResult | None:
        """用 LLM 确认最佳匹配 Skill + 提取参数。

        Args:
            candidate_skills: SkillRegistry.match() 返回的 Top3 候选

        Returns:
            SkillMatchResult 或 None（LLM 判断都不匹配）
        """
        temp_registry = SkillRegistry()
        temp_registry._skills = candidate_skills
        skills_desc = temp_registry.format_for_prompt(candidate_skills)

        prompt = build_skill_recognition_prompt(
            user_input=user_input,
            history=history,
            skills_description=skills_desc,
        )

        messages = [
            {"role": "system", "content": "你是一个精确的技能匹配引擎，只输出 JSON。"},
            {"role": "user", "content": prompt},
        ]

        response_format = _pick_response_format(
            SKILL_MATCH_RESPONSE_FORMAT_STRICT, SKILL_MATCH_RESPONSE_FORMAT_FALLBACK
        )

        for attempt in range(2):  # 解析失败允许重试 1 次
            try:
                response = call_llm_with_cb(
                    self.client, self.model, messages,
                    temperature=0.1, max_tokens=4000,
                    call_site="decision_engine.match_skill",
                    response_format=response_format,
                )
                raw_text = response.choices[0].message.content or ""
                logger.debug(f"Skill 确认原始输出: {raw_text[:200]}")
                result = self._parse_skill_result(raw_text)
                if result is not None:
                    return result
                # 解析失败 → 重试
                if attempt == 0:
                    messages.append({
                        "role": "assistant", "content": raw_text
                    })
                    messages.append({
                        "role": "user",
                        "content": "输出格式有误，请严格按照 JSON Schema 输出。"
                    })
            except CircuitBreakerError:
                raise
            except Exception as e:
                err_msg = str(e)
                # json_schema 不支持时降级到 json_object
                if _is_json_schema_unavailable(err_msg):
                    logger.warning(f"json_schema 不支持，降级到 json_object: {e}")
                    _mark_json_schema_unsupported()
                    response_format = SKILL_MATCH_RESPONSE_FORMAT_FALLBACK
                    continue
                logger.error(f"Skill 匹配 LLM 调用失败: {e}")
                break

        return None

    def _parse_skill_result(self, raw_text: str) -> SkillMatchResult | None:
        """解析 Structured Outputs 返回的 JSON（硬约束版本）。

        Schema 保证输出是合法 JSON，直接 json.loads + 字段映射。
        skill_name 为 "none" 表示无匹配，返回 None。
        """
        if not raw_text:
            return None

        try:
            data = json.loads(raw_text.strip())
        except json.JSONDecodeError as e:
            logger.warning(f"Skill 匹配返回非法 JSON（不应发生）: {e}")
            return None

        skill_name = (data.get("skill_name") or "").strip()
        if not skill_name or skill_name.lower() == "none":
            return None

        confidence = data.get("confidence")
        if not isinstance(confidence, (int, float)):
            # 降级到 json_object 时可能返回字符串数字（如 "0.92"），尝试转换
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.7  # 缺省置信度，兼容旧逻辑

        args = data.get("args") or {}
        if not isinstance(args, dict):
            args = {}

        logger.info(
            f"Skill 确认: {skill_name} (置信度={confidence:.2f}, "
            f"参数={list(args.keys())})"
        )
        return SkillMatchResult(
            skill_name=skill_name,
            confidence=float(confidence),
            args=args,
            raw_response=raw_text,
        )

    # ── system 构建（单条 system，两块结构）────────────────────

    def _build_system_content(
        self, base: str = "你是一个使用 ReAct 模式推理的智能助手决策引擎。"
    ) -> str:
        """构建 system 内容：基础系统提示词 + 【历史摘要】段落。

        单条 system、两块结构——不新增多条 system 消息。
        无摘要时只返回基础提示词（行为与改造前完全一致）。
        """
        summary = (getattr(self, "session_summary", "") or "").strip()
        if summary:
            return f"{base}\n\n【历史摘要】：{summary}"
        return base

    # ── ReAct 输出解析 ────────────────────────────────────────

    def decide_with_skills(
        self,
        user_input: str,
        history: list[dict],
        tools: list[ToolMeta],
        reflection_hints: list[str],
        skills_candidates: list[dict] | None = None,
        turn: int = 0,
        current_plan: str = "",
    ) -> AgentDecision:
        """增强版决策：优先 Skill 匹配 + ReAct 推理（五阶段版本）。

        返回 AgentDecision，action 可能是 call_skill / call_tools / direct_answer。
        五阶段：Plan → Thought → Action → Observation → Evaluation → Decision

        Args:
            turn: 当前轮次（0=首轮，>0=后续轮次）
            current_plan: 当前已有的规划内容（后续轮次注入）
        """
        from .tool_registry import ToolRegistry
        temp_registry = ToolRegistry()
        temp_registry.load(tools)
        tools_description = temp_registry.format_for_prompt()

        # 格式化 Skills 描述（只放 Top3，不放全量）
        if skills_candidates:
            temp_skill_reg = SkillRegistry()
            temp_skill_reg._skills = skills_candidates
            skills_description = temp_skill_reg.format_for_prompt(skills_candidates)
        else:
            skills_description = ""

        # 【已移除】曾把 Skill 名注入「可用工具」列表（标题「可调用的技能（作为高级工具使用）」），
        # 想让 ReAct 循环能调 Skill。但 LLM 输出的 action 只有 call_tools / direct_answer
        # （见 REACT_DECISION_SCHEMA 的 enum），call_skill 分支不可达 —— 于是 LLM 只能用
        # call_tools 去调 Skill 名（deep_kb_search / weather_advice），而它们不在 MCP 工具
        # 注册表里，必然报「工具不存在」。Skill 的正确入口是 unified_agent 的前置匹配
        # （SkillRegistry.match → LLM 确认 → SkillExecutor），不经过 ReAct 循环。

        prompt = build_decision_prompt(
            user_input=user_input,
            history=history,
            tools_description=tools_description,
            reflection_hints=reflection_hints,
            skills_description=skills_description,
            turn=turn,
            current_plan=current_plan,
        )

        messages = [
            {"role": "system", "content": self._build_system_content()},
            {"role": "user", "content": prompt},
        ]

        raw_text = ""
        response_format = _pick_response_format(
            REACT_RESPONSE_FORMAT_STRICT, REACT_RESPONSE_FORMAT_FALLBACK
        )

        for attempt in range(2):
            try:
                response = call_llm_with_cb(
                    self.client, self.model, messages,
                    temperature=0.1, max_tokens=4000,
                    call_site="decision_engine.decide",
                    response_format=response_format,
                )
                raw_text = response.choices[0].message.content or ""
                logger.debug(f"ReAct 决策原始输出: {raw_text[:300]}")
                decision = self._parse_react_output(raw_text)
                if decision is not None:
                    return decision

                # 重试（Structured Outputs 一般不会解析失败，保留兜底）
                if attempt == 0:
                    messages.append({"role": "assistant", "content": raw_text})
                    messages.append({
                        "role": "user",
                        "content": "输出格式有误，请严格按照 JSON Schema 输出。"
                    })
            except CircuitBreakerError:
                raise
            except Exception as e:
                err_msg = str(e)
                # json_schema 不支持时降级到 json_object
                if _is_json_schema_unavailable(err_msg):
                    logger.warning(f"json_schema 不支持，降级到 json_object: {e}")
                    _mark_json_schema_unsupported()
                    response_format = REACT_RESPONSE_FORMAT_FALLBACK
                    continue
                logger.error(f"ReAct 决策调用失败: {e}")
                break

        logger.warning(
            f"ReAct 决策解析失败（2次尝试），LLM 最后输出前200字: "
            f"{raw_text[:200] if raw_text else '(空)'}"
        )
        return AgentDecision(
            action="direct_answer",
            direct_response=(
                "抱歉，我暂时无法处理这个请求。可能是问题涉及的步骤太多"
                "（如需要同时查询大量城市的天气），建议分批次提问。"
            ),
        )

    def _parse_react_output(self, raw_text: str) -> AgentDecision | None:
        """解析 Structured Outputs 返回的 JSON（硬约束版本）。

        由于 response_format=json_schema 保证了输出是合法 JSON，
        解析逻辑大幅简化：直接 json.loads + 字段映射。
        五阶段字段：plan, thought, evaluation, decision
        """
        if not raw_text:
            return None

        text = raw_text.strip()

        # Structured Outputs 保证是合法 JSON，直接解析
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"Structured Outputs 返回非法 JSON（不应发生）: {e}")
            return None

        # 提取五阶段字段（Schema 已保证类型正确）
        action = data.get("action", "direct_answer")
        plan = data.get("plan", "")
        thought = data.get("thought", "")
        evaluation = data.get("evaluation", "")
        decision = data.get("decision", "continue")

        # decision=abort → 强制转为 direct_answer
        if decision == "abort":
            return AgentDecision(
                action="direct_answer",
                direct_response=data.get("direct_response", "")
                    or evaluation
                    or "抱歉，遇到无法解决的问题，请求终止。",
                plan=plan,
                thought=thought,
                evaluation=evaluation,
                decision=decision,
            )

        # direct_answer 模式
        if action == "direct_answer":
            return AgentDecision(
                action="direct_answer",
                direct_response=data.get("direct_response", ""),
                plan=plan,
                thought=thought,
                evaluation=evaluation,
                decision=decision,
            )

        # call_tools 模式
        tools = []
        for t in data.get("tools", []):
            tools.append(ToolDecision(
                tool_name=t.get("tool_name", ""),
                arguments=t.get("arguments", {}),
                reason=thought,
            ))

        return AgentDecision(
            action="call_tools",
            tools=tools,
            execution_mode=data.get("execution_mode", "serial"),
            plan=plan,
            thought=thought,
            evaluation=evaluation,
            decision=decision,
        )
