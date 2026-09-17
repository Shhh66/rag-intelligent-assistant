# 可观测性计划（LangFuse 全链路追踪）

> **实现状态（2026-07 初版，2026-09 修订）**：✅ 已接入并验证（降级安全）。默认 `LANGFUSE_ENABLED=False`；起服务并配密钥后置 True 即启用。
> **2026-09 修订**：SDK 曾因版本漂移（`langfuse` 包升到 4.x）**静默丢光 trace**——SDK v3+ 改用 OTel 上报，打 OSS v2 服务端全是 404，而降级封装只在 debug 级记日志、不抛错。已锁 `langfuse>=2.60,<3` 并改造上报接口（`usage_details=` → `usage=`），同时补齐 generation 耗时与 trace 级 name/user/session 归因。
> 定位：**性价比最高**——项目已有 `token_tracker`（usage + call_site + 持久化）与 `qa_log.jsonl`，LangFuse 只需把这些现成数据升级为可视化的 span 树，埋点成本低、收益直观。

## 已实现部分（本轮）

- **降级安全封装**：新增 `observability.py`——`LANGFUSE_ENABLED=False`/缺密钥/SDK异常/服务未起 全部 no-op，绝不阻断主链路（实测：启用但服务未起时仅打一条导出失败日志，不崩溃）。
- **自托管部署**：`docker-compose.langfuse.yml`（Postgres + LangFuse），数据卷指向 D 盘 `.cache/langfuse_db`；密钥走 `.env`。
- **埋点**：工具调用 span（`scheduler._audit` 内，带 success/latency/error + ERROR 级别标记）；LLM generation 复用 `token_tracker.record()` 上报（含 usage/model/call_site/cost）；`_run()` 结束 flush。
- **trace_id 与审计共享**：同一 `trace_id` 既落审计 JSONL 又串 LangFuse trace，一次打通"机器审计 + 可视化排障"。
- **注意（依赖冲突）**：本地 Windows venv 里 `paddlepaddle 2.6.2` 要求 `protobuf<=3.20.2`，装完 langfuse 须手动回退；**容器（Linux）无此约束**——该上界只在 Windows 生效，容器内 protobuf 7.x 与 paddle 共存无碍。

---

## LangFuse 实际能观测到什么

> 本节描述**当前代码实际埋了什么**（区别于下文第三、四节的设计稿）。

### 一次 `chat()` = 一条 trace

完整调用树（2026-09 实测样例，13 个节点）：

```
trace <32位hex>   latency=57.7s   name=chat   user_id=<用户>   session_id=<会话>
├─ [SPAN]       查询改写                     1.9s
├─ [GENERATION] query_rewrite.clarify       tok=73/251
├─ [SPAN]       混合检索                     2.3s
├─ [GENERATION] retriever.translate_query   tok=64/234
├─ [SPAN]       重排                        29.1s   ⚠️ 瓶颈
├─ [GENERATION] retriever.rag_answer        tok=1351/1187
├─ [SPAN]       工具:ask_knowledge_base       success=true / latency_ms=17275
├─ [GENERATION] decision_engine.decide      tok=1022/523
├─ [SPAN]       judge_evaluate              round=0 / collected_info_length=38
├─ [GENERATION] judge.evaluate              tok=402/93
├─ [GENERATION] decision_engine.final_answer tok=1038/1046
├─ [GENERATION] memory.extract              tok=423/365
└─ [GENERATION] memory.summarize            tok=278/153
```

### 埋点清单

**SPAN（5 类，带耗时 / 成败 / 业务标签）**

| Span 名 | 位置 | metadata |
|---|---|---|
| `工具:<工具名>` | `scheduler.py` | `success` / `error` / `latency_ms` |
| `查询改写` | `retriever.py` | `mode` + 改写前后文本 |
| `混合检索` | `retriever.py` | `hybrid_enabled` |
| `重排` | `retriever.py` | `candidates`（候选数） |
| `judge_evaluate` | `unified_agent.py` | `round` / `collected_info_length` |

**GENERATION（每次 LLM 调用，按 `call_site` 归因，含 token 与成本）**

