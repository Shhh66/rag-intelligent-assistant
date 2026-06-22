"""工具调用调度器

支持串行和并行两种执行模式。

串行模式：工具按顺序执行，后续工具可感知前序工具结果
并行模式：无依赖工具通过 asyncio.gather 并发执行

每个工具调用都有独立超时和错误处理，单个失败不影响其他工具。
"""

import asyncio
import logging
import time

from .mcp_client_manager import MCPSession, ToolCallTimeoutError
from .tool_registry import ToolRegistry
from .decision_engine import ToolDecision

logger = logging.getLogger(__name__)


class Scheduler:
    """工具调用调度器：支持串行和并行两种执行模式。

    串行模式：工具按顺序执行，后续工具可引用前序工具结果（通过上下文注入）
    并行模式：无依赖工具通过 asyncio.gather 并发执行

    每个工具调用都有独立超时和错误处理，单个失败不影响其他工具。
    """

    def __init__(
        self,
        mcp_client: MCPSession,
        registry: ToolRegistry,
        default_timeout: float = 60.0,
    ):
        self.mcp_client = mcp_client
        self.registry = registry
        self.default_timeout = default_timeout

    async def execute(
        self,
        decisions: list[ToolDecision],
        mode: str,
    ) -> list[dict]:
        """执行工具调用列表，返回每个调用的结果字典。

        每个结果字典 =
        {"tool_name": ..., "arguments": ..., "result": ..., "is_error": ..., "latency_ms": ...}
        """
        if not decisions:
            return []

        if mode == "parallel":
            return await self._execute_parallel(decisions)
        else:
            return await self._execute_serial(decisions)

    # ── 串行执行 ──────────────────────────────────────────────

    async def _execute_serial(self, decisions: list[ToolDecision]) -> list[dict]:
        """顺序执行，前一个结果可被后续工具引用。

        通过将前序结果作为上下文追加到后续决策中实现依赖传递。
        """
        results = []
        for decision in decisions:
            result = await self._execute_one(decision)
            results.append(result)
        return results

    # ── 并行执行 ──────────────────────────────────────────────

    async def _execute_parallel(self, decisions: list[ToolDecision]) -> list[dict]:
        """并发执行：asyncio.gather(*tasks)，各自独立超时和错误处理。"""
        async def _safe_execute(decision: ToolDecision) -> dict:
            return await self._execute_one(decision)

        tasks = [_safe_execute(d) for d in decisions]
        results = await asyncio.gather(*tasks)
        return list(results)

    # ── 单工具执行 ────────────────────────────────────────────

    async def _execute_one(self, decision: ToolDecision) -> dict:
        """执行单个工具调用：

        1. registry.validate(name, args) -- 参数校验
        2. mcp_client.call_tool(name, args) -- MCP 调用
        3. 从 CallToolResult 中提取文本内容
        4. 记录耗时和错误状态
        """
        start = time.time()

        # 1. 参数校验
        valid, error_msg = self.registry.validate(
            decision.tool_name, decision.arguments
        )
        if not valid:
            latency = (time.time() - start) * 1000
            return {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "result": f"参数校验失败: {error_msg}",
                "is_error": True,
                "latency_ms": round(latency, 2),
            }

        # 2. MCP 调用
        try:
            result = await asyncio.wait_for(
                self.mcp_client.call_tool(
                    decision.tool_name, decision.arguments
                ),
                timeout=self.default_timeout,
            )

            # 3. 提取文本内容
            text = self._extract_text(result)
            is_error = getattr(result, 'isError', False)
            latency = (time.time() - start) * 1000

            logger.info(
                f"工具 {decision.tool_name} 执行完成 "
                f"({'失败' if is_error else '成功'}, {latency:.0f}ms)"
            )

            return {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "result": text,
                "is_error": is_error,
                "latency_ms": round(latency, 2),
            }

        except asyncio.TimeoutError:
            latency = (time.time() - start) * 1000
            logger.warning(f"工具 {decision.tool_name} 调用超时")
            return {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "result": f"工具调用超时 ({self.default_timeout}s)",
                "is_error": True,
                "latency_ms": round(latency, 2),
            }

        except Exception as e:
            latency = (time.time() - start) * 1000
            logger.error(f"工具 {decision.tool_name} 执行异常: {e}")
            return {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "result": f"执行异常: {type(e).__name__}: {e}",
                "is_error": True,
                "latency_ms": round(latency, 2),
            }

    # ── 结果提取 ──────────────────────────────────────────────

    def _extract_text(self, result) -> str:
        """从 CallToolResult.content 中提取文本。

        优先取 text/TextContent，级联取 structuredContent 或字符串化。
        """
        # 尝试从 content 列表提取
        if hasattr(result, 'content') and result.content:
            texts = []
            for item in result.content:
                if hasattr(item, 'text'):
                    texts.append(item.text)
                elif isinstance(item, str):
                    texts.append(item)
                elif hasattr(item, 'type') and item.type == 'text':
                    texts.append(getattr(item, 'text', str(item)))
                else:
                    texts.append(str(item))
            return "\n".join(texts) if texts else str(result)

        # 尝试 structuredContent
        if hasattr(result, 'structuredContent') and result.structuredContent:
            import json
            return json.dumps(result.structuredContent, ensure_ascii=False)

        # 级联字符串化
        return str(result)
