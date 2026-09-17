# MCP 可控智能体平台 —— 私有知识问答与业务自动化

基于 **MCP 协议**构建的生产级 RAG + Agent 智能体平台。核心理念：**Agent 不关心工具内部实现，只需一套标准协议即可将任意业务系统接入为可调用的工具**。已接入知识库检索、天气查询、教务课表查询等工具，展示 Agent 自主发现工具、判断意图、编排调用的完整链路。

## 为什么谈"生产级"

一个能跑的 Demo，本质是「`while` 循环 + 几次工具调用」。但要让 Agent 真的上线，必须回答另外一批问题：模型 API 超时了怎么办、工具挂了会不会拖垮全局、上下文塞爆了怎么办、钱花在哪了、出了问题怎么查、用户 A 会不会看到用户 B 的数据。

本项目围绕这些工程问题，落地了六大模块能力。

## 生产级六大模块

| # | 模块 | 关键实现 |
|---|------|---------|
| 1 | 核心执行引擎 | 自研五阶段 ReAct + 独立 Judge + 非 LLM 护栏 |
| 2 | 模型网关层 | 统一 LLM 入口（限流→熔断→重试→降级）+ 单任务预算 + 成本归因 |
| 3 | 工具管理层 | MCP 标准化接入 + 权限无状态透传 + 超时/重试/进程隔离 |
| 4 | 记忆管理层 | 短期滚动摘要压缩 + 长期双写实体记忆 |
| 5 | 可观测性层 | trace 贯穿 + 审计落盘 + 成本归因 + 熔断告警 + 链路还原 |
| 6 | 安全层 | fail-closed 守卫 + JWT/RBAC + 数据隔离 + 两层限流 |

## 1. 核心执行引擎

系统核心，负责运行 Agent Loop、调度工具、组装上下文。

**自研五阶段决策**（替换 LangChain AgentExecutor）：每轮决策产出「规划 → 推理 → 行动 → 观察 → 评估 → 决策」完整链路，全部结构化留痕，可回放。

**独立 Judge 模型**：由独立模型判断「已收集信息是否足够回答」，与主决策引擎职责分离，可独立替换；"信息不足"会真正触发下一轮，而不是靠模型在输出里自我声明"我答完了"。

**非 LLM 护栏**（不依赖模型自觉）：轮次上限、死循环检测、熔断快速失败。

**异常兜底**：模型返回格式异常、工具调用失败、API 超时均有对应处理；结构化输出探测到接口不支持时自动降级，不中断请求。

> 与 LangChain AgentExecutor 的三点硬差异：① 无依赖工具**异步并行**（"查天气 + 查知识库"同时发起）② **结构化输出**约束替代正则解析 ③ **非 LLM 护栏**兜底。其余（五阶段 / Judge / 技能路由）是架构可维护性加分项，非效果层硬优势。

## 2. 模型网关层

封装的不是"调用 API"，而是**把不可靠的上游变成可靠的依赖**。

| 能力 | 说明 |
|------|------|
| 统一 LLM 入口 | `call_llm_with_cb` —— **限流 → 熔断 → 用量记账**串成一条链，所有主进程调用点共用 |
| 熔断保护 | per-destination 三态状态机，**fail-closed**（下游故障必须快速失败）；4xx 客户端错误不计入熔断 |
| 模型路由 | 按调用点分派模型的**机制**已就位（当前单一模型，未启用路由） |
| 单任务 Token 预算 | 超上限强制终止并返回中间结果 |
| 成本归因 | 按调用点记账，区分**缓存命中/未命中**输入价与**峰谷时段**价 |

> 瞬时故障的**重试**不在这一层——它用在**工具调用**路径上（见下一节「故障隔离」），
> LLM 调用路径当前是「熔断 + 限流」，不做重试。

## 3. 工具管理层

MCP 协议场景下的工具注册、发现、鉴权与执行。

**标准化接入**：工具用 `@mcp.tool()` 注册即自动发现，接入新业务系统无需改 Agent 代码。

**权限控制**：工具元数据声明权限要求 + 请求级权限参数**无状态透传**（登录态 → 会话 → 工具调用 → 数据检索全程透传，无文件共享、无跨进程可变状态，杜绝串户）。

