# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

始终使用中文回复。

## 环境约定（重要）

**本项目安装的所有依赖都必须安装在 D 盘，禁止占用 C 盘。**
- Python 依赖装入 D 盘的项目 venv：`d:\VsCode\AI\rag_assistant\venv`（`pip install` 前先激活它）。
- pip 下载缓存与临时解压目录指向 D 盘：`PIP_CACHE_DIR=D:\VsCode\AI\.cache\pip`、`TMPDIR/TEMP/TMP=D:\VsCode\AI\.cache\tmp`（或 `pip install --cache-dir D:\VsCode\AI\.cache\pip`）。
- HuggingFace / 模型缓存指向 D 盘：设置 `HF_HOME`（如 `D:\VsCode\AI\.cache\huggingface`），避免模型下载到 C 盘用户目录。
- 安装任何新依赖前确认目标路径在 D 盘，安装后核对未在 C 盘新增占用。

## 项目概述

基于 **MCP 协议**构建的可扩展 LLM 智能体框架。核心理念：Agent 不关心工具内部实现，只需一套标准协议即可将**任意业务系统**接入为可调用的工具。已接入 RAG 知识库检索、天气查询、教务系统，展示了 Agent 自主发现工具、判断意图、编排调用的完整链路。

**三层架构**（均已实现）：
- **MCP 工具层**：`@mcp.tool()` 注册，自动发现，标准化接入
- **Skills 技能层**：声明式字典定义，关键词+向量双重匹配，失败无感降级 ReAct
- **ReAct 推理层**：自研轻量引擎，LLM 自主 Thought→Action→Observation 循环，保留多工具并行

**企业级能力**（部署运维改造 P0–P3 已落地）：模型服务化（embed/rerank 独立 HTTP 服务）、权限无状态透传、熔断器（per-destination）、限流（两层令牌桶）、检索缓存（Redis）、向量库服务化（Client/Server）、容器化（Docker 4 服务编排）、可观测（审计/LangFuse）、RAGAS 评测。详见 [企业级RAG架构.md](rag_assistant/技术文档/企业级RAG架构.md)。

## 常用命令

```bash
cd rag_assistant
venv\Scripts\activate.bat

# === 对话界面 (Streamlit) ===
streamlit run app.py

# === 管理后台 (Vue 3) ===
uvicorn api_server:app --port 8000          # 后端 API（必须先启动）
cd frontend && npm run dev                  # 前端 → http://localhost:5173

# === 模型独立服务（可选，多实例共享；不启动则内嵌进程加载）===
python embed_server.py                      # 嵌入服务 → :8001
python rerank_server.py                     # 重排服务 → :8002

# === Docker 部署（4 服务编排）===
docker build -t rag-assistant .             # 构建镜像（先于 compose）
docker compose up -d                        # 起 api-server + embed-server + rerank-server + main

# === Redis（检索缓存 + 限流，缺失则降级关闭）===
docker run -d --name redis -p 6379:6379 redis

# === 调试工具 ===
npx @modelcontextprotocol/inspector python mcp_server.py    # MCP Inspector
python -c "from agent import Agent; a = Agent(); print(a.chat('北京天气？'))"

# === 向量库运维 ===
python kb_manager.py status                          # 知识库状态
python kb_manager.py list                            # 文档清单
python kb_manager.py add <文件>                      # 增量添加
python kb_manager.py remove "<相对路径>"              # 删除文档
python kb_manager.py add-dir <目录> --recursive      # 批量导入
python kb_manager.py repair                          # 校验 Chroma ⇔ db_meta 一致性
python kb_manager.py rollback --list                 # 列出快照
python kb_manager.py rollback <时间戳>               # 回退到指定快照
python kb_manager.py clear --yes                     # 清空知识库

# === RAG 评测 (RAGAS) ===
python rag_eval/run_eval.py --config both            # 完整 A/B 评测(基线 vs 优化)
python rag_eval/run_eval.py --config both --limit 2  # 小样本快跑(省 Token,先验证链路)
python rag_eval/run_eval.py --config both --save-baseline  # 并保存基线快照
python rag_eval/adapters.py                          # 自测 RAGAS 适配器(LLM/嵌入包装)

# === 可观测性 (LangFuse 自托管，可选) ===
docker compose -f rag_assistant/docker-compose.langfuse.yml up -d   # 起 LangFuse → http://localhost:3000
# 注册账号→建 project→拿 pk/sk 填 .env→config.LANGFUSE_ENABLED=True
```

## 核心架构