| 分组 | call_site |
|---|---|
| 决策 | `decision_engine.decide` / `.final_answer` / `.match_skill` |
| 评判 | `judge.evaluate` |
| 检索 | `retriever.translate_query` / `.rag_answer` / `.direct_answer` / `.eval_answer` |
| 查询改写 | `query_rewrite.clarify` / `.multi` / `.hyde` |
| 长期记忆 | `memory.extract` / `.summarize` / `.extract_summary` |
| 会话摘要 | `memory.compress` / `.verify` / `.compress_retry` |
| Skill | `skill_executor.post_process` / `.default_summarize` |
| 其他 | `reranker.llm` / `weather.translate_city` |

### trace 级标签（便于检索与归因）

| 字段 | 来源 | 用途 |
|---|---|---|
| `name` | 固定 `chat` | LangFuse 列表里辨认 |
| `user_id` | `chat(user_id=...)` 请求级参数 | 按用户筛选 trace |
| `session_id` | Agent 实例级；`clear_memory()` 时换新 | 多轮对话归为同一 session |

MCP 子进程通过环境变量 `MCP_TRACE_ID` / `MCP_USER_ID` / `MCP_SESSION_ID` 继承（见 `unified_agent._run`），保证**子进程内的检索 span 并入同一 trace 且归因不丢**。

### 能回答的问题

| 问题 | 怎么看 |
|---|---|
| 「这次为什么慢？」 | 看 trace 瀑布图，哪条最宽就是瓶颈 |
| 「哪个工具不稳定？」 | 按 `工具:*` span 的 `success=false` 筛 |
| 「钱花在哪一步？」 | 按 `call_site` 看 token / 成本分布 |
| 「这次查询经历了什么？」 | 展开调用树，每个节点都有输入输出 |
| 「失败发生在哪一步？」 | 找 `level=ERROR` 的 span |

**实测案例**：一次 57 秒的查询，瀑布图上一眼看出 **29 秒卡在重排**，4.7 秒在最终生成——翻日志得找半天。

### 与已有能力的关系

同一个 `trace_id` 贯穿三处，互补而非重复：

| 载体 | 面向 | 特点 |
|---|---|---|
| **LangFuse** | 人 | 可视化调用树、瀑布图、成本仪表盘 |
| **`tool_audit.jsonl`** | 机器 | 结构化落盘，可编程分析、按 `user_id` 复盘 |
| **`trace_view.py`** | 命令行 | `python trace_view.py -t <前缀>` 文本版链路 |

### 边界（当前看不到的）

| 缺什么 | 原因 |
|---|---|
| 历史 trace 的成本 | 模型价格 2026-09 才在 LangFuse 注册，不追溯既有数据 |
| 精确成本口径 | LangFuse 仅支持单一单价；项目 `token_tracker` 区分**高峰/空闲**双档 + **缓存命中**折扣，**成本以项目侧为准**，LangFuse 看趋势 |
| 离线评测结果 | RAGAS 分数未回写 LangFuse，"离线评测"与"在线追踪"仍是两条线 |
| trace 之间的父子关系 | 多轮对话是并列的独立 trace（用 `session_id` 分组，不是嵌套） |

---

## 一、现状分析

| 能力 | 现状 | 证据 |
|------|------|------|
| Token/成本统计 | **成熟** | `token_tracker.py`：`record(model, usage, call_site)` + `MODEL_PRICING` 折算 + `token_log.jsonl` 实时落盘 |
| 调用点归类 | **有** | `call_site` 标签已覆盖全部 LLM 调用（`retriever.*`/`decision_engine.*`/`query_rewrite.*`/`reranker.llm`/`weather.*`） |
| 问答日志 | **一问一答粒度** | `evaluation.py::log()` → `qa_log.jsonl`，`app.py:277` 整轮结束记一条 |
| 全链路 trace | **无** | 每步工具调用/每个 LLM span 的输入输出/耗时无结构化留存；工具级耗时只进 stderr，与 qa_log 不关联 |
| 可观测框架 | **完全没有** | 全仓库无 langfuse/langsmith/opentelemetry |

**核心缺口**：现有观测是"孤立的点"——成本在 `token_log.jsonl`、问答在 `qa_log.jsonl`、工具耗时在 stderr，**三者无法用一个 trace_id 串成一次完整请求的调用树**。排查"某次回答为什么慢/为什么错"时，无法一眼看到 `chat → Skill匹配 → ReAct决策 → 工具调用 → 检索 → 重排 → 生成` 的完整时间线与每步输入输出。

---

## 二、目标能力

