# MCP 工程化计划（权限控制 · 错误重试 · 调用审计）

> **实现状态（2026-07）**：✅ **错误重试 + 调用审计 + trace_id 已实现并验证**；⏳ 工具级权限 + 统一元数据留待下轮。
> 定位：这是当前架构**最真实的短板**——工具调用层"裸注册、无权限、重试不统一、审计仅 stderr"，补齐后直接提升生产可靠性与安全性。

## 已实现部分（本轮）

- **统一重试退避**：`mcp_client_manager.py::call_tool` 用 tenacity 包裹，指数退避（`TOOL_RETRY_MAX/BACKOFF_BASE/MAX_WAIT`），只重试瞬时故障（超时/连接）；`scheduler` 与 `skill_executor` 两条路径**共用此层自动统一**。实测：0.3s 超时工具触发 3 次尝试、退避 0.5+1s、总耗时 2.5s。
- **结构化审计**：新增 `tool_audit.py`，工具调用落 `tool_audit.jsonl`（trace_id/工具/入参脱敏/结果摘要/耗时/成败/retry_count）；`scheduler._execute_one` 与 `skill_executor._execute_step`/`_error_result` 两路径均埋点。
- **trace_id 贯穿**：`unified_agent._run()` 生成 `uuid4().hex`，透传 Scheduler/SkillExecutor + `token_tracker.set_trace_id()`，一问多工具归并同一 trace。实测知识库问题的 `search_knowledge_base`+`ask_knowledge_base` 两次调用共享同一 trace_id。
- 顺带去掉 `scheduler._execute_one` 外层 `wait_for`（超时+重试已下沉到 `call_tool`，避免掐断重试）。

---

## 一、现状分析

| 能力 | 现状 | 证据 |
|------|------|------|
| 工具级权限 | **无**。任何调用方可调任何工具 | `mcp_server.py` 6 个 `@mcp.tool()` 裸注册，工具内不校验身份 |
| 错误重试 | **不统一**。主路径无重试 | `scheduler.py::_execute_one` 一次即返回 `is_error`；`skill_executor.py` 有 `max_retries=2` 但**无退避** |
| 调用审计 | **仅 stderr**。不落盘、无 trace_id | `mcp_client_manager.py:70` 只 `logger.info(...)`，工具 IO 不持久化 |
| 权限体系 | 文档级 RBAC，**不覆盖工具** | `api_server.py`+`permission.db` 保护 REST 端点/知识库检索；`KB_PERMISSION_ENABLED` 是**死配置**（无代码读取） |

**核心缺口**：现有 JWT 已签发 `permissions`（如 `search_all/upload/manage_users`）与 `kb_groups`，但这套身份信息**从未传导到工具调用层**。工具调用完全无鉴权、无统一容错、无可追溯记录。

---

## 二、目标能力

1. **工具级权限校验**：某用户/角色只能调用被授权的工具（如访客禁用 `clear_memory`、未授权用户禁用某业务工具）。
2. **统一重试 + 指数退避**：所有 MCP 工具调用走同一条容错通道，瞬时故障（超时/网络抖动）自动退避重试，主路径与 Skill 路径行为一致。
3. **结构化调用审计**：每次工具调用落盘一条含 `trace_id / 调用者 / 工具名 / 入参 / 结果摘要 / 耗时 / 成败` 的结构化记录，可追溯、可统计。

---

## 三、落地设计（引用现有代码）

### 3.1 工具级权限校验

- **权限来源复用现有 JWT**：`api_server.py::login()` 已在 token 里放 `permissions`。登录后除 `set_current_kb_groups()`（`app.py:151`）外，**新增 `set_current_tool_permissions()`**，同样写入跨进程共享文件（仿 `kb_permission_context.json` 模式），MCP 子进程读取。
- **校验钩子位置**：在 `scheduler.py::_execute_one`（工具调用唯一收口）调用前插入 `_check_tool_permission(tool_name, caller_perms)`，无权限直接返回结构化拒绝（不抛异常，走现有 `is_error` 通道）。
- **工具→权限映射表**：新建配置（config.py 或 json），如 `{"clear_memory": ["manage_users"], "query_weather": ["*"]}`，`*` 表示公开。默认放开（兼容现状），开关 `TOOL_PERMISSION_ENABLED` 控制。
- **顺带修死配置**：让 `KB_PERMISSION_ENABLED` 真正接线（当前定义了却无人读取），或明确移除，消除误导。

### 3.2 统一重试 + 指数退避

- **引入 `tenacity`**（轻量、成熟）。在 `mcp_client_manager.py::call_tool` 包一层：`@retry(stop=stop_after_attempt(N), wait=wait_exponential(...), retry=retry_if_exception_type((ToolCallTimeoutError, ...)))`。
- **只重试瞬时错误**：超时、连接错误可重试；参数校验失败、权限拒绝**不重试**（快速失败）。
- **收敛两套重试**：`skill_executor.py` 现有 `for attempt in range(max_retries)` 改为复用同一退避策略，主路径（scheduler）与 Skill 路径行为统一。
- **配置**：`TOOL_RETRY_MAX=3`、`TOOL_RETRY_BACKOFF_BASE=0.5`、`TOOL_RETRY_MAX_WAIT=8`（config.py）。

### 3.3 结构化调用审计

- **新建 `tool_audit.py`**：`AuditLogger.log(trace_id, caller, tool, args, result_preview, latency_ms, success, error)`，落盘 `tool_audit.jsonl`（仿 `token_tracker._persist_record` 的实时追加模式），或复用 `permission.db` 加 `tool_audit` 表。
- **trace_id 贯穿一次 chat**：在 `unified_agent.py::chat()` 生成一个 `trace_id`，透传到 `scheduler` 每次工具调用，使一问多工具可归并为一条链路（也为后续 LangFuse 可观测性打基础，见 `可观测性LangFuse.md`）。
- **埋点位置**：`scheduler.py::_execute_one` 成功/失败分支各记一条；入参脱敏（长文本截断、敏感字段掩码）。

