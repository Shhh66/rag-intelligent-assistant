"""
独立 Judge 模型模块

职责：判断当前已收集的信息是否足够回答用户问题
输出：Yes（信息足够）/ No（信息不足）

设计原则：
- 职责单一：只判断，不推理
- 轻量调用：只需输出 1 个 token
- 降级安全：失败时回退到主 LLM Decision 判断
"""

import logging
from typing import Literal

from openai import OpenAI

from mcp_unified_agent.circuit_breaker import call_llm_with_cb

logger = logging.getLogger(__name__)

# Judge Prompt 模板
JUDGE_PROMPT_TEMPLATE = """#【任务目标】
{task_goal}

#【已收集信息】
{collected_info}

#【用户问题】
{user_query}

#【判断要求】
当前已收集的信息是否足够完整地回答用户问题？

#【输出格式】
只输出一个单词：Yes 或 No
- Yes：信息足够，可以生成最终答案
- No：信息不足，需要继续收集"""


class JudgeResult:
    """Judge 模型返回结果"""

    def __init__(
        self,
        decision: Literal["Yes", "No", "error"],
        raw_output: str = "",
        error_msg: str = "",
        round_idx: int = 0,
    ):
        self.decision = decision
        self.raw_output = raw_output
        self.error_msg = error_msg
        self.round_idx = round_idx

    @property
    def is_sufficient(self) -> bool:
        """信息是否足够"""
        return self.decision == "Yes"

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "raw_output": self.raw_output,
            "error_msg": self.error_msg,
            "round_idx": self.round_idx,
        }


class JudgeModel:
    """
    独立 Judge 模型

    职责：判断已收集信息是否足够回答用户问题
    """

    def __init__(self, client: OpenAI | None = None):
        from config import GROQ_API_KEY, GROQ_BASE_URL, JUDGE_MODEL, JUDGE_MAX_TOKENS, JUDGE_TEMPERATURE, JUDGE_TIMEOUT
        self.client = client or OpenAI(
            api_key=GROQ_API_KEY,
            base_url=GROQ_BASE_URL,
        )
        self.model = JUDGE_MODEL
        self.max_tokens = JUDGE_MAX_TOKENS
        self.temperature = JUDGE_TEMPERATURE
        self.timeout = JUDGE_TIMEOUT

    def build_prompt(
        self,
        task_goal: str,
        collected_info: str,
        user_query: str,
    ) -> str:
        """构建 Judge Prompt"""
        return JUDGE_PROMPT_TEMPLATE.format(
            task_goal=task_goal,
            collected_info=collected_info or "暂无收集到的信息",
            user_query=user_query,
        )

    def evaluate(
        self,
        task_goal: str,
        collected_info: str,
        user_query: str,
        round_idx: int = 0,
    ) -> JudgeResult:
        """
        调用 Judge 模型判断信息是否足够

        Args:
            task_goal: 任务目标（通常是用户原始问题）
            collected_info: 已收集的信息（Observation 汇总）
            user_query: 用户原始问题
            round_idx: 当前轮次（用于日志）

        Returns:
            JudgeResult: 包含 Yes/No/error 的判断结果
        """
        prompt = self.build_prompt(task_goal, collected_info, user_query)

        try:
            response = call_llm_with_cb(
                client=self.client,
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                call_site="judge.evaluate",
            )

            raw_output = response.choices[0].message.content.strip()

            # 解析输出
            decision = self._parse_output(raw_output)

            logger.info(
                f"[Judge] Round {round_idx}: {decision} "
                f"(raw='{raw_output[:50]}')"
            )

            return JudgeResult(
                decision=decision,
                raw_output=raw_output,
                round_idx=round_idx,
            )

        except Exception as e:
            logger.warning(f"[Judge] 调用失败，降级到主 LLM Decision: {e}")
            return JudgeResult(
                decision="error",
                error_msg=str(e),
                round_idx=round_idx,
            )

    def _parse_output(self, raw_output: str) -> Literal["Yes", "No", "error"]:
        """
        解析 Judge 输出

        容错处理：
        1. 精确匹配 Yes/No
        2. 忽略大小写和空白
        3. 包含 Yes/No 关键词即可
        """
        cleaned = raw_output.strip().lower()

        # 精确匹配
        if cleaned == "yes":
            return "Yes"
        if cleaned == "no":
            return "No"

        # 模糊匹配（包含关键词）
        if "yes" in cleaned:
            return "Yes"
        if "no" in cleaned:
            return "No"

        # 无法解析
        logger.warning(f"[Judge] 无法解析输出: '{raw_output}'")
        return "error"


# 全局单例
_judge_instance: JudgeModel | None = None


def get_judge() -> JudgeModel:
    """获取 Judge 模型单例"""
    global _judge_instance
    if _judge_instance is None:
        _judge_instance = JudgeModel()
    return _judge_instance
