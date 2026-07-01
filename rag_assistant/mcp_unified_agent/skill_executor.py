"""Skill 执行器

执行 Skill 的步骤序列：参数校验 → 槽位填充 → 工具调用 → 结果汇总。
支持并行/串行两种模式，含超时、重试、部分失败降级、max_depth 防护。
"""

import asyncio
import logging
import re
import time
from typing import Optional

from .mcp_client_manager import MCPSession

logger = logging.getLogger(__name__)

# 默认超时（秒）
DEFAULT_STEP_TIMEOUT = 30.0
DEFAULT_SKILL_TIMEOUT = 60.0
MAX_DEPTH = 2  # 防止无限递归


class SkillExecutionError(Exception):
    """Skill 执行失败（外部可捕获后降级 ReAct）。"""
    pass


class SkillExecutor:
    """Skill 执行器：参数校验 + 工具调用 + 结果汇总。

    每次执行返回 (answer: str, logs: list[dict])。
    执行失败抛出 SkillExecutionError，由上层降级到 ReAct。
    """

    def __init__(
        self,
        mcp_session: MCPSession,
        step_timeout: float = DEFAULT_STEP_TIMEOUT,
        skill_timeout: float = DEFAULT_SKILL_TIMEOUT,
        depth: int = 0,
    ):
        self.mcp = mcp_session
        self.step_timeout = step_timeout
        self.skill_timeout = skill_timeout
        self.depth = depth
        self._logs: list[dict] = []

    # ── 主入口 ────────────────────────────────────────────────

    async def execute(self, skill: dict, args: dict) -> str:
        """执行一个 Skill。

        Args:
            skill: Skill 定义字典
            args: 已填充的参数（如 {"city": "常州", "query": "什么是ML"}）

        Returns:
            str: 最终回答文本

        Raises:
            SkillExecutionError: 执行失败，上层应降级到 ReAct
        """
        # 深度防护
        if self.depth >= MAX_DEPTH:
            raise SkillExecutionError(f"Skill 执行深度超限 (max_depth={MAX_DEPTH})")

        start_time = time.time()
        skill_name = skill.get("name", "unknown")
        self._logs = []
        self._log("match", f"开始执行 Skill: {skill_name}", {"args": args})

        try:
            # 1. 参数校验
            self._validate_args(skill, args)

            # 2. 填充参数占位符 + 执行步骤
            steps = skill.get("steps", [])
            if not steps:
                raise SkillExecutionError("Skill 没有定义 steps")

            execution_mode = skill.get("execution_mode", "serial")

            if execution_mode == "parallel":
                results = await self._execute_parallel(steps, args)
            else:
                results = await self._execute_serial(steps, args)

            # 3. 构建最终回答
            answer = await self._build_final_answer(skill, args, results)

            elapsed = (time.time() - start_time) * 1000
            self._log("success", f"Skill 执行完成: {elapsed:.0f}ms",
                      {"elapsed_ms": elapsed})
            return answer

        except SkillExecutionError:
            raise
        except Exception as e:
            self._log("error", f"Skill 执行异常: {e}", {"error": str(e)})
            raise SkillExecutionError(f"Skill {skill_name} 执行失败: {e}")

    # ── 参数校验 ──────────────────────────────────────────────

    def _validate_args(self, skill: dict, args: dict):
        """校验参数是否符合 arg_slots 的 JSON Schema 约束。

        三种分支策略：
        1. 必填参数缺失 → 抛出 SkillExecutionError（上层反问用户）
        2. 参数格式/类型错误 → 同上
        3. 非必填参数缺失 → 使用默认值，不阻塞
        """
        arg_slots = skill.get("arg_slots", {})
        if not arg_slots:
            return  # 无参数定义，跳过校验

        for slot_name, slot_def in arg_slots.items():
            is_required = slot_def.get("required", False)
            expected_type = slot_def.get("type", "string")
            value = args.get(slot_name)

            if value is None or value == "":
                if is_required:
                    raise SkillExecutionError(
                        f"缺少必填参数「{slot_name}」"
                        f"（{slot_def.get('description', '')}），请反问用户补充"
                    )
                else:
                    # 非必填：跳过
                    continue

            # 类型校验
            if expected_type == "string" and not isinstance(value, str):
                raise SkillExecutionError(
                    f"参数「{slot_name}」类型错误：期望 string，实际 {type(value).__name__}"
                )
            elif expected_type == "number" and not isinstance(value, (int, float)):
                raise SkillExecutionError(
                    f"参数「{slot_name}」类型错误：期望 number"
                )

            # 枚举校验
            enum_values = slot_def.get("enum")
            if enum_values and value not in enum_values:
                raise SkillExecutionError(
                    f"参数「{slot_name}」值「{value}」不在允许范围内: {enum_values}"
                )

        self._log("validate", "参数校验通过", {"args": args})

    # ── 串行执行 ──────────────────────────────────────────────

    async def _execute_serial(self, steps: list, args: dict) -> list[dict]:
        """串行执行步骤，前一步失败则中断，返回已完成的步骤结果。"""
        results = []
        for i, step in enumerate(steps):
            result = await self._execute_step(step, args, i)
            results.append(result)
            if result.get("is_error"):
                self._log("partial", f"串行步骤 {i} 失败，中断执行，"
                          f"返回已完成 {len(results)} 步的部分结果")
                break
        return results

    # ── 并行执行 ──────────────────────────────────────────────

    async def _execute_parallel(self, steps: list, args: dict) -> list[dict]:
        """并行执行步骤，单步失败不影响其他步骤。"""
        tasks = [self._execute_step(step, args, i) for i, step in enumerate(steps)]
        results = await asyncio.gather(*tasks)
        return list(results)

    # ── 单步执行 ──────────────────────────────────────────────

    async def _execute_step(self, step: dict, args: dict, index: int) -> dict:
        """执行单个工具调用步骤（带超时、重试）。"""
        tool_name = step.get("tool", "unknown")
        step_args = step.get("args", {})
        is_retryable = step.get("retryable", True)
        is_critical = step.get("critical", True)

        # 填充参数占位符 {city} → args["city"]
        filled_args = self._fill_args(step_args, args)

        start = time.time()
        max_retries = 2 if is_retryable else 1

        for attempt in range(max_retries):
            try:
                result = await asyncio.wait_for(
                    self.mcp.call_tool(tool_name, filled_args),
                    timeout=self.step_timeout,
                )

                text = self._extract_text(result)
                is_error = getattr(result, 'isError', False)
                latency = (time.time() - start) * 1000

                if is_error:
                    raise SkillExecutionError(f"工具返回错误: {text[:200]}")

                self._log("step", f"步骤 {index}: {tool_name} 成功 ({latency:.0f}ms)",
                          {"tool": tool_name, "latency_ms": latency, "attempt": attempt + 1})

                return {
                    "tool_name": tool_name,
                    "arguments": filled_args,
                    "result": text,
                    "is_error": False,
                    "latency_ms": round(latency, 2),
                    "critical": is_critical,
                }

            except asyncio.TimeoutError:
                self._log("step_timeout", f"步骤 {index}: {tool_name} 超时 "
                          f"(attempt {attempt + 1}/{max_retries})")
                if attempt + 1 >= max_retries:
                    return self._error_result(tool_name, filled_args, is_critical,
                                              f"工具调用超时 ({self.step_timeout}s)")

            except SkillExecutionError as e:
                self._log("step_error", f"步骤 {index}: {tool_name} 失败 - {e}")
                if attempt + 1 >= max_retries:
                    return self._error_result(tool_name, filled_args, is_critical, str(e))

            except Exception as e:
                self._log("step_error", f"步骤 {index}: {tool_name} 异常 - {e}")
                if not is_retryable or attempt + 1 >= max_retries:
                    return self._error_result(tool_name, filled_args, is_critical,
                                              f"{type(e).__name__}: {e}")

        # 不应到达这里
        return self._error_result(tool_name, filled_args, is_critical, "未知错误")

    # ── 参数填充 ──────────────────────────────────────────────

    def _fill_args(self, template: dict, args: dict) -> dict:
        """将模板中的 {city} 占位符替换为实际参数值。"""
        filled = {}
        for key, value in template.items():
            if isinstance(value, str):
                # 替换 {param_name} 占位符
                def replacer(m):
                    param_name = m.group(1)
                    return str(args.get(param_name, m.group(0)))
                filled[key] = re.sub(r'\{(\w+)\}', replacer, value)
            else:
                filled[key] = value
        return filled

    # ── 结果汇总 ──────────────────────────────────────────────

    async def _build_final_answer(
        self, skill: dict, args: dict, results: list[dict]
    ) -> str:
        """汇总 Skill 执行结果，生成最终回答。

        - 有 post_process + final_prompt → LLM 用自定义 Prompt 加工
        - 其他情况 → 默认调用通用 LLM 汇总（确保不会返回原始工具数据给用户）
        """
        # 检查关键步骤
        critical_failures = [
            r for r in results
            if r.get("is_error") and r.get("critical", True)
        ]
        success_results = [r for r in results if not r.get("is_error")]

        if not success_results:
            raise SkillExecutionError(
                f"所有工具调用均失败: "
                + "; ".join(r.get("result", "")[:80] for r in results)
            )

        # 有自定义 final_prompt → 个性化加工
        if skill.get("post_process"):
            return await self._post_process(skill, args, results)

        # 默认：始终走 LLM 通用汇总，确保不返回原始数据
        return await self._default_summarize(skill, args, results, critical_failures)

    async def _post_process(self, skill: dict, args: dict, results: list[dict]) -> str:
        """用 LLM 对工具结果进行后置加工（如穿衣建议）。"""
        from openai import OpenAI
        from config import GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL
        from token_tracker import get_tracker

        # 拼接原始结果
        result_parts = []
        for r in results:
            if not r.get("is_error"):
                result_parts.append(f"[{r['tool_name']}]\n{r['result']}")
        raw_results = "\n\n".join(result_parts)

        prompt = skill.get("final_prompt", "基于以下信息，回答用户问题：\n{results}")
        prompt = prompt.replace("{results}", raw_results)

        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=30.0)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=800,
        )

        get_tracker().record(LLM_MODEL, response.usage, call_site="skill_executor.post_process")
        return response.choices[0].message.content or "（无法生成建议）"

    async def _default_summarize(
        self, skill: dict, args: dict, results: list[dict],
        critical_failures: list[dict],
    ) -> str:
        """默认 LLM 汇总：Skill 未配置 post_process 时的兜底。

        确保任何 Skill 执行后都不会直接返回原始工具数据给用户。
        """
        from openai import OpenAI
        from config import GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL
        from token_tracker import get_tracker

        # 拼接成功结果
        parts = []
        for r in results:
            if not r.get("is_error"):
                parts.append(f"### [{r['tool_name']}]\n{r['result']}")
        raw_results = "\n\n".join(parts)

        # 拼接用户问题的参数上下文
        user_intent = ", ".join(f"{k}={v}" for k, v in args.items())

        prompt = (
            f"你是一个智能助手。基于以下工具返回的信息，回答用户的问题。\n\n"
            f"## 用户意图\n{user_intent}\n\n"
            f"## 工具返回结果\n{raw_results}\n\n"
            f"## 回答要求\n"
            f"1. 用自然流畅的语言综合所有工具结果来回答用户\n"
            f"2. 如果用户问了生活类问题（如穿什么、带伞等），"
            f"请基于数据给出具体建议\n"
            f"3. 如果用户问了知识类问题，请准确回答\n"
        )

        # 如果有关键步骤失败，追加提示
        if critical_failures:
            failed_names = [r["tool_name"] for r in critical_failures]
            prompt += (
                f"\n4. 注意：以下工具查询失败，请在回答中简要说明："
                f"{', '.join(failed_names)}"
            )

        client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=30.0)
        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.5,
            max_tokens=1200,
        )

        get_tracker().record(LLM_MODEL, response.usage,
                             call_site="skill_executor.default_summarize")
        return response.choices[0].message.content or "（无法生成回答）"

    # ── 工具方法 ──────────────────────────────────────────────

    def _error_result(self, tool: str, args: dict, critical: bool, msg: str) -> dict:
        return {
            "tool_name": tool,
            "arguments": args,
            "result": msg,
            "is_error": True,
            "latency_ms": 0,
            "critical": critical,
        }

    def _extract_text(self, result) -> str:
        """从 CallToolResult.content 中提取文本。"""
        if hasattr(result, 'content') and result.content:
            texts = []
            for item in result.content:
                if hasattr(item, 'text'):
                    texts.append(item.text)
                elif isinstance(item, str):
                    texts.append(item)
                else:
                    texts.append(str(item))
            return "\n".join(texts) if texts else str(result)
        return str(result)

    def _log(self, event: str, message: str, detail: dict = None):
        """基础可观测性埋点。"""
        entry = {
            "event": event,
            "message": message,
            "detail": detail or {},
            "depth": self.depth,
        }
        self._logs.append(entry)
        level = {"error": logging.WARNING, "step_timeout": logging.WARNING}.get(
            event, logging.INFO
        )
        logger.log(level, f"[SkillExecutor] {message}")

    def get_logs(self) -> list[dict]:
        """获取执行日志（供上层埋点使用）。"""
        return list(self._logs)


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== Skill Executor 自测 ===\n")

    # 1. 测试参数校验
    from skills import discover_skills
    discover_skills(reload=True)
    from mcp_unified_agent.skill_registry import SkillRegistry
    registry = SkillRegistry()
    registry.load_all()

    skill = registry.get("comprehensive_query")
    print(f"📋 测试 Skill: {skill['name']}\n")

    # 2. 测试参数填充
    class DummyExecutor:
        def _fill_args(self, template, args):
            return SkillExecutor.__init__.__code__  # placeholder
    # 直接测试 _fill_args
    executor = SkillExecutor.__new__(SkillExecutor)
    executor.step_timeout = 30.0
    executor.skill_timeout = 60.0
    executor.depth = 0
    executor._logs = []

    filled = executor._fill_args(
        {"city": "{city}", "query": "{query}"},
        {"city": "常州", "query": "什么是机器学习"}
    )
    print(f"   参数填充测试: {filled}")

    # 3. 测试参数校验
    try:
        executor._validate_args(skill, {"city": "常州"})
        print(f"   ❌ 应抛出异常（缺少必填 query）")
    except SkillExecutionError as e:
        print(f"   ✅ 正确捕获: {e}")

    try:
        executor._validate_args(skill, {"city": "常州", "query": "test"})
        print(f"   ✅ 参数齐全，校验通过")
    except SkillExecutionError as e:
        print(f"   ❌ 不应失败: {e}")

    print("\n🎉 Skill Executor 逻辑自测完成！")