1. **全链路 trace**：一次 `chat()` = 一个 trace，内部每步（决策、工具、检索、重排、LLM 生成）= 一个 span，形成可视化调用树，含耗时、输入、输出。
2. **Token/成本归因**：复用 `token_tracker` 的 usage，在 LangFuse 里按 span/call_site/模型维度看成本分布。
3. **失败 Case 定位**：失败请求自动标红，可回放当时的输入、检索片段、报错，快速定位是"检索问题、决策问题还是生成问题"。

---

## 三、落地设计（引用现有代码）

### 3.1 部署（D 盘自托管）

- LangFuse 支持 **Docker 自托管**（免费、数据不出本地）。`docker-compose` 数据卷指向 D 盘，遵守 CLAUDE.md 环境约定。
- 或用 LangFuse Cloud（省部署，但数据出本地，评估隐私后再定）。
- 配置写 `.env`：`LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST`。

### 3.2 trace 骨架

- **trace 起点**：`unified_agent.py::chat()` 开一个 trace（与 `MCP工程化.md` 的 `trace_id` 复用同一个 ID，两条线打通）。
- **span 埋点**（用 LangFuse SDK 的 `@observe()` 装饰器或上下文管理器，侵入极小）：
  - `SkillRegistry.match()` → span「skill匹配」
  - `DecisionEngine.decide_with_skills()` → span「ReAct决策」
  - `Scheduler._execute_one()` → span「工具:xxx」（每个工具一个）
  - `retriever.retrieve_and_answer / answer_with_fallback` → span「检索」（内嵌改写/混合/重排子 span）
  - LLM 生成 → span「生成」
- **generation 类型 span**：LLM 调用用 LangFuse 的 `generation` 类型，直接吃 `response.usage`，自动算 token/成本。

### 3.3 复用 token_tracker

- `token_tracker.record()` 已有 `call_site` 与 `usage`；在 record 时**同步上报 LangFuse generation**（在现有 record 内加一个可选上报，不改调用方）。
- 成本口径与项目 `MODEL_PRICING` 一致，避免两套数字打架。

### 3.4 失败定位

- 异常/降级（改写失败、混合检索降级、重排降级、工具 is_error）作为 span 的 `level=ERROR/WARNING` 标记 + 附当时上下文。
- 与 `evaluation.py` 的 bad case、`RAG评测体系.md` 的 Bad Case 分析对齐，形成"离线评测 + 在线追踪"双视角。

---

## 四、配置项设计

```python
# config.py 新增
LANGFUSE_ENABLED = False             # 可观测性总开关（默认关，零依赖时不影响运行）
# 密钥走 .env：LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
LANGFUSE_SAMPLE_RATE = 1.0           # 采样率（生产可降低）
```

---

## 五、与现有体系联动

- **token_tracker**：成本数据直接喂 LangFuse generation，不重复造轮子。
- **MCP工程化.md**：共用 `trace_id`——审计日志（结构化落盘）+ LangFuse（可视化）互补，一个给机器审计、一个给人排障。
- **RAG评测体系.md**：离线 RAGAS 指标 + 在线 LangFuse trace，覆盖"实验室质量"与"生产表现"。

---

## 六、优先级与代价

- **优先级**：★★★★★ 埋点侵入小（装饰器/上下文管理器）、复用现成成本数据、收益直观（可视化调用树 + 成本仪表盘）。
- **代价**：新增 `langfuse` 依赖（D 盘）；自托管需 Docker；埋点需覆盖主要调用点（一次性工作）。
- **收益**："全链路可观测"是 Agent 工程化的标配能力，简历上"接入 LangFuse 做 trace/成本/失败定位"是硬亮点。

---

## 七、风险与注意

- **降级不阻断**：`LANGFUSE_ENABLED=False` 或上报失败时，绝不能影响主链路（上报包 try/except，异步 flush）。
- **隐私**：自托管优先，避免用户问答/文档片段出本地；Cloud 方案需评估合规。
- **性能**：上报走异步批量 flush，避免阻塞请求；高并发可调 `LANGFUSE_SAMPLE_RATE`。
- **密钥管理**：LangFuse 密钥进 `.env`（已被 .gitignore），不硬编码（呼应本项目刚修过的密钥迁移）。

---

## 八、低成本优化补充

### 8.1 优先埋点核心路径，不追求全覆盖

