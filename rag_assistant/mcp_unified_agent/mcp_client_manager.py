"""MCP 客户端会话包装器

轻量级封装，委托给 MCP ClientSession。
实际的子进程生命周期由 unified_agent._run() 中的 async with 管理。
"""

import asyncio
import logging

from mcp import ClientSession

logger = logging.getLogger(__name__)


def _retry_config():
    """从 config 读取重试参数（安全降级到默认值）。"""
    try:
        import config
        return (
            getattr(config, "TOOL_RETRY_ENABLED", True),
            getattr(config, "TOOL_RETRY_MAX", 3),
            getattr(config, "TOOL_RETRY_BACKOFF_BASE", 0.5),
            getattr(config, "TOOL_RETRY_MAX_WAIT", 8.0),
        )
    except Exception:
        return True, 3, 0.5, 8.0


class MCPConnectionError(Exception):
    """MCP 连接相关异常"""
    pass


class ToolCallTimeoutError(Exception):
    """工具调用超时"""
    pass


class MCPSession:
    """MCP 会话包装器，委托给 ClientSession。

    不管理子进程生命周期——那由外层 async with stdio_client 管理。
    """

    def __init__(
        self,
        session: ClientSession,
        server_command: str = "",
        server_args: list | None = None,
        server_dir: str = "",
        call_timeout: float = 60.0,
    ):
        self._session = session
        self.server_command = server_command
        self.server_args = server_args or []
        self.server_dir = server_dir
        self.call_timeout = call_timeout
        self._tools_cache: list | None = None

    @property
    def is_connected(self) -> bool:
        return self._session is not None

    # ── 工具操作 ──────────────────────────────────────────────

    async def list_tools(self) -> list:
        """获取工具列表（带缓存）。"""
        if self._tools_cache is not None:
            return self._tools_cache

        result = await self._session.list_tools()
        self._tools_cache = list(result.tools)
        names = [t.name for t in self._tools_cache]
        logger.info(f"获取到 {len(self._tools_cache)} 个工具: {names}")
        return self._tools_cache

    async def call_tool(self, name: str, arguments: dict):
        """调用 MCP 工具（带超时 + 统一指数退避重试）。

        只重试瞬时故障（超时/连接错误）；工具业务错误(isError)由上层判断，不在此重试。
        重试全部失败后抛出最后一次异常，保持调用方原有 except 兜底不变。
        """
        enabled, max_attempts, backoff_base, max_wait = _retry_config()

        async def _once():
            try:
                result = await asyncio.wait_for(
                    self._session.call_tool(name, arguments),
                    timeout=self.call_timeout,
                )
                logger.info(f"调用: {name}({arguments}) → isError={result.isError}")
                return result
            except asyncio.TimeoutError:
                raise ToolCallTimeoutError(f"工具 {name} 超时 ({self.call_timeout}s)")

        if not enabled or max_attempts <= 1:
            return await _once()

        # 只对瞬时错误重试：超时、连接类异常
        from tenacity import (
            AsyncRetrying, stop_after_attempt, wait_exponential,
            retry_if_exception_type, RetryError,
        )
        transient = (ToolCallTimeoutError, ConnectionError, TimeoutError, OSError)
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(max_attempts),
                wait=wait_exponential(multiplier=backoff_base, max=max_wait),
                retry=retry_if_exception_type(transient),
                reraise=True,
            ):
                with attempt:
                    n = attempt.retry_state.attempt_number
                    if n > 1:
                        logger.warning(f"工具 {name} 第 {n}/{max_attempts} 次重试")
                    return await _once()
        except RetryError as e:  # reraise=True 下一般不会走到，兜底
            raise e.last_attempt.exception()

    async def refresh_tools(self) -> list:
        """强制刷新工具列表。"""
        self._tools_cache = None
        return await self.list_tools()

    async def check_tool_online(self, tool_name: str) -> bool:
        """检查指定工具是否在线。"""
        try:
            tools = await self.list_tools()
            return any(t.name == tool_name for t in tools)
        except Exception:
            return False
