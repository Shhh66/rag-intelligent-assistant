# MCP 可控智能体平台 —— 私有知识问答与业务自动化

基于 **MCP 协议**构建的企业级 RAG + Agent 智能体平台。核心理念：**Agent 不关心工具内部实现，只需一套标准协议即可将任意业务系统接入为可调用的工具**。目前已接入知识库检索、天气查询、教务课表查询等工具，展示 Agent 自主发现工具、判断意图、编排调用的完整链路。

对标企业落地三大痛点：**大模型私有资料幻觉、Agent 调用无管控、审计缺失**，除核心 RAG / Agent 能力外，还落地了权限管控、熔断限流、可观测、自动化评测、容器化部署等一整套生产级能力。

**换个工具集就能换一个业务场景**：天气查询换成订单 API → 客服助手；接入监控日志 → 运维自诊断；接入 SQL 引擎 → 业务自助查数。新增工具只需注册一个 MCP 接口，Agent 代码零改动。

## 核心特性

- **三层架构**：MCP 工具层（标准化接入）+ Skills 技能层（关键词 + 向量双重匹配）+ ReAct 推理层（自研，支持多工具并行）
- **端到端 RAG**：文档解析（PDF/Word/OCR）→ 混合检索（BM25 + 向量 RRF）→ 重排（BGE-Reranker）→ 查询改写（指代消解/双语）→ LLM 生成，全链路可降级
- **企业级能力**：权限隔离（分组 + 工具粒度鉴权）、熔断/限流/重试容错、Redis 缓存、全链路可观测（trace_id + LangFuse）、Token 成本统计、长期记忆
- **自动化评测**：RAGAS + LLM-as-a-Judge（A/B 对照 + Bad Case 归因）+ Agent 端到端评测（工具调用成功率 + 端到端延迟）
- **管理后台**：Vue3 + Element Plus（用户/角色/分组/文档/审计，RBAC 权限控制）
- **容器化部署**：Docker 四服务编排，一键启动

## 架构

```
┌─ Streamlit (app.py) ── 对话界面 + 文档上传 + 登录守卫 ──────────┐
├─ Vue3 管理后台 (frontend/) ── 用户/角色/分组/审计 ───────────────┤
│                                                                │
│      ┌─ FastAPI (api_server.py) ── JWT + SQLite + RBAC ──┐     │
│      └────────────────────────────────────────────────────┘     │
▼                          ▼                                      │
┌───────────────────────────────────────────────────────────────┐│
│  UnifiedAgent（智能体核心，主进程常驻）                          ││
│  Skill 匹配 → ReAct 推理 → 工具调度（并行/串行）                 ││
└──────────┬────────────────────────────────────────────────────┘│
           │ MCP stdio 子进程（per-request）                       │
           ▼                                                      │
┌───────────────────────────────────────────────────────────────┐│
│  mcp_server.py（7 个 MCP 工具）                                 ││
│  query_weather / ask_knowledge_base / search_knowledge_base     ││
│  check_kb_status / debug_rerank / clear_memory / edu_query_schedule │
└──────────┬────────────────────────────────────────────────────┘│
           │ 检索管线（缓存→改写→混合→重排→生成，全降级）           │
           ▼                                                      │
   Redis(缓存+限流)  embed_server(:8001)  rerank_server(:8002)    │
```

## 一键运行

### 方式一：Docker 一键部署（推荐）

```bash
cd rag_assistant
docker build -t rag-assistant .     # 构建镜像（首次）
docker compose up -d                # 起 4 个服务
```

| 服务 | 地址 | 说明 |
|------|------|------|
| 对话界面 | http://localhost:8501 | Streamlit 主服务 |
| 权限后端 | http://localhost:8000 | FastAPI（JWT + RBAC） |
| 嵌入服务 | http://localhost:8001 | 嵌入模型独立服务 |
| 重排服务 | http://localhost:8002 | 重排模型独立服务 |

停止：`docker compose down`。模型缓存与向量库均挂载宿主机 D 盘，数据持久化。

### 方式二：本地开发运行

```bash
cd rag_assistant
venv\Scripts\activate.bat          # Windows（Mac/Linux: source venv/bin/activate）
pip install -r requirements.txt

# 启动模型服务（可选：不启动则主进程内嵌加载模型）
python embed_server.py             # :8001
python rerank_server.py            # :8002

# 启动对话界面
streamlit run app.py               # :8501
```

### 方式三：管理后台（前端）