不必一次性给所有函数埋点，先覆盖主链路 **5-6 个核心 Span** 即可拿到 80% 效果：

```
chat 总 Trace
  ├─ Skill 匹配 (SkillRegistry.match)
  ├─ ReAct 决策 (DecisionEngine.decide_with_skills)
  ├─ 工具调用 (Scheduler._execute_one，每工具一个)
  ├─ RAG 检索 (retriever，内嵌改写/混合/重排子 span)
  └─ 最终生成 (LLM generation)
```

边缘功能（知识库管理、用户登录、`debug_rerank`）后续再补。个人项目用最少工作量拿到可演示、可排障的核心能力。

### 8.2 业务维度标签（价值从"排障"延伸到"运营分析"）

给核心 Span 加 metadata 标签，不改架构、只加几个字段，就能按维度统计业务指标：

| Span | 业务标签 | 可统计的指标 |
|------|---------|-------------|
| 检索 Span | `retrieve_channel`(向量/BM25/混合)、`is_rewritten`(是否走改写) | 混合检索占比、**改写生效率** |
| 决策 Span | `hit_skill`(是否命中 Skill)、`react_turns`(ReAct 轮数) | **Skill 命中率**、平均决策轮数 |
| 工具 Span | `is_retry`、`retry_count` | **工具重试率**、易失败工具排行 |

这些标签的数据来源现成：`hybrid_retriever` 知道走没走 BM25、`query_rewriter` 知道有没有改写、`decision_engine` 知道命没命中 Skill、`MCP工程化.md` 的重试逻辑知道 retry_count。与现有 RAG 评测、成本统计体系对齐，形成"效果 + 成本 + 运营"多维视图。

### 8.3 异常标记复用现有降级体系（不重定义错误）

直接把现有降级场景映射到 LangFuse Span 级别，全链路异常定义一致：

| 场景 | LangFuse 级别 |
|------|:---:|
| 工具调用失败、检索异常、DB 不可用 | `ERROR` |
| 重排降级、改写降级(翻译空)、混合检索降级纯向量、Skill 未命中降级 ReAct | `WARNING` |
| 正常执行 | `INFO` (默认) |

现有代码里这些降级点都有明确日志（`⚠️` 前缀），埋点时顺手打级别标签即可，零额外错误体系。

### 8.4 准备一个演示用 Bad Case（面试杀手锏）

预置一个可复现的排障演示，比口头说"我接了 LangFuse"直观 10 倍：

> **演示脚本**：拿一个口语化模糊问题（如"那个能量消耗咋回事"），在 `QUERY_REWRITE_ENABLED=False` 下故意让它答不准 → 打开 LangFuse Trace → 展开检索 Span → 看到 `is_rewritten=false`、检索输入是原始口语 query、召回内容偏 → 定位根因"未开查询改写"→ 打开开关重跑，Trace 里 `is_rewritten=true`、召回正确、答案改善。

一次点击式的可视化排障，把"可观测性的价值"讲活。可直接复用 `RAG评测体系.md` 里已有的 fuzzy 分层题作素材。

---

> 状态：方案已规划，**待用户审阅后再决定是否实现**。

---

## 九、做深短板改进计划（STAR 法则）

> 承接《生产级Agent改进计划.md》的「做深清单」，把两块最显眼的短板用 STAR 结构落成可执行计划。可观测性指标告警是本文档 LangFuse 的延伸（trace 已有，补指标告警）；成本控制模型网关是并列的做深短板。

### 9.1 可观测性：从「全链路 trace」补到「指标 + 告警」

**S（情境）**：可观测性已具备 trace_id 贯穿、审计 JSONL、LangFuse 全链路追踪、Token 记录（见前八节），缺的是**运行时指标采集与告警**——熔断打开、限流触发、端到端延迟劣化只散落在 stderr 日志，无人值守时故障不可见，排查靠事后翻日志。

**T（任务）**：补上可观测性的最后两环——监控指标采集 + 告警，让运行状态实时可见、故障自动发现。

**A（行动）**：
1. 新增 `metrics.py`，暴露 Prometheus 风格 `/metrics` 端点，采集四类指标：熔断状态（每 destination 的 CLOSED/OPEN/HALF_OPEN）、限流触发次数（用户级/LLM 级）、工具调用成功率与端到端延迟 P50/P99、LLM 调用次数与 Token 消耗。
2. 接入 Grafana 仪表盘 + 告警规则：熔断 OPEN 持续、5xx 率、延迟 P99 超阈值 → webhook 通知。
3. 复用现有 `trace_id`，让指标、审计、LangFuse 三线对齐，形成「trace 排障 + 指标监控 + 告警」闭环。