---

## 四、配置项设计

```python
# config.py 新增
TOOL_PERMISSION_ENABLED = False      # 工具级权限总开关（默认关，兼容现状）
TOOL_PERMISSION_MAP = {              # 工具 → 所需权限；"*" = 公开
    "clear_memory": ["manage_users"],
    "*": ["*"],
}
TOOL_RETRY_MAX = 3                   # 工具调用最大重试次数
TOOL_RETRY_BACKOFF_BASE = 0.5        # 指数退避基数(秒)
TOOL_RETRY_MAX_WAIT = 8              # 单次最大等待(秒)
TOOL_AUDIT_ENABLED = True            # 调用审计开关
TOOL_AUDIT_PATH = "./tool_audit.jsonl"
```

> 注：`TOOL_PERMISSION_MAP` 是初版简易方案；推荐按 **8.1 统一工具元数据体系**把权限/重试/脱敏收敛到 `ToolMeta` 的 `required_perms/retryable/sensitive_args` 字段，避免多处配置各自为政。

---

## 五、与现有体系联动

- **权限**：复用 `api_server.py` 的 JWT `permissions` 与跨进程共享文件模式（`kb_permission_context.json` 已验证可行），无需新鉴权体系。
- **审计**：与 `token_tracker`（成本）、`evaluation.py`（问答日志）形成"成本 / 质量 / 调用"三条可观测数据线；`trace_id` 为后续 LangFuse 串联做铺垫。
- **重试**：与 `skill_executor` 现有重试收敛为一套；与"改写/混合/重排全程可降级"的项目降级哲学一致。

---

## 六、优先级与代价

- **优先级**：★★★★★ 三项里**审计**最易落地（纯增量、零风险）、**重试**次之（tenacity 包一层）、**权限**稍重（需打通跨进程身份传递）。建议顺序：审计 → 重试 → 权限。
- **代价**：新增 `tenacity` 依赖（装 D 盘）；权限需前端登录态传导到 MCP 子进程；审计文件需配轮转/清理策略。
- **收益**：生产级可靠性 + 安全边界 + 可追溯性，是"MCP 协议工程化"最直接的简历落点。

---

## 七、风险与注意

- **跨进程身份传递**：MCP 子进程与主进程分离，工具权限上下文须走共享文件/环境变量，注意并发与时效（仿现有权限文件，但要处理多用户并发场景）。
- **重试放大副作用**：非幂等工具（如写操作）重试需谨慎；本项目工具目前多为只读检索，风险低，但审计/权限工具须标注 `no_retry`。
- **审计文件膨胀**：`tool_audit.jsonl` 需按大小/日期轮转，避免无限增长（可复用 token_log 的思路）。
- **默认关闭**：权限开关默认 `False`，避免误伤现有 Streamlit 单用户流程；灰度验证后再开。

---

## 八、低成本高收益补充

以下三项与现有设计对齐，几乎零额外成本，却能显著拔高设计的系统性。

### 8.1 统一工具元数据体系（收敛三处逻辑到一个来源）

现状：`skill_executor.py` 已有 `retryable` 步骤标记的雏形，但工具属性散落各处。建议在**工具注册层**（`tool_registry.ToolMeta`，已存在且预留了 `source_server` 扩展位）统一挂载工具级元数据：

```python
# ToolMeta 扩展字段（示意）
retryable: bool = False          # 是否允许重试（瞬时故障）
idempotent: bool = True          # 是否幂等（写操作设 False，重试须谨慎）
sensitive_args: list = []        # 审计脱敏字段名（如 ["api_key","content"]）
required_perms: list = ["*"]     # 所需权限（替代散落的 TOOL_PERMISSION_MAP）
```

**三处逻辑统一读它**：重试(3.2)读 `retryable/idempotent`、审计脱敏(3.3)读 `sensitive_args`、权限校验(3.1)读 `required_perms`。

> **价值**：从"三套散落配置"收敛为"一个工具属性来源"，形成**统一的工具元数据管控体系**——这是比"分别加三个功能"更高阶的设计叙事，且与现有 `ToolMeta`/`skill retryable` 一脉相承，不是另起炉灶。

### 8.2 权限最小化默认原则（防未来漏配）

当前"默认全放开"兼容现状，但新增工具时**忘配权限 = 安全漏洞**。补一条兜底规则：

- **查询类工具**（只读，`idempotent=True`）→ 默认公开
- **写操作/敏感工具**（`idempotent=False` 或标记 `sensitive`）→ **默认需授权**（未显式配置即拒绝）

即"未知工具按危险处理"。同时 `required_perms` 预留**角色级**映射（复用 `api_server` 的 `roles` 表），后续多角色扩展不改核心校验逻辑。

### 8.3 审计轻量统计脚本（补齐"调用"数据链）

审计不止于落盘追溯，加一个几十行的 `tool_audit_stats.py`（仿 `evaluation.py::get_stats()` / `token_tracker.get_session_summary()`）：

- 统计：**工具调用频次、成功率、平均耗时、错误类型排行、按 trace_id 的调用链长度分布**。
- 与 `RAG评测体系.md`（质量）、`token_tracker`（成本）形成完整的 **成本 / 质量 / 调用** 三条可观测数据链。

> **价值**：从"能追溯故障"升级为"能分析工具使用画像"——高频工具值得优化、低成功率工具值得排查、常被连用的工具组合可**沉淀为新 Skill**，为工具/Skill 迭代提供数据支撑。

---

> 状态：方案已规划，**待用户审阅后再决定是否实现**。
