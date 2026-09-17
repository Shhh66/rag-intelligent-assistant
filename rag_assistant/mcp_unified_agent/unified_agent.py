"""MCP 统一智能体 —— 主入口

完整流水线（v2：ReAct + Skills）：
0. Skill 匹配    → 关键词+向量双重匹配 Top3 候选
1. 工具预筛选    → 向量检索 top-N 候选工具
2. LLM 决策      → ReAct 推理（SKILL / Thought→Action→Obs / direct_answer）
3. 执行          → Skill 执行器 或 MCP 调度器
4. 结果回填      → 工具结果注入上下文，回到步骤 2（最多 max_turns 轮）
5. 最终回答      → LLM 汇总所有工具结果，生成回答

运行模型：每次 chat() 使用 asyncio.run() 完成完整的
MCP 连接 → 流水线 → 断开连接周期，避免跨事件循环问题。
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from token_tracker import get_tracker

from openai import OpenAI
from mcp.client.stdio import stdio_client
from mcp import ClientSession, StdioServerParameters

from .mcp_client_manager import MCPSession
from .circuit_breaker import CircuitBreakerError, call_llm_with_cb
from .prompt_templates import (
    build_session_summary_prompt,
    build_summary_verify_prompt,
    build_summary_retry_prompt,
)
from rate_limiter import RateLimitError
from .tool_registry import ToolRegistry
from .decision_engine import DecisionEngine, SkillMatchResult
from .scheduler import Scheduler
from .skill_registry import SkillRegistry
from .skill_executor import SkillExecutor, SkillExecutionError

# 可选模块
try:
    from .tool_vector_filter import ToolVectorFilter
    HAS_VECTOR_FILTER = True
except ImportError:
    HAS_VECTOR_FILTER = False

try:
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from long_term_memory import get_memory
    HAS_LONG_MEMORY = True
except Exception:
    HAS_LONG_MEMORY = False

logger = logging.getLogger(__name__)

logging.basicConfig(
    level=logging.INFO,
    format='[UnifiedAgent] %(levelname)s %(message)s',
    stream=sys.stderr,
)


class UnifiedAgent:
    """MCP 统一智能体。

    每次 chat() 调用完成完整的 MCP 连接 → 流水线 → 断开周期。
    对话历史与会话摘要在多次调用间持久保留。
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
        self._session_summary: str = ""      # 会话摘要（压缩早期对话，注入 system）
        self._skill_registry: SkillRegistry | None = None  # Skill 注册表
        self._user_id: str = "default"       # 当前用户身份（长期记忆隔离，请求级，不再读文件）
        # 会话标识：供 LangFuse 把多轮对话归入同一 session（清空记忆时换新）
        import uuid as _uuid
        self._session_id: str = _uuid.uuid4().hex

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

    def chat(self, user_input: str, kb_groups: list = None, user_id: str = None,
             permissions: list = None) -> str:
        """同步入口：兼容 Streamlit 等同步框架。

        每次调用完成完整的 MCP 连接 → 流水线 → 断开周期。
        kb_groups：请求级权限分组，注入知识库工具（None=不限权限）。
        user_id：当前用户身份，供长期记忆隔离（None=回落 default，不残留上次身份）。
        permissions：请求级工具权限（None=不限，不校验工具鉴权）。
        """
        if not user_input or not user_input.strip():
            return "请输入您的问题。"

        # 每次调用都显式设置，None 回落 default（避免残留上一个用户身份）
        self._user_id = user_id or "default"

        try:
            return asyncio.run(self._run(user_input, kb_groups, permissions))
        except CircuitBreakerError as e:
            # 熔断打开：下游模型持续故障，快速失败，不再进入 ReAct 循环反复重试
            logger.warning(f"下游模型熔断: {e}")
            return "下游模型服务暂时不可用（熔断保护中），请稍后重试。"
        except RateLimitError as e:
            # LLM 调用限流：频率超限，快速失败（日志带当前用户，便于排查是谁触发）
            logger.warning(f"LLM 调用限流 (user={self._user_id}): {e}")
            return "请求过于频繁，请稍后重试。"
        except Exception as e:
            logger.error(f"chat 异常: {type(e).__name__}: {e}", exc_info=True)
            return f"处理请求时出现错误: {type(e).__name__}: {e}"

    # ── 主流程 ────────────────────────────────────────────────

    async def _run(self, user_input: str, kb_groups: list = None,
                   permissions: list = None) -> str:
        """完整的一次对话流程。

        直接在方法内使用 async with 管理 stdio 子进程，
        避免嵌套 __aenter__ 在 Windows 上的兼容问题。
        """
        start_time = time.time()
        # 标记新一轮对话开始（不重置历史数据）
        get_tracker().start_conversation()

        # 生成本次对话的 trace_id：贯穿工具审计与 LangFuse 可观测，一次打通两边
        import uuid
        trace_id = uuid.uuid4().hex
        get_tracker().set_trace_id(trace_id)

        # 设置观测上下文（trace 名 / 用户 / 会话），供 LangFuse 按用户与会话检索归因
        try:
            from observability import set_obs_context
            set_obs_context(trace_id=trace_id, user_id=self._user_id,
                            session_id=self._session_id)
        except Exception:
            pass

        # 把 trace_id 经环境变量带进 MCP 子进程，使子进程内检索链路的 span
        # 也能并入同一条 trace（子进程 per-request 新建，拿不到主进程状态）。
        # 取不到默认环境就不注入（env=None），行为与改造前完全一致。
        try:
            from mcp.client.stdio import get_default_environment
            # ⚠️ 必须并入 os.environ：get_default_environment() 只返回 PATH/HOME 等
            #    系统级白名单变量，容器里注入的 REDIS_HOST / LANGFUSE_HOST 等业务变量
            #    不会被子进程继承，子进程会回落到 config 的 localhost 默认值
            #    （表现为子进程内 Redis 连不上、LangFuse 上报失败）。
            #    本地开发看不出来，因为本地默认值恰好就是 localhost。
            child_env = {
                **get_default_environment(), **os.environ,
                "MCP_TRACE_ID": trace_id,
                "MCP_USER_ID": self._user_id,
                "MCP_SESSION_ID": self._session_id,
            }
        except Exception:
            child_env = None
        params = StdioServerParameters(
            command=self.server_command,
            args=self.server_args,
            env=child_env,
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
                # 注入会话摘要（单条 system 的【历史摘要】块，压缩早期对话而来）
                decision_engine.session_summary = self._session_summary
                scheduler = Scheduler(mcp, tool_registry, self.call_timeout,
                                      trace_id=trace_id, kb_groups=kb_groups,
                                      permissions=permissions,
                                      user_id=self._current_user_id())

                answer = await self._pipeline(
                    user_input=user_input,
                    mcp_session=mcp,
                    tool_registry=tool_registry,
                    decision_engine=decision_engine,
                    scheduler=scheduler,
                )

                elapsed = (time.time() - start_time) * 1000
                # 记录本次对话的 Token 用量汇总
                summary = get_tracker().get_session_summary()
                logger.info(
                    f"run 完成: {elapsed:.0f}ms | "
                    f"Token: {summary['total_tokens']:,} "
                    f"(in={summary['total_input']:,}, out={summary['total_output']:,}) | "
                    f"费用: ¥{summary['total_cost']:.6f}"
                )
                # 给 trace 补顶层入参/出参，让 LangFuse 列表页一眼可见「问了什么/答了什么」。
                # 必须在 flush 之前 —— 否则这次 update 不在本批上报里。
                try:
                    from observability import update_trace_io
                    update_trace_io(trace_id, input=user_input, output=answer)
                except Exception:
                    pass
                # flush LangFuse 上报（降级安全）
                try:
                    from observability import flush_obs
                    flush_obs()
                except Exception:
                    pass
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

            # 初始化 Skill 注册表
            if self._skill_registry is None:
                self._skill_registry = SkillRegistry()
                skill_count = self._skill_registry.load_all()
                logger.info(f"Skill 注册表初始化完成: {skill_count} 个 Skill")

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

    def _format_collected_info(self, all_results: list[dict]) -> str:
        """格式化已收集的信息，用于 Judge 模型判断。

        Args:
            all_results: 所有工具执行结果

        Returns:
            格式化后的信息字符串
        """
        if not all_results:
            return "暂无收集到的信息"

        info_parts = []
        for i, result in enumerate(all_results, 1):
            tool_name = result.get("tool_name", "unknown")
            is_error = result.get("is_error", False)
            result_content = result.get("result", "")

            status = "失败" if is_error else "成功"
            # 截断过长的结果
            result_str = str(result_content)[:500]
            if len(str(result_content)) > 500:
                result_str += "..."

            info_parts.append(f"{i}. [{status}] {tool_name}: {result_str}")

        return "\n".join(info_parts)

    # ── 核心流水线 ────────────────────────────────────────────

    async def _pipeline(
        self,
        user_input: str,
        mcp_session: MCPSession,
        tool_registry: ToolRegistry,
        decision_engine: DecisionEngine,
        scheduler: Scheduler,
    ) -> str:
        """完整的异步流水线（v2：Skill 匹配 + ReAct 循环）。

        0. Skill 匹配（前置）→ 命中则 1 轮执行
        1-N. ReAct 循环 → Thought→Action→Obs → Final Answer
        """
        all_results: list[dict] = []

        # ═══ 步骤 0：Skill 匹配（前置，仅首轮） ═══
        if self._skill_registry and self._skill_registry.count > 0:
            candidates = self._skill_registry.match(user_input)
            if candidates:
                logger.info(
                    f"Skill 候选: "
                    + ", ".join(f"{c['skill']['name']}({c['score']:.2f})"
                                for c in candidates)
                )
                # LLM 确认 + 提取参数
                skill_result = decision_engine.match_skill(
                    user_input=user_input,
                    history=self._history,
                    candidate_skills=[c["skill"] for c in candidates],
                )
                if skill_result and skill_result.confidence >= 0.5:
                    skill = self._skill_registry.get(skill_result.skill_name)
                    if skill:
                        logger.info(
                            f"Skill 确认: {skill_result.skill_name} "
                            f"(置信度={skill_result.confidence:.2f}, "
                            f"参数={list(skill_result.args.keys())})"
                        )
                        try:
                            executor = SkillExecutor(
                                mcp_session,
                                step_timeout=self.call_timeout,
                                trace_id=scheduler.trace_id,
                                registry=tool_registry,
                                permissions=scheduler.permissions,
                                user_id=scheduler.user_id,
                            )
                            answer = await executor.execute(
                                skill, skill_result.args
                            )
                            # 记录执行日志
                            for log_entry in executor.get_logs():
                                logger.info(
                                    f"[SkillLog] {log_entry['event']}: "
                                    f"{log_entry['message']}"
                                )
                            self._record_conversation(user_input, answer)
                            return answer
                        except SkillExecutionError as e:
                            logger.warning(
                                f"Skill 执行失败，降级到 ReAct: {e}"
                            )
                            # 无感降级：继续执行下方 ReAct 循环
                else:
                    logger.info(
                        f"Skill 匹配未确认"
                        f"{f' (置信度={skill_result.confidence:.2f})' if skill_result else ''}"
                        f"，进入 ReAct 循环"
                    )

        # ═══ 步骤 1-N：ReAct 推理循环（五阶段版本） ═══

        # 五阶段状态
        current_plan = ""  # 首轮规划，后续轮次注入
        tool_call_signatures: list[str] = []  # 死循环检测：工具调用签名历史
        LOOP_DETECTION_THRESHOLD = 3  # 连续相同签名阈值

        # 成本控制：单任务 Token 预算（0=不限，>0 超预算强制终止返回中间结果）
        task_budget = self._get_config_int("TASK_TOKEN_BUDGET", 0)

        import hashlib

        def _tool_signature(tool_name: str, arguments: dict) -> str:
            """生成工具调用签名，用于死循环检测。"""
            sig_str = f"{tool_name}:{sorted(arguments.items())}"
            return hashlib.md5(sig_str.encode()).hexdigest()[:12]

        for turn in range(self.max_turns):
            logger.info(f"=== 第 {turn + 1}/{self.max_turns} 轮决策 ===")

            # 1. 工具预筛选
            if turn == 0 and self._vector_filter_ready and self._tool_filter:
                candidate_names = self._tool_filter.filter(user_input)
                candidate_tools = tool_registry.get_by_names(candidate_names)
                logger.info(f"向量预筛选: {len(candidate_tools)}/{tool_registry.count}")
            else:
                candidate_tools = tool_registry.get_all()

            # 2. 长期记忆检索注入（跨会话，按用户隔离）
            # 注：原先还有一路「历史工具选型」提示（内存态反思记忆），实测匹配精度
            # 不足（字符 bigram 分数分布完全重叠、误召漏召并存），已移除。
            # 长期记忆只存「从单轮问题里读不出来的用户信息」（画像/约束/项目背景），
            # 不存工具使用习惯——工具由当前问题决定，存了也用不上。详见
            # 技术文档/长期记忆.md 第十节。
            hints = []
            if HAS_LONG_MEMORY:
                try:
                    hints = list(get_memory().retrieve(self._current_user_id(), user_input))
                except Exception:
                    pass
            # 将前序轮次的工具结果注入提示，避免 LLM 不知情重复调用
            if all_results:
                for r in all_results[-5:]:  # 最近 5 条
                    status = "失败" if r.get("is_error") else "成功"
                    hints.append(
                        f"[本轮已执行] 工具「{r['tool_name']}」→ {status}: "
                        f"{str(r.get('result', ''))[:120]}"
                    )

            # 3. LLM 决策（五阶段：Plan → Thought → Action → Observation → Evaluation → Decision）
            # 不注入 Skill 候选：Skill 入口是上面的「前置匹配」，ReAct 循环内调不动 Skill
            # （action 枚举只有 call_tools/direct_answer，call_skill 分支不可达）。
            # 曾经把 Skill 当「高级工具」宣传给 LLM，导致它用 call_tools 调 Skill 名 → 必然失败。
            decision = decision_engine.decide_with_skills(
                user_input=user_input,
                history=self._history,
                tools=candidate_tools,
                reflection_hints=hints,
                skills_candidates=None,
                turn=turn,
                current_plan=current_plan,
            )

            # 记录首轮规划
            if turn == 0 and decision.plan:
                current_plan = decision.plan
                logger.info(f"规划: {current_plan[:200]}")

            logger.info(
                f"决策: action={decision.action}, "
                f"skill={decision.skill_name}, "
                f"tools={[t.tool_name for t in decision.tools]}, "
                f"mode={decision.execution_mode}, "
                f"decision={decision.decision}, "
                f"evaluation={decision.evaluation[:100] if decision.evaluation else ''}"
            )

            # 思考留痕：记录本轮 ReAct 决策（合规「每一步思考可回溯」）
            try:
                from tool_audit import log_decision
                log_decision(
                    trace_id=scheduler.trace_id,
                    turn=turn,
                    action=decision.action,
                    thought=decision.thought,
                    tool_names=[t.tool_name for t in decision.tools],
                    skill_name=decision.skill_name,
                    plan=decision.plan,
                    evaluation=decision.evaluation,
                    decision=decision.decision,
                    user_id=scheduler.user_id,
                )
            except Exception:
                pass

            # 4. 直接回答 → 返回（含 abort 场景）
            if decision.action == "direct_answer":
                answer = decision.direct_response or "（无法生成回答）"
                self._record_conversation(user_input, answer)
                return answer

            # 4b. 死循环检测：连续 N 次相同工具签名 → 强制终止
            if decision.tools:
                for t in decision.tools:
                    sig = _tool_signature(t.tool_name, t.arguments)
                    tool_call_signatures.append(sig)

                # 检查最近 N 个签名是否全部相同
                if len(tool_call_signatures) >= LOOP_DETECTION_THRESHOLD:
                    recent = tool_call_signatures[-LOOP_DETECTION_THRESHOLD:]
                    if len(set(recent)) == 1:
                        logger.warning(
                            f"死循环检测触发：连续 {LOOP_DETECTION_THRESHOLD} 次相同工具调用 "
                            f"({decision.tools[0].tool_name})"
                        )
                        answer = decision_engine.final_answer(
                            user_input=user_input,
                            history=self._history,
                            tool_results=all_results,
                        )
                        self._record_conversation(user_input, answer)
                        return answer

            # 4c. 调用 Skill（ReAct 循环内）→ 通过 SkillExecutor
            if decision.action == "call_skill" and decision.skill_name:
                skill = self._skill_registry.get(decision.skill_name) if self._skill_registry else None
                if skill:
                    try:
                        executor = SkillExecutor(mcp_session, step_timeout=self.call_timeout, trace_id=scheduler.trace_id,
                                                 registry=tool_registry, permissions=scheduler.permissions,
                                                 user_id=scheduler.user_id)
                        answer = await executor.execute(skill, decision.skill_args)
                        self._record_conversation(user_input, answer)
                        return answer
                    except SkillExecutionError as e:
                        logger.warning(f"ReAct 内 Skill 执行失败: {e}，继续下一轮")
                        hints.append(f"[本轮已执行] Skill「{decision.skill_name}」→ 失败: {str(e)[:120]}")
                        continue

            # 5. 执行工具调用
            if not decision.tools:
                break

            results = await scheduler.execute(
                decision.tools, decision.execution_mode
            )
            all_results.extend(results)

            # 6. 工具已执行完毕，结果进入下一轮上下文（见上方 all_results 收集）
            # ── 工具结果处理 ──
            success_count = sum(1 for r in results if not r.get("is_error"))
            all_failed = success_count == 0

            # 5b. 独立 Judge 模型判断信息是否足够
            # 位置说明：必须放在「记录反思」之后、`if not all_failed` 之前——
            # 否则 Judge 说 No 只会打一行日志，紧接着被「任一工具成功即返回」覆盖，
            # 判定形同虚设（位置错了，逻辑就等于没有）。
            from config import JUDGE_ENABLED
            if JUDGE_ENABLED:
                try:
                    from judge import get_judge
                    from observability import obs_span
                    judge = get_judge()

                    # 格式化已收集信息
                    collected_info = self._format_collected_info(all_results)

                    # LangFuse 记录 Judge span
                    with obs_span(
                        name="judge_evaluate",
                        trace_id=scheduler.trace_id,
                        metadata={
                            "round": turn,
                            "collected_info_length": len(collected_info),
                        },
                        input=user_input,
                    ):
                        judge_result = judge.evaluate(
                            task_goal=user_input,
                            collected_info=collected_info,
                            user_query=user_input,
                            round_idx=turn,
                        )

                    # 审计日志记录 Judge 结果
                    try:
                        from tool_audit import log_decision
                        log_decision(
                            trace_id=scheduler.trace_id,
                            turn=turn,
                            action="judge",
                            thought=f"Judge: {judge_result.decision}",
                            tool_names=[],
                            skill_name="",
                            plan=current_plan,
                            evaluation=judge_result.raw_output,
                            decision=judge_result.decision.lower(),
                            user_id=scheduler.user_id,
                        )
                    except Exception:
                        pass

                    if judge_result.is_sufficient:
                        logger.info(
                            f"[Judge] Round {turn}: 信息足够，直接生成答案"
                        )
                        answer = decision_engine.final_answer(
                            user_input=user_input,
                            history=self._history,
                            tool_results=all_results,
                        )
                        self._record_conversation(user_input, answer)
                        return answer

                    # 信息不足 → 真正进入下一轮继续收集
                    logger.info(f"[Judge] Round {turn}: 信息不足，继续下一轮")
                    continue
                except Exception as e:
                    # Judge 不可用 → 降级到原有逻辑（任一工具成功即汇总返回）
                    logger.warning(f"[Judge] 调用失败，降级到原有逻辑: {e}")

            if not all_failed:
                # 有至少一个工具成功：统一走 final_answer 汇总
                # （不再抄近路直接返回原始结果——query_weather 等工具的
                #  原始输出需要 LLM 加工才能变成用户可读的自然语言）
                if len(decision.tools) > 1:
                    logger.info(
                        f"{success_count}/{len(decision.tools)} 个工具成功，"
                        f"直接汇总"
                    )
                answer = decision_engine.final_answer(
                    user_input=user_input,
                    history=self._history,
                    tool_results=all_results,
                )
                self._record_conversation(user_input, answer)
                return answer

            # 工具失败 → 记录失败信息，下一轮 LLM 会看到 accumulated 结果
            failed_names = [
                r["tool_name"] for r in results
                if r.get("is_error")
            ]
            if failed_names:
                logger.warning(f"工具失败: {failed_names}，将在下一轮决策中提示 LLM")

            # 成本控制：单任务 Token 预算检查（超预算强制终止返回中间结果）
            if task_budget > 0:
                try:
                    from model_gateway import check_budget
                    if check_budget(task_budget):
                        logger.warning(
                            f"单任务 Token 预算耗尽（{task_budget}），强制终止"
                        )
                        break
                except Exception:
                    pass

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

    def _compress_history(self) -> bool:
        """把超出窗口的早期对话压缩成摘要，滚动更新 self._session_summary。

        成功：裁剪 self._history 只保留最近 SESSION_SUMMARY_KEEP_RECENT 条，返回 True。
        未启用 / 无可压缩内容 / 调用失败：不改动 history，返回 False（调用方回退硬截断）。

        滚动压缩：旧摘要作为输入参与下一轮压缩，保证摘要长度恒定、不随对话膨胀。
        """
        try:
            import config as _cfg
            enabled = getattr(_cfg, "SESSION_SUMMARY_ENABLED", True)
            keep = max(2, int(getattr(_cfg, "SESSION_SUMMARY_KEEP_RECENT", 10)))
            target = int(getattr(_cfg, "SESSION_SUMMARY_TARGET_TOKENS", 800))
            max_tokens = int(getattr(_cfg, "SESSION_SUMMARY_MAX_TOKENS", 2000))
            verify_enabled = getattr(_cfg, "SESSION_SUMMARY_VERIFY_ENABLED", True)
            verify_truncate = getattr(
                _cfg, "SESSION_SUMMARY_VERIFY_FALLBACK_TRUNCATE", False)
        except Exception:
            return False

        if not enabled:
            return False
        if len(self._history) <= keep:
            return False  # 没有可压缩的部分

        old_msgs = self._history[:-keep]
        recent_msgs = self._history[-keep:]

        # 格式化待压缩对话
        lines = []
        for msg in old_msgs:
            role = "用户" if msg.get("role") == "user" else "助手"
            lines.append(f"[{role}]: {str(msg.get('content', ''))[:500]}")
        dialogue = "\n".join(lines)

        prompt = build_session_summary_prompt(
            existing_summary=self._session_summary,
            dialogue=dialogue,
            target_tokens=target,
        )

        try:
            resp = call_llm_with_cb(
                self._llm_client, self.model,
                [{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=max_tokens,
                call_site="memory.compress",
            )
            summary = (resp.choices[0].message.content or "").strip()
            if not summary:
                logger.warning("会话摘要压缩返回空，回退硬截断")
                return False
        except Exception as e:
            logger.warning(f"会话摘要压缩失败（回退硬截断）: {e}")
            return False

        # 压缩后校验：比对原文，若摘要丢了关键信息 → 带缺失项定向重压一次
        if verify_enabled:
            missing = self._verify_summary(dialogue, summary)
            if missing:
                logger.info(f"摘要校验发现 {len(missing)} 项遗漏，定向重压")
                retried = self._retry_compress(dialogue, missing, target, max_tokens)
                if retried:
                    summary = retried
                elif verify_truncate:
                    logger.warning("摘要校验未通过且重压失败，回退硬截断")
                    return False
                else:
                    logger.warning("摘要校验未通过且重压失败，沿用原摘要（仍优于硬截断）")

        self._session_summary = summary
        self._history = recent_msgs
        logger.info(
            f"会话摘要压缩: {len(old_msgs)} 条 → 摘要 {len(summary)} 字"
            f"（保留最近 {len(recent_msgs)} 条原文）"
        )
        return True

    @staticmethod
    def _parse_verify_json(text: str):
        """从校验响应中提取 JSON 对象；无法解析返回 None。"""
        if not text:
            return None
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _verify_summary(self, dialogue: str, summary: str) -> list[str]:
        """校验摘要是否丢失原文关键信息。

        返回缺失项列表（供定向重压）；校验通过 / 无法判定 / 调用出错 一律返回 []。
        即「校验本身失败不作为压缩失败的依据」——避免校验环节反而阻断主链路。
        """
        try:
            import config as _cfg
            max_tokens = int(getattr(_cfg, "SESSION_SUMMARY_VERIFY_MAX_TOKENS", 512))
        except Exception:
            max_tokens = 512
        try:
            prompt = build_summary_verify_prompt(dialogue, summary)
            resp = call_llm_with_cb(
                self._llm_client, self.model,
                [{"role": "user", "content": prompt}],
                temperature=0, max_tokens=max_tokens,
                call_site="memory.verify",
            )
            text = (resp.choices[0].message.content or "").strip()
            data = self._parse_verify_json(text)
            if data is None:
                logger.debug("摘要校验结果无法解析，跳过校验（不阻断）")
                return []
            if data.get("ok") is True:
                return []
            return [str(m) for m in (data.get("missing") or []) if str(m).strip()]
        except Exception as e:
            logger.warning(f"摘要校验调用失败（跳过校验，不阻断）: {e}")
            return []

    def _retry_compress(self, dialogue: str, missing: list[str],
                        target: int, max_tokens: int) -> str:
        """定向重压：把校验发现的缺失项喂回，要求补全。失败返回空串。"""
        try:
            prompt = build_summary_retry_prompt(
                existing_summary=self._session_summary,
                dialogue=dialogue,
                missing=missing,
                target_tokens=target,
            )
            resp = call_llm_with_cb(
                self._llm_client, self.model,
                [{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=max_tokens,
                call_site="memory.compress_retry",
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as e:
            logger.warning(f"摘要定向重压失败: {e}")
            return ""

    def _record_conversation(self, user_input: str, answer: str) -> None:
        """记录一轮对话到历史。"""
        self._history.append({"role": "user", "content": user_input})
        self._history.append({"role": "assistant", "content": answer})
        # 历史上限由配置驱动：MAX_MEMORY_ROUNDS 轮 × 每轮 2 条消息（user+assistant）
        # max(2, ...) 兜底：避免配置为 0 时 Python 的 [-0:] 切片退化成「保留全部」
        max_msgs = max(2, self._get_config_int("MAX_MEMORY_ROUNDS", 10) * 2)
        compressed = False
        if len(self._history) > max_msgs:
            # 优先压缩（保留核心决策与结论）；压缩失败/未启用则回退硬截断（原行为）
            compressed = self._compress_history()
            if not compressed:
                self._history = self._history[-max_msgs:]
        # 长期记忆（跨会话，降级安全，不阻断）
        if HAS_LONG_MEMORY:
            try:
                if compressed:
                    # 摘要驱动：从「已过滤的摘要」抽取（每 N 轮一次，信噪比更高）
                    get_memory().extract_from_summary(
                        self._current_user_id(), self._session_summary
                    )
                else:
                    # 未压缩（开关关闭/失败/未达阈值）：保持原有「每轮从原始对话抽取」
                    get_memory().extract_and_store(
                        self._current_user_id(), user_input, answer
                    )
            except Exception:
                pass

    def _current_user_id(self) -> str:
        """返回当前用户身份（请求级，由 chat(user_id=...) 设置，不再读文件）。"""
        return self._user_id

    def clear_memory(self) -> None:
        """清空对话记忆（会话历史 + 会话摘要）。"""
        import uuid
        self._history.clear()
        self._session_summary = ""   # 会话摘要随对话历史一起清空
        self._session_id = uuid.uuid4().hex   # 换新会话标识：LangFuse 里两段对话分为两个 session
        logger.info("对话记忆已清空")

    @property
    def memory(self):
        """向后兼容：提供 .memory 属性（app.py 需要 .memory.clear()）。"""
        return self
