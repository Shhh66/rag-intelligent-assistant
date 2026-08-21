# 企业级 RAG 系统架构

> 从企业级视角梳理整个 RAG 代码架构，覆盖**分层、进程模型、两条核心链路、六大企业级能力、关键架构决策与取舍、演进路线**。反映部署运维改造 P0–P3 全部落地后的最新状态。
>
> 区别于 [目录架构.md](目录架构.md)（代码地图/目录罗列），本文档回答的是「**为什么这样分层、状态放在哪、容错/权限/可观测如何闭环**」。

---

## 一、总体架构

### 1.1 分层架构（7 层）

```
┌──────────────────────────────────────────────────────────────┐
│ ① 前端层      Streamlit(app.py) + Vue3(frontend/)            │
├──────────────────────────────────────────────────────────────┤
│ ② 接入服务层  api_server(权限+限流) + mcp_server(MCP工具)     │
│              + embed_server + rerank_server(模型服务)         │
├──────────────────────────────────────────────────────────────┤
│ ③ 智能体核心  mcp_unified_agent/(ReAct决策 + Skill + 调度)    │
├──────────────────────────────────────────────────────────────┤
│ ④ 工具层      Weather / 教务 / 知识库(MCP 工具)              │
├──────────────────────────────────────────────────────────────┤
│ ⑤ RAG 检索层  loader → splitter → hybrid(BM25+向量) → rerank │
├──────────────────────────────────────────────────────────────┤
│ ⑥ 支撑层      config / 熔断 / 限流 / 缓存 / 审计 / 记忆 / 可观测│
├──────────────────────────────────────────────────────────────┤
│ ⑦ 评测层      rag_eval/(RAGAS 4 指标 A/B)                    │
└──────────────────────────────────────────────────────────────┘
```

### 1.2 进程模型（关键：状态放在常驻进程）

企业级架构的第一个核心问题是「**有状态的组件放哪个进程**」。本项目有四种进程形态，状态归属是刻意的设计：

| 进程 | 生命周期 | 常驻状态 | 为什么不放状态 |
|------|---------|---------|--------------|
| **主进程**（app.py + UnifiedAgent） | 常驻（`st.session_state.agent`） | 熔断器、限流器、对话历史、反思记忆、长期记忆 | ✅ 状态天然跨请求累积 |
| **MCP 子进程**（mcp_server.py） | **per-request**（每次 chat 新建 stdio） | 无 | 每次归零，放熔断/限流会形同虚设 |
| **模型服务**（embed/rerank_server） | 常驻 HTTP | 模型权重（420MB/568MB） | 多实例共享一份模型 |
| **权限后端**（api_server） | 常驻 HTTP | JWT、SQLite、用户级限流 | 独立鉴权，不信任入参 |
| **Redis** | 常驻 | 检索缓存、限流令牌桶 | 多实例共享的分布式状态 |

**架构铁律**：熔断器、限流器这种「跨请求统计」的组件，必须放**常驻进程**（主进程）；子进程 per-request，只能做「无状态 + 单次请求内降级」。

### 1.3 数据流（一次用户提问的完整路径）

```
用户提问
  ↓
app.py(Streamlit, 常驻) ── kb_groups/user_id 注入 ──▶ UnifiedAgent.chat()
  ↓ 主进程常驻（熔断/限流状态在这里累积）
  ├─ Skill 匹配(关键词+向量) → LLM 确认 → 命中则 1 轮执行
  └─ 否则 ReAct 循环（最多 5 轮）
       ├─ decision_engine.decide_with_skills ──▶ call_llm_with_cb ──[限流→熔断]──▶ DeepSeek
       └─ scheduler.execute ──call_tool(stdio)──▶ mcp_server.py(子进程, per-request)
            ├─ ask_knowledge_base → retriever.answer_with_fallback
            │    ├─ 查缓存(Redis, 命中跳过检索)
            │    ├─ 查询改写 → 混合检索(BM25+向量 RRF) → 双语翻译 → 合并去重
            │    ├─ 重排(BGE) → 写缓存 → LLM 生成
            │    └─ 权限过滤(kb_groups → ChromaDB where)
            ├─ query_weather / edu_query_schedule
            └─ 失败靠 fallback 兜底（子进程无熔断，单次请求内降级）
  ↓
final_answer ──▶ 汇总工具结果 → 用户可读回答（引用来源）
```

---

## 二、两条核心链路