**故障隔离**：单次调用超时控制；只重试瞬时故障；工具调用**进程隔离**（MCP stdio 子进程，子进程失败不影响主进程）。

> **状态归属是刻意的设计**：熔断器、限流器、对话历史这类需要跨请求累积的状态放在**主进程常驻**；MCP 子进程保持无状态、单次请求内失败靠降级兜底。把熔断器放进 per-request 子进程，状态每次归零，等于形同虚设。

## 4. 记忆管理层

决定 Agent 能否处理长任务、跨会话任务。

**短期记忆 —— 滚动摘要压缩**

对话超出窗口时不再"丢弃最早的消息"，而是由模型压缩成摘要：

- **滚动压缩**：旧摘要参与下一轮压缩，**摘要长度恒定**，不随对话增长而膨胀
- **保留原文窗口**：最近若干轮保留原文保真，只压缩更早部分
- **压缩后校验**：比对原文找遗漏 → 带缺失项**定向重压**一次
- **降级安全**：压缩不可用时回退原有硬截断，主链路不中断

**长期记忆 —— 持久化实体记忆**

| 机制 | 做法 |
|------|------|
| 存储 | 向量库（语义召回）+ 关系库（结构化字段与精确去重）双写 |
| 抽取 | 从**会话摘要**抽取（而非每轮原始对话）——每 N 轮一次，更省；摘要已滤掉寒暄，信噪比更高 |
| 去重 | **稳定实体 ID**（同用户 + 同归一化内容 → 同 ID），而非含时间戳的事件 ID |
| 强化 | 同一实体重复出现 → **权重正向累加**（`weight += delta`），而非删旧存新 |
| 改口 | 语义近似但事实已变 → **新值优先**，累积权重继承，旧记录作废 |
| 遗忘 | 时间衰减（超期线性降权），权重与置信度语义分离 |
| 召回 | 交叉编码器重排分做**相关性门槛**——不相关内容宁可不注入，也不硬凑条数 |
| 隔离 | 按用户隔离，独立于会话清理 |

> **一个实测结论**：本项目的嵌入模型上，纯向量分数**无法**区分相关与不相关（实测两组分布完全重叠），必须靠交叉编码器重排打分才能做出有效的相关性门槛。这是记忆检索里最关键的一环。

## 5. 可观测性层

生产系统排查问题的基础。

| 能力 | 说明 |
|------|------|
| 全链路 trace | 一次提问的所有决策轮次、工具调用、模型调用归并到同一个 trace_id |
| 工具审计 | 入参脱敏 + 结果摘要 + 耗时 + 成败 + 重试次数，结构化落盘 |
| 链路还原 | `trace_view.py` 按 trace 还原完整流程：每轮规划/推理/评估 + 每次工具调用的入参、结果、耗时 |
| 成本归因 | Token 与费用按调用点归因，定位"钱花在哪一步" |
| 熔断告警 | 轮询熔断器状态，状态迁移即告警（打开/恢复），三通道输出（日志 + 落盘 + webhook） |
| 端到端评测 | `agent_eval.py` 聚合工具调用成功率与端到端延迟 |
| LangFuse | 可视化全链路追踪（可选，默认关闭，降级安全） |

## 6. 安全层

| 层次 | 能力 |
|------|------|
| 入口 | 登录守卫 **fail-closed**（读与写分别守卫，未登录不能检索/上传） |
| 身份 | JWT 鉴权 + RBAC（用户 / 角色 / 分组） |
| 工具调用 | 工具级权限声明 + 数据检索按分组过滤 |
| 数据隔离 | 知识库按分组隔离；长期记忆按用户隔离 |
| 频率 | 两层令牌桶限流（用户级 + 模型调用级），Redis 缺失时 fail-open 不阻断主链路 |
| 密钥 | 全部走 `.env` 并纳入 gitignore，代码零硬编码 |

## 架构分层