**R（结果）**：熔断打开、限流触发、延迟劣化实时可感、自动告警，故障发现从「人工盯日志」升级为「自动告警」——补齐「Demo vs 生产」最直接的可观测证据。

### 9.2 成本控制：简化模型网关

> **MVP 已实现**（`model_gateway.py`）：模型路由（`route_model`）+ 单任务 Token 预算（`check_budget`）。零破坏——仅在 `call_llm_with_cb` 新增一行路由、`_pipeline` 循环新增一段预算检查；`MODEL_ROUTE` / `TASK_TOKEN_BUDGET` 默认关闭时行为与改造前完全一致。

**S（情境）**：所有 LLM 调用统一走单一大模型（`deepseek-flash`）——意图判断、参数提取、技能匹配等简单任务与 ReAct 决策、最终回答等强推理任务用同一模型；且多轮 ReAct 循环无成本上限，理论上可能把 token 打爆。

**T（任务）**：做简化模型网关，具备按调用点分派模型的能力 + 单任务 Token 预算控制。

**A（行动）**：
1. 新增 `model_gateway.py` 路由层：按 `call_site` 分派模型，`MODEL_ROUTE` 配置即生效。
2. 单任务 Token 预算：超上限强制终止并返回中间结果（现状只有 80% 告警、不终止）。
3. 缓存复用：相同参数的工具调用结果缓存（检索缓存已有，扩展到其他可缓存工具）。

**R（结果）**：**单任务预算防超支**是当前真正在起作用的成本控制；
**模型路由机制就位但暂未启用** —— 因为 `deepseek-flash`（V4.1）在并发（2500 vs 500）、
图像理解上均优于 `deepseek-v4-pro`，价格还更低，**没有「强推理切 pro」的必要**。
路由的价值留待接入第二个模型（其他厂商 / 本地模型）时兑现。

> 状态：两块 MVP 均已落地——可观测性告警（`alerting.py`）、成本控制模型网关（`model_gateway.py`）。标准路线（Prometheus 全家桶 / 完整模型网关）作为讲设计素材（见 9.3 及生产级文档），待有规模后再上。

### 9.3 告警落地：轻量已实现 + 标准路线讲设计

**轻量告警已落地（MVP，`alerting.py`）**：进程内轮询熔断器状态、检测状态迁移（CLOSED/HALF_OPEN → OPEN 告警「熔断打开」、OPEN → CLOSED 告警「熔断恢复」），三通道输出（stderr 日志 + `alert.log` 落盘 + 预留钉钉/飞书 webhook）。已跑通演示，零破坏（独立新文件，未动任何已有代码）。

**标准路线（Prometheus 全家桶，讲设计素材，不实现）**：

**S（情境）**：轻量告警是单机 MVP——进程内轮询 + 本地落盘，能演示、能防单机无人值守，但无法规模化：多实例部署时每个实例各存一份状态，指标无法统一采集、告警无法集中管理。

**T（任务）**：设计标准化指标 + 告警体系，让多实例运行状态可统一采集、可视化、自动告警。

**A（行动/设计）**：
1. **指标层**：`prometheus_client` 暴露 `/metrics`，四类指标——熔断状态（Gauge，scrape 时实时读而非埋点写）、限流触发（Counter）、工具调用/延迟（Counter + Histogram）、LLM Token（Counter）。
2. **采集层**：Prometheus 定时抓取各实例 `/metrics`。
3. **可视化 + 告警**：Grafana 仪表盘；Alertmanager 规则（熔断 OPEN 持续、5xx 率、P99 延迟超阈值）→ webhook（钉钉/飞书）。

**R（结果/预期）**：从「单机日志人工盯」升级为「集群级实时监控 + 自动告警」，故障发现从被动翻日志变主动推送。

**面试讲法（一句话）**：*「告警能力我先用轻量方案跑通了——进程内轮询熔断器状态、状态迁移即告警、落盘加 webhook；如果要上多实例，再上 Prometheus 全家桶做标准化指标采集和集中告警，这块我知道怎么设计。」*