```
┌─ Streamlit (app.py) ─── 用户对话 + 文档上传 + 登录守卫(fail-closed) ┐
├─ Vue3 管理后台 (frontend/) ─── 用户/角色/分组/审计 ────────────────┤
│                                                                    │
│        ┌─ FastAPI (api_server.py) ─┐                               │
│        │ JWT + SQLite + 用户级限流   │                               │
│        └───────────────────────────┘                               │
▼                              ▼                                      │
┌──────────────────────────────────────────────────────────────────┐│
│  UnifiedAgent (mcp_unified_agent/, 主进程常驻)                    ││
│  Skill 匹配 → ReAct 推理 → 工具调度(并行/串行)                     ││
│  decision_engine/skill_executor 的 LLM 调用走 call_llm_with_cb     ││
│    └─ call_llm_with_cb ──[限流→熔断]──▶ DeepSeek                   ││
│  （熔断器/限流器状态在主进程常驻，跨请求累积）                        ││
└──────────┬───────────────────────────────────────────────────────┘│
           │ MCP stdio 子进程(per-request，每次 chat 新建)            │
           ▼                                                         │
┌──────────────────────────────────────────────────────────────────┐│
│  mcp_server.py (7 个 MCP 工具)                                    ││
│  query_weather / ask_knowledge_base / search_knowledge_base        ││
│  check_kb_status / debug_rerank / clear_memory / edu_query_schedule││
└──────────┬───────────────────────────────────────────────────────┘│
           │ 子进程无状态，失败靠 fallback 兜底                        │
           ▼                                                         │
┌────────────────────────────────┐                                   │
│  RAG 检索管线（全降级）          │                                   │
│  缓存(Redis,命中跳过) → 改写    │                                   │
│  → 混合(BM25+向量 RRF)         │                                   │
│  → 重排(BGE) → LLM 生成        │                                   │
└────────────────────────────────┘                                   │
   │              │              │
   ▼              ▼              ▼
Redis(缓存+限流)  embed_server   rerank_server
                  (HTTP :8001)   (HTTP :8002)
```

**进程模型（状态归属是刻意的设计）**：
- 主进程（常驻）：熔断器、限流器、对话历史、反思记忆——状态跨请求累积
- MCP 子进程（per-request）：无状态，单次请求内失败靠 fallback 兜底
- 模型服务（HTTP）：多实例共享一份模型权重
- Redis：分布式共享状态（检索缓存 + 限流令牌桶）

## 文件职责

| 文件 | 角色 |
|------|------|
| `mcp_unified_agent/` | **智能体核心**：MCP 客户端、ReAct 决策引擎、Skill 注册表/执行器、并行调度器、反思记忆 |
| `mcp_unified_agent/circuit_breaker.py` | **熔断器**：per-destination 三态状态机 + `call_llm_with_cb` 统一 LLM 入口（限流+熔断，只在常驻进程用） |
| `skills/builtin/` | 3 个内置 Skill：综合查询、天气建议、深度检索（声明式字典，自动发现） |
| `mcp_server.py` | **MCP 工具层**：FastMCP 服务，`@mcp.tool()` 注册 7 个工具 |
| `embed_server.py` | 嵌入模型独立服务（HTTP `/embed` + `/ready`，多实例共享 420MB 模型） |
| `rerank_server.py` | 重排模型独立服务（HTTP `/rerank` + `/ready`，568MB 模型） |
| `agent.py` | 薄封装 `class Agent(UnifiedAgent)`，供 app.py 使用 |
| `app.py` | Streamlit 对话界面 + 登录守卫 + 文档上传 |
| `api_server.py` | **FastAPI 权限后端**：JWT 鉴权、用户/角色/分组 CRUD、审计日志、用户级限流 |
| `frontend/` | **Vue 3 + Element Plus 管理后台**：用户/角色/分组/文档/审计 |
| `vector_store.py` | ChromaDB 增量增删改、快照备份/回退、权限过滤、元数据索引、`_get_client()` 工厂（PersistentClient/HttpClient） |
| `kb_manager.py` | CLI 运维工具：10+ 命令管理向量库全生命周期 |
| `retriever.py` | RAG 检索主链路：缓存 → 改写 → 混合检索(双语) → 合并去重 → 重排 → Prompt；`answer_with_fallback`(生产)与 `retrieve_and_answer`(评测) |
| `query_rewriter.py` | 查询改写：clarify/multi/hyde 三模式 + LRU 缓存 + 埋点 + 无感降级 |
| `hybrid_retriever.py` | 混合检索：BM25 稀疏 + 向量稠密双通道，RRF 加权融合，异常降级纯向量 |
| `bm25_index.py` | BM25 关键词索引：jieba 分词 + 停用词过滤 + 内容签名驱动懒重建 + pkl 持久化 |
| `reranker.py` | BGE-Reranker-v2-m3 Cross-Encoder 重排 + LLM 降级兜底 |
| `document_loader.py` | 文档解析：PyMuPDF(PDF) + python-docx(DOCX) + PaddleOCR 降级通道 |
| `text_splitter.py` | Markdown 标题切分 + 递归二次切分 + 小章节合并 |
| `ocr_processor.py` | PaddleOCR/PP-Structure 懒加载 + 公式识别三级降级链 |
| `token_tracker.py` | Token 用量追踪：会话累积 + 文件持久化 + 成本计算；`set_trace_id()` + `record()` 上报 LangFuse |
| `long_term_memory.py` | **长期记忆**：跨会话实体记忆(Chroma 语义 + SQLite 结构化)，LLM 抽取 + 按用户隔离 + 去重/权重/时间衰减 |
| `tool_audit.py` | **工具调用审计**：trace_id/入参脱敏/结果摘要/耗时/成败/重试数 → `tool_audit.jsonl` |
| `observability.py` | **LangFuse 可观测**：全链路 span/generation 封装，降级安全(未启用/服务未起全 no-op) |
| `search_cache.py` | **Redis 检索缓存**：key=md5(query+排序 kb_groups)，TTL 自动过期，权限维度进 key 防串台 |
| `rate_limiter.py` | **Redis 令牌桶限流**：两层（用户级 `rate:user` + LLM 级 `rate:llm`），Lua 原子操作，fail-open |
| `rag_eval/` | **RAGAS 评测体系**：A/B 对比、4 指标量化、分层 + Bad Case + 成本报告 |
| `config.py` | 全局配置：LLM/嵌入/重排/权限/熔断/限流/缓存/PDF 阈值/公式等所有配置项 |

