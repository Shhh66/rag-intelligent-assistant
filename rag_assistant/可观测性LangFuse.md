# 可观测性计划（LangFuse 全链路追踪）

> **实现状态（2026-07）**：✅ **已接入并验证（降级安全）**。默认 `LANGFUSE_ENABLED=False`；起服务并配密钥后置 True 即启用。
> 定位：**性价比最高**——项目已有 `token_tracker`（usage + call_site + 持久化）与 `qa_log.jsonl`，LangFuse 只需把这些现成数据升级为可视化的 span 树，埋点成本低、收益直观。

## 已实现部分（本轮）

- **降级安全封装**：新增 `observability.py`——`LANGFUSE_ENABLED=False`/缺密钥/SDK异常/服务未起 全部 no-op，绝不阻断主链路（实测：启用但服务未起时仅打一条导出失败日志，不崩溃）。
- **自托管部署**：`docker-compose.langfuse.yml`（Postgres + LangFuse），数据卷指向 D 盘 `.cache/langfuse_db`；密钥走 `.env`。
- **埋点**：工具调用 span（`scheduler._audit` 内，带 success/latency/error + ERROR 级别标记）；LLM generation 复用 `token_tracker.record()` 上报（含 usage/model/call_site/cost）；`_run()` 结束 flush。
- **trace_id 与审计共享**：同一 `trace_id` 既落审计 JSONL 又串 LangFuse trace，一次打通"机器审计 + 可视化排障"。
- **注意（依赖冲突已解决）**：langfuse 会拉高 protobuf 破坏 paddle，安装后须 `pip install "protobuf<=3.20.2"` 回退；langfuse 走 HTTP 不受影响，二者共存（见 requirements.txt 注释）。

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
