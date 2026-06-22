"""MCP 统一智能体 —— 主入口

完整流水线：
1. 工具预筛选    → 向量检索 top-N 候选工具
2. LLM 决策      → 构建 Prompt，LLM 输出结构化决策（direct_answer / call_tools）
3. MCP 调度执行  → 串行或并行调用 MCP 工具
4. 结果回填      → 工具结果注入上下文，回到步骤 2（最多 max_turns 轮）
5. 最终回答      → LLM 汇总所有工具结果，生成回答
6. 反思记忆      → 记录工具选择，供未来参考

运行模型：每次 chat() 使用 asyncio.run() 完成完整的
MCP 连接 → 流水线 → 断开连接周期，避免跨事件循环问题。
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

from openai import OpenAI
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters

from .mcp_client_manager import MCPSession
from .tool_registry import ToolRegistry
from .decision_engine import DecisionEngine
from .scheduler import Scheduler

# 可选模块
try:
    from .tool_vector_filter import ToolVectorFilter
    HAS_VECTOR_FILTER = True
except ImportError:
    HAS_VECTOR_FILTER = False

try:
    from .reflection_memory import ReflectionMemory, ReflectionEntry
    HAS_REFLECTION = True
except ImportError:
    HAS_REFLECTION = False

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='[UnifiedAgent] %(levelname)s %(message)s',
    stream=sys.stderr,
)


class UnifiedAgent:
    """MCP 统一智能体。

    每次 chat() 调用完成完整的 MCP 连接 → 流水线 → 断开周期。
    对话历史和反思记忆在多次调用间持久保留。
    """

    def __init__(self, config: dict | None = None):
        config = config or {}

        # 核心配置
        self.max_turns = config.get("max_turns", 5)
        self.call_timeout = config.get("call_timeout", 60.0)
        self.enable_vector_filter = config.get("enable_vector_filter", True)

        # MCP 客户端工厂（参数准备，每次 chat() 重新创建连接）
        self.server_dir = Path(__file__).resolve().parent.parent
        self.server_command = config.get("server_command") or self._detect_python()
        # 使用相对路径 "mcp_server.py"，因为 stdio_client 在 Windows 上
        # 处理绝对路径时可能有问题。父进程的 cwd 即 server_dir。
        self.server_args = config.get("server_args", ["mcp_server.py"])

        # LLM 客户端
        self._init_llm_client(config)

        # 持久状态（跨 chat() 调用保留）
        self._history: list[dict] = []       # 对话历史
        self._reflection: ReflectionMemory | None = None  # 反思记忆

        # 工具向量索引（懒加载，首次 chat() 时构建并缓存）
        self._tool_filter: ToolVectorFilter | None = None
        self._vector_filter_ready = False
        self._tool_registry_snapshot: ToolRegistry | None = None

        # 标记是否已完成首次初始化
        self._cold_start = True

    def _init_llm_client(self, config: dict) -> None:
        """初始化 LLM 客户端。"""
        try:
            from config import GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL
            self.model = config.get("model", LLM_MODEL)
            self._llm_client = OpenAI(
                api_key=GROQ_API_KEY,
                base_url=GROQ_BASE_URL,
                timeout=30.0,
            )
        except ImportError:
            import os
            from dotenv import load_dotenv
            load_dotenv()
            self.model = config.get("model", os.getenv("LLM_MODEL", "llama-3.3-70b-versatile"))
            self._llm_client = OpenAI(
                api_key=os.getenv("GROQ_API_KEY", ""),
                base_url=os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1"),
                timeout=30.0,
            )

    @staticmethod
    def _detect_python() -> str:
        """自动检测 Python 解释器：优先使用 venv。"""
        server_dir = Path(__file__).resolve().parent.parent
        candidates = [
            server_dir / "venv" / "Scripts" / "python.exe",
            server_dir / "venv" / "bin" / "python3",
            server_dir / ".venv" / "Scripts" / "python.exe",
            server_dir / ".venv" / "bin" / "python3",
        ]
        for p in candidates:
            if p.exists():
                logger.info(f"检测到虚拟环境: {p}")
                return str(p)
        return sys.executable

    # ── 同步入口 ──────────────────────────────────────────────

    def chat(self, user_input: str) -> str:
        """同步入口：兼容 Streamlit 等同步框架。

        每次调用完成完整的 MCP 连接 → 流水线 → 断开周期。
        """
        if not user_input or not user_input.strip():
            return "请输入您的问题。"

        try:
            return asyncio.run(self._run(user_input))
        except Exception as e:
            logger.error(f"chat 异常: {type(e).__name__}: {e}", exc_info=True)
            return f"处理请求时出现错误: {type(e).__name__}: {e}"

    # ── 主流程 ────────────────────────────────────────────────

    async def _run(self, user_input: str) -> str:
        """完整的一次对话流程。

        直接在方法内使用 async with 管理 stdio 子进程，
        避免嵌套 __aenter__ 在 Windows 上的兼容问题。
        """
        start_time = time.time()

        params = StdioServerParameters(
            command=self.server_command,
            args=self.server_args,
        )
        logger.info(f"MCP 启动: {self.server_command} {' '.join(self.server_args)}")

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                logger.info("MCP 握手完成")

                mcp = MCPSession(
                    session=session,
                    server_command=self.server_command,
                    server_args=self.server_args,
                    server_dir=str(self.server_dir),
                    call_timeout=self.call_timeout,
                )

                # 1. 首次调用：构建工具缓存和向量索引
                if self._cold_start:
                    await self._warm_up(mcp)

                # 2. 拉取最新工具列表
                mcp_tools = await mcp.list_tools()
                tool_registry = ToolRegistry()
                tool_registry.load(mcp_tools)

                if self._tool_registry_snapshot:
                    self._tool_registry_snapshot.load(mcp_tools)
                else:
                    self._tool_registry_snapshot = tool_registry

                # 3. 执行流水线
                decision_engine = DecisionEngine(self._llm_client, self.model)
                scheduler = Scheduler(mcp, tool_registry, self.call_timeout)

                answer = await self._pipeline(
                    user_input=user_input,
                    mcp_session=mcp,
                    tool_registry=tool_registry,
                    decision_engine=decision_engine,
                    scheduler=scheduler,
                )

                elapsed = (time.time() - start_time) * 1000
                logger.info(f"run 完成: {elapsed:.0f}ms")
                return answer

    async def _warm_up(self, mcp_client: MCPSession) -> None:
        """首次预热：拉取工具列表、构建向量索引、初始化反思记忆。"""
        logger.info("首次预热开始...")

        try:
            mcp_tools = await mcp_client.list_tools()

            # 缓存工具注册表快照
            self._tool_registry_snapshot = ToolRegistry()
            self._tool_registry_snapshot.load(mcp_tools)
            logger.info(f"已缓存 {self._tool_registry_snapshot.count} 个 MCP 工具")

            # 构建工具向量索引
            if HAS_VECTOR_FILTER and self.enable_vector_filter:
                try:
                    top_n = self._get_config_int("MCP_TOOL_TOP_N", 5)
                    self._tool_filter = ToolVectorFilter(top_n=top_n)
                    self._tool_filter.build_index(
                        self._tool_registry_snapshot.get_all()
                    )
                    self._vector_filter_ready = True
                    logger.info("工具向量索引构建完成")
                except Exception as e:
                    logger.warning(f"向量索引构建失败（跳过预筛选）: {e}")

            # 初始化反思记忆
            if HAS_REFLECTION and self._reflection is None:
                max_entries = self._get_config_int("MCP_REFLECTION_MAX", 50)
                self._reflection = ReflectionMemory(max_entries=max_entries)

        except Exception as e:
            logger.error(f"预热失败: {e}")
            raise

        self._cold_start = False
        logger.info("首次预热完成")

    def _get_config_int(self, key: str, default: int) -> int:
        """从 config.py 读取整数配置。"""
        try:
            import config
            return getattr(config, key, default)
        except ImportError:
            return default

    # ── 核心流水线 ────────────────────────────────────────────

    async def _pipeline(
        self,
        user_input: str,
        mcp_session: MCPSession,
        tool_registry: ToolRegistry,
        decision_engine: DecisionEngine,
        scheduler: Scheduler,
    ) -> str:
        """完整的异步流水线。

        for turn in range(max_turns):
            1. 工具预筛选
            2. LLM 决策
            3. 直接回答 → 返回
            4. 执行工具调用
            5. 记录反思
            6. 继续或汇总
        """
        all_results: list[dict] = []

        for turn in range(self.max_turns):
            logger.info(f"=== 第 {turn + 1}/{self.max_turns} 轮决策 ===")

            # 1. 工具预筛选
            if turn == 0 and self._vector_filter_ready and self._tool_filter:
                candidate_names = self._tool_filter.filter(user_input)
                candidate_tools = tool_registry.get_by_names(candidate_names)
                logger.info(f"向量预筛选: {len(candidate_tools)}/{tool_registry.count}")
            else:
                candidate_tools = tool_registry.get_all()

            # 2. 获取反思提示（含前序工具调用结果）
            hints = []
            if self._reflection:
                hints = self._reflection.get_relevant_hints(user_input)
            # 将前序轮次的工具结果注入提示，避免 LLM 不知情重复调用
            if all_results:
                for r in all_results[-5:]:  # 最近 5 条
                    status = "失败" if r.get("is_error") else "成功"
                    hints.append(
                        f"[本轮已执行] 工具「{r['tool_name']}」→ {status}: "
                        f"{str(r.get('result', ''))[:120]}"
                    )

            # 3. LLM 决策
            decision = decision_engine.decide(
                user_input=user_input,
                history=self._history,
                tools=candidate_tools,
                reflection_hints=hints,
            )

            logger.info(
                f"决策: action={decision.action}, "
                f"tools={[t.tool_name for t in decision.tools]}, "
                f"mode={decision.execution_mode}"
            )

            # 4. 直接回答 → 返回
            if decision.action == "direct_answer":
                answer = decision.direct_response or "（无法生成回答）"
                self._record_conversation(user_input, answer)
                return answer

            # 5. 执行工具调用
            if not decision.tools:
                break

            results = await scheduler.execute(
                decision.tools, decision.execution_mode
            )
            all_results.extend(results)

            # 6. 记录反思
            if self._reflection:
                for i, tool_dec in enumerate(decision.tools):
                    r = results[i] if i < len(results) else {}
                    self._reflection.record(ReflectionEntry(
                        query=user_input,
                        selected_tool=tool_dec.tool_name,
                        success=not r.get("is_error", True),
                        result_preview=str(r.get("result", ""))[:200],
                        latency_ms=r.get("latency_ms", 0),
                    ))

            # 单工具成功 → 直接返回结果（无需再调 final_answer）
            if len(decision.tools) == 1 and not results[0].get("is_error"):
                answer = results[0]["result"]
                self._record_conversation(user_input, answer)
                return answer

            # 工具失败 → 记录失败信息，下一轮 LLM 会看到 accumulated 结果
            failed_names = [
                r["tool_name"] for r in results
                if r.get("is_error")
            ]
            if failed_names:
                logger.warning(f"工具失败: {failed_names}，将在下一轮决策中提示 LLM")

        # 达到最大轮次：强制汇总
        logger.info(f"达到最大轮次，汇总 {len(all_results)} 条结果")
        if all_results:
            answer = decision_engine.final_answer(
                user_input=user_input,
                history=self._history,
                tool_results=all_results,
            )
        else:
            answer = "处理超时，未能完成工具调用。请简化问题重试。"

        self._record_conversation(user_input, answer)
        return answer

    # ── 对话记忆 ──────────────────────────────────────────────

    def _record_conversation(self, user_input: str, answer: str) -> None:
        """记录一轮对话到历史。"""
        self._history.append({"role": "user", "content": user_input})
        self._history.append({"role": "assistant", "content": answer})
        if len(self._history) > 20:
            self._history = self._history[-20:]

    def clear_memory(self) -> None:
        """清空对话记忆和反思记忆。"""
        self._history.clear()
        if self._reflection:
            self._reflection.clear()
        logger.info("对话记忆和反思记忆已清空")

    @property
    def memory(self):
        """向后兼容：提供 .memory 属性（app.py 需要 .memory.clear()）。"""
        return self