### 2.1 RAG 检索链路（子进程内，无状态 + 全降级）

```
query
  → 检索缓存(Redis, 命中则跳过检索，仍走重排+LLM)
  → 查询改写 clarify（失败回退原 query）
  → 混合检索 hybrid（BM25 稀疏 + 向量稠密，RRF 融合；失败回退纯向量）
  → 双语检索（中文 + 翻译成英文；翻译失败跳过英文）
  → 合并去重
  → 重排 rerank（Cross-Encoder 精排；失败降级 LLM → 原序）
  → LLM 生成（引用来源标注）
```

**设计原则**：子进程是 per-request 的，所以检索链路「无状态 + 全链路降级」——改写/混合/翻译/重排任一失败都无感回退，主链路绝不中断。

### 2.2 Agent 决策链路（主进程内，有状态 + 容错）

```
user_input
  → Skill 匹配（关键词 + 向量双重匹配 Top3）
  → decision_engine.match_skill（LLM 确认，confidence ≥ 0.5 才执行）
  → 命中 → SkillExecutor（1 轮执行，失败降级 ReAct）
  → 未命中 → ReAct 循环（Thought → Action → Observation，最多 5 轮）
       ├─ decide_with_skills（LLM 决策，每轮都调）
       ├─ scheduler（并行/串行工具调度）
       └─ 工具结果回填 → 下一轮决策
  → final_answer（LLM 汇总所有工具结果）
```

**设计原则**：主进程决策链路是「有状态 + 容错三件套」——决策失败整个请求失败（最致命），所以熔断器、限流器都接在这里。

---

## 三、六大企业级能力

### 3.1 权限与安全（fail-closed + 信任边界）

| 维度 | 实现 |
|------|------|
| 组级权限 | `kb_group`（部门）+ `visibility`（public/internal），ChromaDB `where` 过滤 |
| 双通道一致 | BM25 索引签名纳入 metadata，权限变更驱动重建（防两通道泄露） |
| 入口 fail-closed | 登录守卫（读）+ 上传守卫（写），未登录不能检索/上传 |
| 请求级无状态 | `kb_groups` 从登录态一路透传（非文件共享），消除跨进程共享可变状态 |
| 信任边界 | MCP 工具默认 `kb_groups=None` 可伪造——stdio 内部可控，暴露 HTTP/SSE 需网关鉴权（明示边界） |

**关键**：入口 fail-closed（默认拒绝），内部无状态信任入参——「谁能进来」和「进来能看什么」两层分离。

### 3.2 容错三件套（重试 / 熔断 / 限流）

| 机制 | 管什么 | 位置 | 降级方向 |
|------|--------|------|---------|
| **重试**（tenacity 指数退避） | 单次瞬时抖动（超时/连接） | `mcp_client_manager.call_tool` | 只重试瞬时故障，业务错误不重试 |
| **熔断**（circuit_breaker） | 下游持续故障 | `call_llm_with_cb`（主进程 decision_engine + skill_executor） | **fail-closed**（故障必须快速失败） |
| **限流**（rate_limiter） | 上游频率超限 | 用户级 `api_server` + LLM 级 `call_llm_with_cb` | **fail-open**（Redis 挂了放行） |

**三者维度正交**：重试管「单次」、熔断管「下游」、限流管「上游」，互不替代。

**熔断器设计**（per-destination + HALF_OPEN 单探测）：
- `get_breaker(destination)` 按下游分区，天气挂了不误伤知识库（Hystrix command key 同思想）
- HALF_OPEN 只放 1 次试探，防并发放大

**限流器设计**（两层 + 令牌桶）：
- 用户级 `rate:user:{username}`（防滥用/控成本），LLM 级 `rate:llm:deepseek`（配合供应商 RPM）
- Redis Lua 原子操作，令牌桶允许突发 + 平滑

### 3.3 可观测

| 能力 | 实现 |
|------|------|
| 工具调用审计 | `tool_audit.py` → `tool_audit.jsonl`（trace_id / 入参脱敏 / 耗时 / 成败 / 重试数） |
| Token 用量 + 成本 | `token_tracker.py`（会话累积 + 文件持久化 + 定价表） |
| 全链路可观测 | `observability.py`（LangFuse，未启用全 no-op 降级） |
| trace_id 贯穿 | 一问多工具归并同一链路（审计 + LangFuse 打通） |
| 问答溯源 | Prompt 强制引用来源 + 末尾列参考 |

