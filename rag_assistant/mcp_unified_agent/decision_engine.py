"""LLM 决策引擎

构建 Prompt → 调用 LLM → 解析结构化 JSON 决策。

负责将工具元数据、对话历史、反思记忆注入 Prompt，
然后解析 LLM 返回的 JSON 决策为 AgentDecision 对象。
"""

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from openai import OpenAI

from token_tracker import get_tracker

from .prompt_templates import (
    build_decision_prompt,
    build_final_answer_prompt,
    build_skill_recognition_prompt,
)
from .tool_registry import ToolMeta
from .skill_registry import SkillRegistry

logger = logging.getLogger(__name__)


@dataclass
class ToolDecision:
    """LLM 对单个工具的调用决策"""
    tool_name: str
    arguments: dict
    reason: str = ""


@dataclass
class AgentDecision:
    """LLM 的完整决策"""
    action: Literal["direct_answer", "call_tools", "call_skill"]
    tools: list[ToolDecision] = field(default_factory=list)
    execution_mode: str = "serial"
    direct_response: str | None = None
    skill_name: str = ""       # call_skill 时的 Skill 名
    skill_args: dict = field(default_factory=dict)  # call_skill 时的参数


@dataclass
class SkillMatchResult:
    """Skill 匹配 + 参数提取的完整结果"""
    skill_name: str
    confidence: float
    args: dict
    raw_response: str = ""


class DecisionEngine:
    """决策引擎：构建 Prompt → 调用 LLM → 解析结构化 JSON 决策。

    负责注入工具元数据、对话历史、反思记忆到 Prompt，
    然后解析 LLM 返回的 JSON 决策为 AgentDecision 对象。
    """

    def __init__(self, llm_client: OpenAI, model: str = "llama-3.3-70b-versatile"):
        self.client = llm_client
        self.model = model

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
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.1,
                max_tokens=4000,
            )
            raw_text = response.choices[0].message.content or ""
            get_tracker().record(self.model, response.usage, call_site="decision_engine.decide")
            logger.debug(f"LLM 决策原始输出: {raw_text[:300]}")
            return self._parse_decision(raw_text)

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
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.3,
                max_tokens=4000,
            )
            content = response.choices[0].message.content or "（无回答）"
            get_tracker().record(self.model, response.usage, call_site="decision_engine.final_answer")
            return content
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
            {"role": "system", "content": "你是一个精确的技能匹配引擎，只输出指定格式。"},
            {"role": "user", "content": prompt},
        ]

        for attempt in range(2):  # 解析失败允许重试 1 次
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=4000,
                )
                raw_text = response.choices[0].message.content or ""
                get_tracker().record(self.model, response.usage,
                                     call_site="decision_engine.match_skill")
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
                        "content": "格式错误，请严格按照 SKILL: <技能名>\\n参数: ```json {...}``` 格式输出"
                    })
            except Exception as e:
                logger.error(f"Skill 匹配 LLM 调用失败: {e}")
                break

        return None

    def _parse_skill_result(self, raw_text: str) -> SkillMatchResult | None:
        """解析 LLM 的 Skill 确认输出。"""
        if not raw_text or "none" in raw_text.lower().split("\n")[0]:
            return None

        # 提取 SKILL: <name>
        skill_match = re.search(r'SKILL:\s*(\S+)', raw_text)
        if not skill_match:
            return None
        skill_name = skill_match.group(1)
        if skill_name.lower() == "none":
            return None

        # 提取置信度
        conf_match = re.search(r'置信度:\s*([\d.]+)', raw_text)
        confidence = float(conf_match.group(1)) if conf_match else 0.7

        # 提取参数
        args = self._extract_json_block(raw_text) or {}

        logger.info(
            f"Skill 确认: {skill_name} (置信度={confidence:.2f}, "
            f"参数={list(args.keys())})"
        )
        return SkillMatchResult(
            skill_name=skill_name,
            confidence=confidence,
            args=args,
            raw_response=raw_text,
        )

    # ── ReAct 输出解析 ────────────────────────────────────────

    def decide_with_skills(
        self,
        user_input: str,
        history: list[dict],
        tools: list[ToolMeta],
        reflection_hints: list[str],
        skills_candidates: list[dict] | None = None,
    ) -> AgentDecision:
        """增强版决策：优先 Skill 匹配 + ReAct 推理。

        返回 AgentDecision，action 可能是 call_skill / call_tools / direct_answer。
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

        # 可用工具列表里也注入 Skill 名（让 ReAct 可以调 Skill）
        if skills_candidates:
            skill_tool_lines = ["\n## 可调用的技能（作为高级工具使用）"]
            for s in skills_candidates:
                skill_tool_lines.append(
                    f"### {s['name']}\n描述：{s['description']}\n"
                    f"参数：{', '.join(s.get('arg_slots', {}).keys())}"
                )
            tools_description += "\n".join(skill_tool_lines)

        prompt = build_decision_prompt(
            user_input=user_input,
            history=history,
            tools_description=tools_description,
            reflection_hints=reflection_hints,
            skills_description=skills_description,
        )

        messages = [
            {"role": "system", "content": "你是一个使用 ReAct 模式推理的智能助手决策引擎。"},
            {"role": "user", "content": prompt},
        ]

        for attempt in range(2):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=0.1,
                    max_tokens=4000,
                )
                raw_text = response.choices[0].message.content or ""
                get_tracker().record(self.model, response.usage,
                                     call_site="decision_engine.decide")
                logger.debug(f"ReAct 决策原始输出: {raw_text[:300]}")
                decision = self._parse_react_output(raw_text)
                if decision is not None:
                    return decision

                # 重试
                if attempt == 0:
                    messages.append({"role": "assistant", "content": raw_text})
                    messages.append({
                        "role": "user",
                        "content": "格式有误。请按指定格式输出：SKILL/Thought→Action/Final Answer。"
                    })
            except Exception as e:
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
        """解析 ReAct 格式输出（含 SKILL 指令）。

        支持格式：
        - SKILL: <name>\\n参数: ```json {...}```
        - Thought: ...\\nAction: <tool>\\n参数: ```json {...}```
        - {"action":"direct_answer","response":"..."}
        """
        if not raw_text:
            return None

        text = raw_text.strip()

        # 检测 SKILL 指令
        skill_match = re.search(r'SKILL:\s*(\S+)', text)
        if skill_match:
            skill_name = skill_match.group(1)
            args = self._extract_json_block(text) or {}
            logger.info(f"ReAct 输出: SKILL {skill_name}")
            return AgentDecision(
                action="call_skill",
                skill_name=skill_name,
                skill_args=args,
            )

        # 检测 Thought/Action（ReAct 格式）
        action_match = re.search(r'Action:\s*(\S+)', text)
        thought_match = re.search(r'Thought:\s*(.+?)(?:\n|$)', text)

        if action_match:
            tool_name = action_match.group(1)
            args = self._extract_json_block(text) or {}
            thought = thought_match.group(1).strip()[:200] if thought_match else ""

            return AgentDecision(
                action="call_tools",
                tools=[ToolDecision(
                    tool_name=tool_name,
                    arguments=args,
                    reason=thought,
                )],
                execution_mode="serial",
            )

        # 回退：尝试 JSON 解析
        return self._parse_decision(text)

    @staticmethod
    def _extract_json_block(text: str) -> dict | None:
        """从文本中提取 ```json ... ``` 代码块。"""
        match = re.search(r'```json\s*(.*?)\s*```', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return None
