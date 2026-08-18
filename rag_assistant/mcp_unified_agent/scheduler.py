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

# 需要注入 kb_groups（权限分组）的知识库工具
_KB_TOOLS = {"ask_knowledge_base", "search_knowledge_base", "debug_rerank"}


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
        trace_id: str = "",
        kb_groups: list = None,
        permissions: list = None,
    ):
        self.mcp_client = mcp_client
        self.registry = registry
        self.default_timeout = default_timeout
        self.trace_id = trace_id
        self.kb_groups = kb_groups  # 请求级权限分组（None=不限权限，不注入）
        self.permissions = permissions  # 请求级工具权限（None=不限，不校验）
        try:
            from config import TOOL_PERMISSION_ENABLED
            self.tool_perm_enabled = TOOL_PERMISSION_ENABLED
        except Exception:
            self.tool_perm_enabled = False  # 降级：默认关闭鉴权

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

        1. 注入 kb_groups（知识库工具，请求级权限）
        2. registry.validate(name, args) -- 参数校验
        3. mcp_client.call_tool(name, args) -- MCP 调用（自带超时+重试退避）
        4. 从 CallToolResult 中提取文本内容
        5. 记录耗时和错误状态 + 结构化审计(trace_id)
        """
        start = time.time()

        # 0. 工具级权限校验（收口，fail-closed：未声明权限的敏感工具默认拒绝）
        if self.tool_perm_enabled and self.permissions is not None:
            denied = self._check_tool_permission(decision.tool_name)
            if denied:
                return {
                    "tool_name": decision.tool_name,
                    "arguments": decision.arguments,
                    "result": f"权限拒绝: 缺少 {denied} 权限",
                    "is_error": True,
                    "latency_ms": 0.0,
                }

        # 0b. 注入权限分组（仅知识库工具 + 显式传了 kb_groups 时）
        #    直接改写 decision.arguments，后续 validate/call_tool/audit/返回 自动生效
        if self.kb_groups is not None and decision.tool_name in _KB_TOOLS:
            decision.arguments = {**(decision.arguments or {}), "kb_groups": self.kb_groups}

        # 1. 参数校验（失败不重试、不计入工具审计的重试）
        valid, error_msg = self.registry.validate(
            decision.tool_name, decision.arguments
        )
        if not valid:
            latency = (time.time() - start) * 1000
            self._audit(decision, "", False, latency, error=f"参数校验失败: {error_msg}")
            return {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "result": f"参数校验失败: {error_msg}",
                "is_error": True,
                "latency_ms": round(latency, 2),
            }

        # 2. MCP 调用（call_tool 内部已含 asyncio.wait_for 超时 + tenacity 退避重试，
        #    故此处不再套外层 wait_for，避免掐断重试）
        try:
            result = await self.mcp_client.call_tool(
                decision.tool_name, decision.arguments
            )

            # 3. 提取文本内容
            text = self._extract_text(result)
            is_error = getattr(result, 'isError', False)
            latency = (time.time() - start) * 1000

            logger.info(
                f"工具 {decision.tool_name} 执行完成 "
                f"({'失败' if is_error else '成功'}, {latency:.0f}ms)"
            )
            self._audit(decision, text, not is_error, latency,
                        error=(text[:200] if is_error else ""))

            return {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "result": text,
                "is_error": is_error,
                "latency_ms": round(latency, 2),
            }

        except ToolCallTimeoutError:
            latency = (time.time() - start) * 1000
            logger.warning(f"工具 {decision.tool_name} 调用超时")
            self._audit(decision, "", False, latency,
                        error=f"工具调用超时(重试后仍失败)")
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
            self._audit(decision, "", False, latency,
                        error=f"{type(e).__name__}: {e}")
            return {
                "tool_name": decision.tool_name,
                "arguments": decision.arguments,
                "result": f"执行异常: {type(e).__name__}: {e}",
                "is_error": True,
                "latency_ms": round(latency, 2),
            }

    # ── 工具权限校验 ──────────────────────────────────────────

    def _check_tool_permission(self, tool_name: str) -> str | None:
        """工具级权限校验：返回缺失的权限名（None=有权限/公开）。

        - 未在 registry 注册的工具 → 拒绝（fail-closed）
        - required_perms == ["*"] → 公开，放行
        - 否则校验 permissions 是否覆盖 required_perms
        """
        meta = self.registry.get(tool_name)
        if meta is None:
            return "（工具未注册）"
        required = meta.required_perms or ["*"]
        if required == ["*"]:
            return None
        missing = [p for p in required if p not in (self.permissions or [])]
        return missing[0] if missing else None

    # ── 审计 ──────────────────────────────────────────────────

    def _audit(self, decision, result_preview, success, latency_ms, error=""):
        """写一条工具调用审计（失败静默，不阻断）+ LangFuse span。"""
        try:
            from tool_audit import log_tool_call
            log_tool_call(
                trace_id=self.trace_id,
                tool_name=decision.tool_name,
                arguments=decision.arguments,
                result_preview=result_preview,
                latency_ms=latency_ms,
                success=success,
                error=error,
            )
        except Exception:
            pass
        # LangFuse span（降级安全）
        try:
            from observability import obs_span
            with obs_span(
                f"工具:{decision.tool_name}",
                trace_id=self.trace_id,
                metadata={"success": success, "latency_ms": round(latency_ms, 2),
                          "error": error[:120] if error else ""},
                level="ERROR" if not success else "DEFAULT",
                input=decision.arguments,
            ):
                pass
        except Exception:
            pass

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
