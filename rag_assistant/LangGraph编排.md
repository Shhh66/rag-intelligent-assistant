# LangGraph 编排计划（并存不替换）

> 本文档规划引入 LangGraph 做复杂任务编排。**方案待定，本轮不实现代码。**
> **核心决策：并存不替换。** 保留自研 ReAct 作为项目核心亮点，LangGraph 作为复杂任务/多 Agent 的新增编排通道，讲"按任务复杂度选引擎"的故事。

---

## 一、现状分析

| 维度 | 现状 | 证据 |
|------|------|------|
| 推理引擎 | **自研 ReAct** | `unified_agent.py::_pipeline()` 的 `for turn in range(max_turns)`；手写决策解析 + 调度 |
| 多轮能力 | **伪多轮** | "成功即退出"，无真正 Thought↔Observation 迭代；多轮只在工具连续失败时发生 |
| 编排结构 | 静态 Skill steps + 命令式 turns | **无状态机/DAG**，无条件分支/循环/回滚/checkpoint |
| LangGraph | **零依赖** | 全仓库无 langgraph；agent core 只用 openai + mcp SDK |
| LangChain | 仅 RAG 检索侧 | 推理层完全自研（CLAUDE.md"自研 ReAct 替换 LangChain"属实） |

**核心事实**：自研 ReAct 是**项目最大差异化亮点**（"我手写了推理引擎，还保留了 LangChain 没有的并行工具调用"）。它的短板是"伪多轮、无状态机"，但这些短板可以用**新增 LangGraph 通道**补齐，而非推翻重写。

---

## 二、为什么"并存不替换"（决策依据）

| 方案 | 优点 | 致命问题 |
|------|------|---------|
| **彻底替换**（LangGraph 重写 ReAct） | 拿到状态机/checkpoint | ❌ **削弱核心叙事**（"自研引擎"变"调框架")；❌ 重写 `unified_agent`/`decision_engine`/`scheduler` 三大件，工作量巨大；❌ 现有并行能力/Skill 体系推倒 |
| **并存不替换**（本方案） | ✅ 保住"自研 ReAct"亮点；✅ 复杂任务有 LangGraph 撑；✅ 增量、低风险 | 需维护两套引擎 + 一个路由层 |

**结论**：并存。叙事升级为——"**简单任务走自研轻量 ReAct（快、省），复杂多阶段任务走 LangGraph 状态图（稳、可控），由复杂度路由自动选择**"。这比单纯"我用了 LangGraph"更有工程判断力。

---

## 三、目标架构

```
                 用户请求
                    │
                    ▼
          ┌──────────────────┐
          │  复杂度路由         │  关键词/LLM 判断任务复杂度
          └────┬────────┬─────┘
       简单任务 │        │ 复杂多阶段任务
               ▼        ▼
   ┌───────────────┐  ┌──────────────────────┐
   │ 自研 ReAct     │  │ LangGraph 状态图        │
   │ (现有，保留)    │  │ (新增)                 │
   │ UnifiedAgent  │  │ 多 Agent 编排/条件分支/  │
   │ 单步/少步、并行 │  │ checkpoint/状态传递     │
   └───────────────┘  └──────────────────────┘
          │                    │
          └──── 共用 MCP 工具 / RAG 检索 / token_tracker ────┘
```

---

## 四、落地设计

### 4.1 路由层（新增，轻量）

- **新建 `engine_router.py`**：`route(query, history) -> "react" | "langgraph"`。
- 判定策略（由简到繁）：
  1. **关键词/规则**：含"分析并生成/多步/先…再…/报表"等 → langgraph；单一问答 → react。
  2. **LLM 轻判断**：一次小调用让 LLM 判"这是单步还是多阶段任务"（注意 max_tokens≥512，推理模型陷阱）。
- 默认走 react（兼容现状），langgraph 仅在明确复杂时启用。

### 4.2 LangGraph 通道（新增）

- **新建 `graph_agent/` 包**：定义 `StateGraph`、共享 `State`(TypedDict)、节点（子 Agent）、条件边。
- **节点复用现有能力**：
  - 检索节点 → 调 `retriever.retrieve_and_answer()`
  - 工具节点 → 调现有 MCP 工具（经 `scheduler`/`mcp_client_manager`）
  - Skill 节点 → 包装现有 `SkillExecutor`
- **Supervisor 模式**：承载 `多Agent协作.md` 的主管+三子 Agent。
- **checkpoint**：用 LangGraph 的 checkpointer 做状态持久化——顺带补齐"长期记忆"的会话状态留存（与 `长期记忆.md` 联动）。

### 4.3 共享底座（两引擎复用，不分叉）

- MCP 工具、RAG 检索、`token_tracker`、`trace_id`（`MCP工程化.md`/`可观测性LangFuse.md`）两引擎共用，避免能力分裂。

---