## 关键设计决策

- **MCP 通信**：`UnifiedAgent` 通过 stdio 子进程启动 `mcp_server.py`，用 `async with stdio_client` 管理生命周期（Windows 不能用嵌套 `__aenter__`）
- **ReAct 自研替换 LangChain**：LangChain ReAct 天然串行，自研版本保留并行能力——"查天气+查知识库"同时调
- **Skill 匹配策略**：关键词+向量双重匹配 → Top3 交 LLM 确认 → 命中 1 轮执行 → 未命中走 ReAct → 失败无感降级
- **跨进程权限传递（无状态）**：`kb_groups` 请求级参数从登录态 → session_state → `chat(kb_groups=...)` → Scheduler → MCP 工具 → `search` 一路透传（None=不限权限，无文件共享、无跨进程可变状态）
- **熔断器放主进程，不放子进程**：子进程 per-request、状态每次归零，熔断形同虚设。「状态能跨请求累积」是比「离故障源近」更前置的前提。主进程 decision_engine(4处) + skill_executor(2处) 走 `call_llm_with_cb`；`mcp_client_manager.call_tool` 的熔断只保护「MCP 通道可用性」
- **容错三件套正交**：重试(tenacity)管单次瞬时抖动、熔断(circuit_breaker)管下游故障、限流(rate_limiter)管上游频率。熔断 fail-closed（故障必须快速失败），限流/缓存 fail-open（Redis 挂了放行，不阻断主链路）
- **fallback vs 熔断职责边界**：子进程单次请求内失败靠 fallback 兜底（检索失败→直答、翻译失败→跳过），不接熔断（无状态基础）
- **精准备份而非全库拷贝**：删除/更新/清空前，只备份即将被删的 chunk 数据（ids+documents+metadatas+embeddings），回退时精确恢复
- **快慢双通道 PDF 解析**：90% PDF 走 PyMuPDF 快速通道（多栏检测+标题聚类+页眉过滤），质量差的才启动 PaddleOCR（500MB+，懒加载）
- **混合检索分工**：`hybrid_retriever` 用 RRF 融合 BM25+向量两路召回（只用排名不用绝对分数，回避余弦/BM25 分数不可比），融合后仍交 `reranker` 精排——RRF 负责"合成候选"，Cross-Encoder 负责"精排"
- **BM25 索引跨进程一致**：`bm25_index` 用"chunk 数+内容 hash+权限 metadata"签名驱动懒重建，增删文档或权限变更后自动感知重建
- **改写/混合/评测全程可降级**：查询改写、混合检索、重排任一失败都无感回退（改写→原 query、混合→纯向量、Cross-Encoder→LLM→原序），主链路绝不中断
- **按规模演进**：单机 PersistentClient → 多实例 HttpClient（`CHROMA_SERVER_URL`）→ 百万级再换 Milvus。每一步「到了那个规模才做那个事」
- **Python 解释器**：自动检测 venv Python，确保 MCP 子进程使用正确环境
- **`.env` 加载**：`config.py` 用 `Path(__file__).resolve().parent / ".env"` 绝对路径

## 重要注意事项