```
┌─ 接入层 ──────────────────────────────────────────────────────┐
│  Streamlit 对话界面（含登录守卫）  │  Vue3 管理后台 + FastAPI   │
└───────────────────────────┬───────────────────────────────────┘
                            ▼
┌─ 核心层 ──────────────────────────────────────────────────────┐
│  UnifiedAgent（主进程常驻）                                    │
│    Skill 匹配 → ReAct 决策（五阶段）→ Judge → 工具调度          │
│    熔断 / 限流 / 预算 状态跨请求累积                            │
│  记忆管理：会话摘要压缩 + 长期实体记忆                          │
└───────────────────────────┬───────────────────────────────────┘
                            ▼  MCP stdio 子进程（per-request，无状态）
┌─ 工具层 ──────────────────────────────────────────────────────┐
│  mcp_server.py —— 7 个 MCP 工具，权限声明 + 审计 + 超时/重试    │
└───────────────────────────┬───────────────────────────────────┘
                            ▼
┌─ 基础设施层 ──────────────────────────────────────────────────┐
│  检索管线：缓存 → 改写 → 混合检索(BM25+向量 RRF) → 重排 → 生成  │
│  向量库 │ 关系库 │ Redis（缓存 + 限流） │ 嵌入/重排模型服务      │
└───────────────────────────┬───────────────────────────────────┘
                            ▼
┌─ 运维层 ──────────────────────────────────────────────────────┐
│  trace_id 贯穿 │ 审计落盘 │ 成本归因 │ 熔断告警 │ 链路还原      │
└───────────────────────────────────────────────────────────────┘
```

**全链路可降级**：查询改写、混合检索、重排、模型服务任一环节失败都无感回退（改写→原查询、混合→纯向量、重排→原序、模型服务→内嵌加载），主链路绝不中断。

## 快速开始

### 方式一：Docker 一键部署（推荐）

```bash
cd rag_assistant
docker build -t rag-assistant .     # 多阶段：Node 构建前端 + Python 运行时
docker compose up -d                # 起 5 个服务
```

| 服务 | 地址 | 说明 |
|------|------|------|
| 对话界面 | http://localhost:8501 | Streamlit 主服务 |
| 管理后台 + 权限后端 | http://localhost:8000 | FastAPI（JWT + RBAC）+ Vue3 静态文件 |
| 嵌入服务 | http://localhost:8001 | 嵌入模型独立服务 |
| 重排服务 | http://localhost:8002 | 重排模型独立服务 |
| Redis | localhost:6379 | 检索缓存 + 限流令牌桶 |

停止：`docker compose down`。模型缓存、向量库、运行产物均挂载宿主机持久化。

### 方式二：本地开发运行

```bash
cd rag_assistant
venv\Scripts\activate.bat          # Windows（Mac/Linux: source venv/bin/activate）
pip install -r requirements.txt

# 模型服务（建议启动：不启动则各进程内嵌加载模型，主进程会多吃数百 MB 内存）
python embed_server.py             # :8001
python rerank_server.py            # :8002

streamlit run app.py               # :8501
```

> ⚠️ **若 `.env` 中配置了 `EMBED_SERVER_URL` / `RERANK_SERVER_URL`，就必须先启动对应服务**——服务未启动时嵌入调用会直接失败（不回落本地），重排会降级到本地加载模型。

### 方式三：管理后台（前端热更新）

```bash
uvicorn api_server:app --port 8000          # 先启动权限后端
cd frontend && npm install && npm run dev   # 前端 → http://localhost:5173
```

## 配置

`.env` 中配置密钥与可选服务（缺失的 Key 对应功能自动降级）：

```env
OPENWEATHER_API_KEY=你的OpenWeatherMap密钥
GROQ_API_KEY=你的DeepSeek密钥          # 变量名历史遗留，实际指向 DeepSeek
EMBED_SERVER_URL=http://localhost:8001  # 可选：模型服务化
RERANK_SERVER_URL=http://localhost:8002
ALERT_WEBHOOK_URL=                      # 可选：告警 webhook（钉钉/飞书）
```

`config.py` 中切换模型与检索策略：

```python
LLM_MODEL = "deepseek-flash"
HYBRID_ENABLED = True                # 混合检索
QUERY_REWRITE_ENABLED = True         # 查询改写
SESSION_SUMMARY_ENABLED = True       # 会话摘要压缩
MEMORY_RERANK_ENABLED = True         # 记忆检索相关性过滤
LANGFUSE_ENABLED = False             # 可观测（需自托管服务）
```