## 五、配置项设计

```python
# config.py 新增
ENGINE_MODE = "auto"          # auto(路由) | react(强制自研) | langgraph(强制图)
LANGGRAPH_ENABLED = False     # LangGraph 通道开关（默认关，不影响现有）
ROUTE_STRATEGY = "keyword"    # keyword | llm（复杂度判定方式）
```

---

## 六、与现有体系联动

- **自研 ReAct**：完全保留，作为 `ENGINE_MODE=react` 与简单任务默认引擎。
- **多 Agent**：`多Agent协作.md` 的三 Agent + 主管以 LangGraph 实现，本文档是其基座。
- **长期记忆**：LangGraph checkpointer 可承载会话状态持久化，与 `长期记忆.md` 协同。
- **可观测**：LangGraph 每个节点作为 LangFuse span，多引擎调用树统一可视化。

---

## 七、优先级与代价

- **优先级**：★★ 工作量最大、与"自研亮点"叙事需谨慎平衡。**建议排在 MCP工程化/LangFuse/长期记忆之后**，作为"多 Agent"的前置基座一起做。
- **代价**：维护两套引擎 + 路由层；新增 `langgraph` 依赖（D 盘）；学习成本。
- **收益**：补齐状态机/条件编排/checkpoint 短板；"双引擎按复杂度路由"是有工程判断力的叙事。

---

## 八、风险与注意

- **叙事风险（核心）**：务必保留并突出"自研 ReAct"，LangGraph 定位为"复杂任务补充"而非"替代"。面试话术："我自研了轻量 ReAct 处理主流场景，又用 LangGraph 处理需要状态机的复杂编排，按复杂度路由。"
- **双引擎维护成本**：两套引擎要共用底座（工具/检索/记账），否则能力分裂、维护翻倍。
- **过度工程**：若没有真实复杂任务需求，LangGraph 通道可能闲置；建议与 `多Agent协作.md` 的真实 demo 场景绑定验证。
- **路由误判**：复杂度路由判错会把复杂任务丢给弱引擎或把简单任务丢给重引擎；需可回退 + 可强制指定 `ENGINE_MODE`。
- **依赖**：`langgraph` 装 D 盘，遵守 CLAUDE.md 环境约定。

---

## 九、优化建议（贴合面试定位，低成本高收益）

### 9.1 路由层预留反馈闭环（现在不做，留接口 + 提思路）

在设计里补一句"可迭代的路由优化机制"，体现闭环思考：

- 路由错误的 Case（如复杂任务误判给 ReAct 后失败）→ 自动沉淀到**优化样本库**（复用 `MCP工程化.md` 的审计日志，标记 `route_miss`）。
- 定期用样本迭代关键词规则，样本足够后甚至训练一个轻量分类器替代 LLM 判断（更快更省）。

> **现在只需留接口 + 文档提一句**。面试话术："路由不是写死的，误判 Case 会沉淀成样本，支持规则迭代乃至训练分类器"——体现对闭环迭代的思考。

### 9.2 LangGraph 通道先做最小图（与多 Agent MVP 对齐）

不必一上来做完整三 Agent Supervisor。**先做两节点最小图**跑通流程：

```
检索节点 (retriever) → 总结生成节点 (LLM) → END
```

正好对应 `多Agent协作.md` 的最小 Demo（"分析6G文档→生成调研摘要"）。工作量极小，却能完整演示 **StateGraph / 节点流转 / State 传递** 三个核心特性，面试够用。验证后再扩展为 Supervisor 多 Agent。

### 9.3 checkpoint × 长期记忆 = 完整记忆体系（互补）

LangGraph 自带的 `checkpointer` 与 `长期记忆.md` 恰好互补，两层记忆各管一段：

| 层 | 存什么 | 能力 |
|----|-------|------|
| **长期记忆**（`长期记忆.md`） | 跨会话的实体、偏好、结论 | 让 Agent "记得用户是谁" |
| **LangGraph checkpoint** | 会话内的完整图状态 | 中断恢复、多轮续答、断点重跑 |

二者结合形成 **"长期记忆（跨会话）+ 会话状态（会话内）"** 的完整记忆体系，架构完整性显著增强。

### 9.4 叙事拔高：企业级"成本-效率平衡"

把双引擎上升到工业界 Agent 平台思路：

> "工业界的 Agent 平台不会只用一种引擎，而是**按场景调度流量**：高频简单场景用轻量引擎降本提效，低频复杂场景用重型引擎保证能力，路由层做流量分配。我这个自研 ReAct + LangGraph 的双引擎架构，就是这个思路的微型实现。"

这把"我用了两个框架"升维成"我理解生产级 Agent 的成本-效率权衡"，是更高阶的工程叙事。

---

> 状态：方案已规划，**待用户审阅后再决定是否实现**。是 `多Agent协作.md` 的前置基座。