```bash
uvicorn api_server:app --port 8000     # 先启动权限后端
cd frontend && npm install && npm run dev   # 前端 → http://localhost:5173
```

## 配置

在 `.env` 中配置 API Key（缺失的 Key 对应功能自动降级）：

```env
OPENWEATHER_API_KEY=你的OpenWeatherMap密钥
GROQ_API_KEY=你的DeepSeek密钥    # 变量名历史遗留，实际指向 DeepSeek
```

在 `config.py` 中可切换 LLM 后端与检索策略：

```python
GROQ_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-flash"
HYBRID_ENABLED = True          # 混合检索开关
QUERY_REWRITE_ENABLED = True   # 查询改写开关
```

> 可选能力：Redis（检索缓存 + 限流，缺失自动降级关闭）、LangFuse（可观测，`LANGFUSE_ENABLED` 控制）、ChromaDB 服务化（`CHROMA_SERVER_URL`）。

## 目录结构

```
rag_assistant/
├── app.py                  # Streamlit 对话界面 + 登录守卫
├── api_server.py           # FastAPI 权限后端（JWT + RBAC + 审计）
├── mcp_server.py           # MCP 工具层（7 个工具，FastMCP）
├── agent.py                # Agent 入口（薄封装 UnifiedAgent）
├── config.py               # 全局配置
│
├── mcp_unified_agent/      # 智能体核心
│   ├── unified_agent.py    # 编排调度
│   ├── decision_engine.py  # ReAct 决策引擎（Thought→Action→Observation）
│   ├── scheduler.py        # 串行/并行工具执行
│   ├── tool_registry.py    # 工具元数据 + 权限声明
│   ├── skill_registry.py   # Skill 注册表（关键词+向量匹配）
│   ├── skill_executor.py   # Skill 执行器（参数校验 + 降级）
│   └── circuit_breaker.py  # 熔断器 + call_llm_with_cb 统一 LLM 入口
├── skills/builtin/         # 内置 Skill（综合查询/天气建议/深度检索）
├── frontend/               # Vue3 + TypeScript + Element Plus 管理后台
│
├── retriever.py            # RAG 检索主链路（缓存→改写→混合→重排→生成）
├── hybrid_retriever.py     # BM25 + 向量混合检索（RRF 融合）
├── bm25_index.py           # BM25 关键词索引（签名驱动懒重建）
├── reranker.py             # BGE-Reranker-v2-m3 重排
├── query_rewriter.py       # 查询改写（指代消解/多查询/HyDE）
├── vector_store.py         # ChromaDB 向量存储（增量/权限/快照）
├── kb_manager.py           # 知识库运维 CLI（10+ 命令）
├── document_loader.py      # 文档解析（PDF/Word + OCR 降级）
├── text_splitter.py        # Markdown 标题切分 + 递归二次切分
│
├── embed_server.py         # 嵌入模型服务（:8001）
├── rerank_server.py        # 重排模型服务（:8002）
│
├── rate_limiter.py         # Redis 令牌桶限流（两层）
├── search_cache.py         # Redis 检索缓存
├── token_tracker.py        # Token 用量追踪 + 成本计算
├── tool_audit.py           # 工具调用审计（入参脱敏 + trace_id）
├── observability.py        # LangFuse 可观测
├── long_term_memory.py     # 长期记忆（Chroma 语义 + SQLite 结构化）
│
├── rag_eval/               # RAGAS 评测体系（A/B 对比 + 4 指标）
├── agent_eval.py           # Agent 端到端评测（工具成功率 + 端到端延迟）
├── 技术文档/               # STAR 技术文档（架构决策 + 踩坑记录）
│
├── Dockerfile              # 镜像构建
├── docker-compose.yml      # 四服务编排
└── requirements.txt        # 依赖清单
```

## 评测

```bash
# RAG 检索质量（RAGAS，A/B 对比）
python rag_eval/run_eval.py --config both --limit 2

# Agent 端到端（工具调用成功率 + 端到端延迟，读 tool_audit.jsonl）
python agent_eval.py --report
```

## 命令行调用

```bash
python -c "from agent import Agent; a = Agent(); print(a.chat('北京天气？'))"
```

## MCP Inspector 调试

```bash
npx @modelcontextprotocol/inspector python mcp_server.py
```

## 技术栈

Python / FastAPI / Vue3 / ChromaDB / Redis / Docker / MCP / RAGAS / LangFuse