- **零破坏原则**：所有改造遵循「不配置新参数/不传新参数时，默认行为与改造前一致」——模型服务留空走本地、Redis 缺失降级关闭、`kb_groups=None` 不限权限
- **嵌入模型预加载**：`mcp_server.py` 启动时加载 `paraphrase-multilingual-MiniLM-L12-v2`（420MB），避免首次查询超时
- **重排模型**：BGE-Reranker-v2-m3（568MB），首次加载需 10-30s，`RERANK_MIN_SCORE=-999.0` 表示不过滤（BGE logits 非 0-1 概率）
- **API 超时**：所有 `OpenAI()` 和 `httpx` 调用设了 timeout；`max_tokens=4000`（安全线，非成本控制）
- **Windows 兼容**：MCP 子进程必须 `async with stdio_client`；文件路径统一正斜杠
- **入口 fail-closed**：`app.py` 登录守卫（读）+ 上传守卫（写），未登录不能检索/上传。信任边界：MCP 工具默认 `kb_groups=None` 可伪造，stdio 内部可控，暴露 HTTP/SSE 需网关鉴权
- **filelock 并发控制**：所有 ChromaDB 写操作前获取全局文件锁 `chroma_db/kb.lock`（30s 超时）
- **ChromaDB 稳定 ID**：`chunk_id = {file_hash}_{index:04d}`，保证增量添加天然幂等
- **source 兜底**：查 ChromaDB 时优先 `file_path`，无匹配兜底 `source`（basename），兼容老数据
- **前端代理**：`vite.config.ts` 将 `/api` 代理到 `localhost:8000`，开发时无需跨域配置
- **RAGAS + DeepSeek 兼容**：DeepSeek 只支持 `n=1`，RAGAS 部分指标默认多候选采样(`n>1`)会报 `400 Invalid n value`；解法是 `rag_eval/adapters.py` 里对 `LangchainLLMWrapper` 设 `bypass_n=True`
- **RAGAS 评测清洗**：faithfulness 会把答案里的来源标注/免责声明误判为幻觉；`run_eval.py` 的 `clean_answer_for_eval()` 在打分前清洗这些格式(仅作用于评测副本)
- **推理模型 max_tokens 陷阱（重要）**：`LLM_MODEL=deepseek-v4-flash` 是推理模型，先输出 `reasoning_content` 再出正式 `content`；`max_tokens` 过小(如 50/120)会导致 token 被推理耗尽、`content` 返回空串。凡是"输出短但要它真回话"的调用(查询翻译/改写等)必须给足 `max_tokens`(≥512)
- **RAGAS 打分防超时**：`run_eval.py` 的 `run_ragas()` 传 `RunConfig(timeout=180, max_workers=4, max_retries=3)`——DeepSeek 高并发下易超时，超时的 Job 会以 NaN 污染指标均值；降并发换干净数据
- **混合检索/查询改写开关**：`HYBRID_ENABLED` / `QUERY_REWRITE_ENABLED`(config.py) 控制是否启用；BM25 索引持久化在 `bm25_index.pkl`，停用词表 `bm25_stopwords.txt`
- **工具调用统一重试**：`mcp_client_manager.call_tool` 用 tenacity 指数退避，只重试瞬时故障(超时/连接)，业务错误(isError)/参数校验失败不重试；`_execute_one` 已去掉外层 `wait_for`(超时+重试下沉到 call_tool)
- **trace_id 贯穿**：`unified_agent._run()` 生成 `uuid4().hex`，透传 Scheduler/SkillExecutor + `token_tracker.set_trace_id()`，一问多工具在审计与 LangFuse 里归并同一链路
- **protobuf 冲突(重要)**：`langfuse` 会把 protobuf 拉到 7.x，但 `paddlepaddle 2.6.2` 要求 `protobuf<=3.20.2`(否则 paddle/OCR 崩)；装完 langfuse 必须 `pip install "protobuf<=3.20.2"` 回退，langfuse 走 HTTP 不受影响
- **LangFuse 默认关**：`LANGFUSE_ENABLED=False`；observability.py 全程降级安全(未启用/缺密钥/服务未起均 no-op)
- **长期记忆按用户隔离**：`chat(user_id=...)` → `self._user_id` 实例属性(请求级，无文件共享)，`long_term_memory` 按 user_id 过滤。独立于 `clear_memory()`(清会话不清长期记忆)
- **长期记忆检索用距离分**：本地嵌入的 `similarity_search_with_relevance_scores` 会返回负值，改用 `similarity_search_with_score` 距离分转 `1/(1+dist)`；抽取 LLM 调用 `max_tokens≥512`
- **Redis 依赖**：检索缓存 + 限流都依赖 Redis，缺失时降级关闭（缓存跳过、限流放行），绝不阻断主链路