### 3.4 缓存（Redis 分布式）

- 缓存「检索结果」（合并去重后的 docs），不是「最终答案」（答案依赖 LLM 语义命中率低）
- key = `md5(query + 排序后 kb_groups)`，权限维度进 key 防跨租户串台
- TTL 用 Redis `setex` 自动过期，不自己造 LRU
- Redis 不可用 → 降级关闭缓存，不阻断检索

### 3.5 服务化（模型 + 向量库）

| 服务 | 形态 | 说明 |
|------|------|------|
| embed_server | HTTP `/embed` + `/ready` | 嵌入模型独立，多实例共享一份 420MB |
| rerank_server | HTTP `/rerank` + `/ready` | 重排模型独立，568MB |
| Chroma Client/Server | `CHROMA_SERVER_URL` 一键切换 | 空=PersistentClient（单机），非空=HttpClient（多实例共享） |

### 3.6 评测与质量（RAGAS）

- 4 指标：context_recall / context_precision / faithfulness / answer_relevancy
- A/B 对比：基线（纯向量）vs 优化（双语 + 重排），量化每个优化项的增益
- 分层 + Bad Case 分析 + 成本报告
- DeepSeek 兼容（`n=1`）+ 答案清洗（去除引用格式防误判）

---

## 四、关键架构决策与取舍

| # | 决策 | 取舍 |
|---|------|------|
| 1 | **熔断器放主进程，不放子进程** | 子进程 per-request 状态每次归零，熔断形同虚设。「状态能跨请求累积」是比「离故障源近」更前置的前提 |
| 2 | **fallback vs 熔断职责边界** | fallback 管「单次请求内降级」（子进程），熔断管「跨请求快速失败」（主进程）。子进程不接熔断，靠 fallback 兜底 |
| 3 | **权限无状态透传（非文件共享）** | `kb_groups` 请求级参数贯穿全链路，消除「全局共享可变状态」的串台风险 |
| 4 | **RRF 只做候选合成，Cross-Encoder 做精排** | 回避「BM25 分数 vs 余弦分数不可比」——RRF 用排名、重排用语义 |
| 5 | **熔断 fail-closed，限流 fail-open** | 熔断管「下游故障」（必须快速失败），限流管「频率」（软保护，Redis 挂了放行） |
| 6 | **按规模演进，不超前设计** | 单机 PersistentClient → 多实例 HttpClient → 百万级再换 Milvus。每一步「到了那个规模才做那个事」 |

---

## 五、演进路线（P0 → P3 企业化过程）

| 阶段 | 内容 | 解决的企业级缺口 |
|------|------|----------------|
| **P0** | 模型服务化 + 权限重构 | 模型内嵌进程、权限文件共享 → 服务化 + 请求级无状态 |
| **P1** | 熔断器 + 容器化 | 下游故障雪崩、环境不可复现 → 熔断（主进程）+ Docker 编排 |
| **P2** | 检索缓存 + 向量库服务化 | 热点重复计算、向量库单机 → Redis 缓存 + Client/Server |
| **P3** | 限流（两层） | 缺频率控制 → 用户级 + LLM 级令牌桶 |

**贯穿原则**：每一步「零破坏」——不配置服务地址/不传参数时，行为与改造前完全一致。

---

## 六、模块速查表（按企业级职责）

| 职责域 | 模块 | 关键点 |
|--------|------|--------|
| 权限 | `api_server.py` + `vector_store.py` | JWT + 组级 where 过滤 + 用户级限流 |
| 容错 | `circuit_breaker.py` + `rate_limiter.py` | 熔断（per-destination）+ 限流（两层令牌桶） |
| 缓存 | `search_cache.py` | Redis 检索缓存（权限维度 key） |
| 可观测 | `tool_audit.py` + `token_tracker.py` + `observability.py` | 审计 + 成本 + LangFuse |
| 检索 | `retriever.py` + `hybrid_retriever.py` + `bm25_index.py` + `reranker.py` | 改写→混合→重排全降级 |
| 智能体 | `unified_agent.py` + `decision_engine.py` + `scheduler.py` + `skill_*` | ReAct + Skill + 并行调度 |
| 服务化 | `embed_server.py` + `rerank_server.py` | 模型 HTTP 服务 |
| 评测 | `rag_eval/` | RAGAS 4 指标 A/B |
| 部署 | `Dockerfile` + `docker-compose.yml` | 4 服务编排 + 健康检查 |
