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
)
from .tool_registry import ToolMeta

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
    action: Literal["direct_answer", "call_tools"]
    tools: list[ToolDecision] = field(default_factory=list)
    execution_mode: str = "serial"
    direct_response: str | None = None


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
                max_tokens=600,
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
                max_tokens=800,
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