> 可选能力均为「缺失即降级」：Redis 缺失 → 缓存跳过、限流放行；LangFuse 缺失 → 全 no-op；模型服务缺失 → 内嵌加载。

## 命令行工具

```bash
# 知识库运维
python kb_manager.py status                     # 状态
python kb_manager.py add-dir <目录> --recursive  # 批量导入
python kb_manager.py repair                     # 校验一致性
python kb_manager.py rollback --list            # 快照回退

# 可观测
python trace_view.py --last                     # 还原最近一次提问的完整链路
python trace_view.py --failed                   # 只看有工具失败的请求

# 长期记忆
python long_term_memory.py migrate --dry-run    # 存量记忆迁移（先空跑）

# 评测
python rag_eval/run_eval.py --config both --limit 2   # RAGAS A/B 对照
python agent_eval.py --report                         # Agent 端到端

# 调试
npx @modelcontextprotocol/inspector python mcp_server.py   # MCP Inspector
python -c "from agent import Agent; a = Agent(); print(a.chat('北京天气？'))"
```

## 目录结构

```
rag_assistant/
├── app.py                    # Streamlit 对话界面 + 登录守卫
├── api_server.py             # FastAPI 权限后端（JWT + RBAC + 审计 + 限流）
├── mcp_server.py             # MCP 工具层（7 个工具）
├── agent.py                  # Agent 入口（薄封装）
├── config.py                 # 全局配置
│
├── mcp_unified_agent/        # 智能体核心
│   ├── unified_agent.py      # 编排调度 + 会话摘要压缩 + 预算检查
│   ├── decision_engine.py    # ReAct 五阶段决策引擎
│   ├── scheduler.py          # 工具串行/并行执行
│   ├── circuit_breaker.py    # 熔断器 + 统一 LLM 入口
│   ├── reflection_memory.py  # 反思记忆（短期工具选型）
│   ├── skill_registry.py     # Skill 注册表（关键词 + 向量匹配）
│   └── skill_executor.py     # Skill 执行器
├── judge.py                  # 独立 Judge 模型
├── skills/builtin/           # 内置 Skill
├── frontend/                 # Vue3 + Element Plus 管理后台
│
├── retriever.py              # RAG 主链路（缓存→改写→混合→重排→生成）
├── hybrid_retriever.py       # BM25 + 向量混合检索（RRF 融合）
├── reranker.py               # BGE-Reranker 重排（检索与记忆复用）
├── query_rewriter.py         # 查询改写（指代消解/多查询/HyDE）
├── vector_store.py           # 向量存储（增量/权限/快照）
├── long_term_memory.py       # 长期记忆（双写 + 实体 ID + 权重强化 + 改口覆盖）
├── document_loader.py        # 文档解析（PDF/Word + OCR 降级）
├── kb_manager.py             # 知识库运维 CLI
│
├── embed_server.py           # 嵌入模型服务（:8001）
├── rerank_server.py          # 重排模型服务（:8002）
│
├── rate_limiter.py           # Redis 令牌桶限流
├── search_cache.py           # Redis 检索缓存
├── token_tracker.py          # Token 用量追踪 + 成本计算（峰谷计价）
├── tool_audit.py             # 工具调用审计
├── trace_view.py             # 请求链路查看器
├── alerting.py               # 熔断告警（日志 + 落盘 + webhook）
├── model_gateway.py          # 模型路由 + 单任务 Token 预算
├── observability.py          # LangFuse 可观测（降级安全）
│
├── rag_eval/                 # RAGAS 评测体系
├── agent_eval.py             # Agent 端到端评测
├── 技术文档/                  # STAR 技术文档（架构决策 + 踩坑记录）
├── test_*.py                 # 独立验证脚本（假模型，零 API 成本）
│
├── Dockerfile
├── docker-compose.yml        # 五服务编排
└── requirements.txt
```

## 技术栈

Python / FastAPI / Vue3 / ChromaDB / Redis / Docker / MCP / RAGAS / LangFuse
