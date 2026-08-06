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

基于 **MCP 协议**构建的可扩展 LLM 智能体框架。核心理念：Agent 不关心工具内部实现，只需一套标准协议即可将**任意业务系统**接入为可调用的工具。已接入 RAG 知识库检索和天气查询，展示了 Agent 自主发现工具、判断意图、编排调用的完整链路。

**三层架构**（均已实现）：
- **MCP 工具层**：`@mcp.tool()` 注册，自动发现，标准化接入
- **Skills 技能层**：声明式字典定义，关键词+向量双重匹配，失败无感降级 ReAct
- **ReAct 推理层**：自研轻量引擎，LLM 自主 Thought→Action→Observation 循环，保留多工具并行

## 常用命令

```bash
cd rag_assistant
venv\Scripts\activate.bat

# === 对话界面 (Streamlit) ===
streamlit run app.py

# === 管理后台 (Vue 3) ===
uvicorn api_server:app --port 8000          # 后端 API（必须先启动）
cd frontend && npm run dev                  # 前端 → http://localhost:5173

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
┌─ Streamlit (app.py) ─── 用户对话 + 文档上传 ──────────────┐
├─ Vue3 管理后台 (frontend/) ─── 用户/角色/分组/审计 ───────┤
│                                                          │
│              ┌─ FastAPI (api_server.py) ─┐                │
│              │ JWT + SQLite + 权限鉴权    │                │
│              └───────────────────────────┘                │
│                         │                                  │
▼                         ▼                                  │
┌──────────────────────────────────────────────────────────┐│
│  UnifiedAgent (mcp_unified_agent/)                       ││
│  Skill 匹配(前置) → ReAct 推理 → 工具调度(并行/串行)      ││
│  关键子模块: decision_engine, skill_registry, scheduler   ││
└──────────┬───────────────────────────────────────────────┘│
           │ MCP stdio 子进程                                │
           ▼                                                 │
┌──────────────────────────────────────────────────────────┐│
│  mcp_server.py (6 个 MCP 工具)                            ││
│  query_weather / ask_knowledge_base / search_knowledge_base│
│  check_kb_status / clear_memory / debug_rerank            ││
└──────────┬───────────────────────────────────────────────┘│
           │                                                 │
           ▼                                                 │
┌──────────────────────────────┐                             │
│  RAG 检索管线                 │                             │
│  document_loader → splitter  │                             │
│  → ChromaDB ┐                │                             │
│  query_rewriter(改写)         │                             │
│  → hybrid_retriever          │                             │
│    (BM25 + 向量 RRF 融合)     │                             │
│  → reranker → LLM 生成       │                             │
└──────────────────────────────┘                             │
```

## 文件职责

| 文件 | 角色 |
|------|------|
| `mcp_unified_agent/` | **智能体核心**：MCP 客户端、ReAct 决策引擎、Skill 注册表/执行器、并行调度器、反思记忆 |
| `skills/builtin/` | 3 个内置 Skill：综合查询、天气建议、深度检索（声明式字典，自动发现） |
| `mcp_server.py` | **MCP 工具层**：FastMCP 服务，`@mcp.tool()` 注册所有工具 |
| `agent.py` | 薄封装 `class Agent(UnifiedAgent)`，供 app.py 使用 |
| `app.py` | Streamlit 对话界面 + 登录鉴权 + 文档上传 |
| `api_server.py` | **FastAPI 权限后端**：JWT 鉴权、用户/角色/分组 CRUD、审计日志 |
| `frontend/` | **Vue 3 + Element Plus 管理后台**：用户/角色/分组/文档/审计 |
| `vector_store.py` | ChromaDB 增量增删改、快照备份/回退、权限过滤、元数据索引 |
| `kb_manager.py` | CLI 运维工具：10+ 命令管理向量库全生命周期 |
| `retriever.py` | RAG 检索主链路：查询改写 → 混合检索(双语) → 合并去重 → 重排 → Prompt 构建；`answer_with_fallback`(生产)与 `retrieve_and_answer`(评测,带 A/B 开关) |
| `query_rewriter.py` | 查询改写：clarify(规范化+指代消解)/multi(多查询)/hyde 三模式 + LRU 缓存 + 埋点 + 无感降级 |
| `hybrid_retriever.py` | 混合检索：BM25 稀疏 + 向量稠密双通道，RRF 加权融合，异常降级纯向量 |
| `bm25_index.py` | BM25 关键词索引：jieba 分词 + 停用词过滤 + 内容签名驱动懒重建 + pkl 持久化 |
| `reranker.py` | BGE-Reranker-v2-m3 Cross-Encoder 重排 + LLM 降级兜底 |
| `document_loader.py` | 文档解析：PyMuPDF(PDF) + python-docx(DOCX) + PaddleOCR 降级通道 |
| `text_splitter.py` | Markdown 标题切分 + 递归二次切分 + 小章节合并 |
| `ocr_processor.py` | PaddleOCR/PP-Structure 懒加载 + 公式识别三级降级链 |
| `token_tracker.py` | Token 用量追踪：会话累积 + 文件持久化 + 成本计算；`set_trace_id()` + `record()` 上报 LangFuse generation |
| `long_term_memory.py` | **长期记忆**：跨会话实体记忆(Chroma 语义 + SQLite 结构化)，LLM 抽取 + 按用户隔离 + 去重/权重/时间衰减 + 短→长沉淀 |
| `tool_audit.py` | **工具调用审计**：trace_id/入参脱敏/结果摘要/耗时/成败/重试数 → `tool_audit.jsonl`(实时追加) |
| `observability.py` | **LangFuse 可观测**：全链路 span/generation 封装，降级安全(未启用/服务未起全 no-op) |
| `rag_eval/` | **RAGAS 评测体系**：A/B 对比(基线纯向量 vs 优化双语+重排)、4 指标量化(召回/精准/忠实/相关)、分层 + Bad Case + 成本报告；见 `RAG评测体系.md` |
| `config.py` | 全局配置：LLM/嵌入/重排/权限/PDF 阈值/公式等所有配置项 |

## 关键设计决策

- **MCP 通信**：`UnifiedAgent` 通过 stdio 子进程启动 `mcp_server.py`，用 `async with stdio_client` 管理生命周期（Windows 不能用嵌套 `__aenter__`）
- **ReAct 自研替换 LangChain**：LangChain ReAct 天然串行，自研版本保留并行能力——"查天气+查知识库"同时调
- **Skill 匹配策略**：关键词+向量双重匹配 → Top3 交 LLM 确认 → 命中 1 轮执行 → 未命中走 ReAct → 失败无感降级
- **跨进程权限传递**：主进程 `app.py` 登录后 → `set_current_kb_groups()` 写 `kb_permission_context.json` → MCP 子进程 `vector_store.search()` 实时读取做 ChromaDB where 过滤
- **精准备份而非全库拷贝**：删除/更新/清空前，只备份即将被删的 chunk 数据（ids+documents+metadatas+embeddings），回退时精确恢复到 ChromaDB
- **快慢双通道 PDF 解析**：90% PDF 走 PyMuPDF 快速通道（多栏检测+标题聚类+页眉过滤），质量差的才启动 PaddleOCR（500MB+，懒加载）
- **混合检索分工**：`hybrid_retriever` 用 RRF 融合 BM25+向量两路召回（只用排名不用绝对分数，回避余弦/BM25 分数不可比），融合后仍交 `reranker` 精排——RRF 负责"合成候选"，Cross-Encoder 负责"精排"，二者不冲突
- **BM25 索引跨进程一致**：`bm25_index` 用"chunk 数+内容 hash"签名驱动懒重建，Streamlit 增删文档后 MCP 子进程检索时自动感知重建，无需侵入每个增删改函数
- **改写/混合/评测全程可降级**：查询改写、混合检索、重排任一失败都无感回退（改写→原 query、混合→纯向量、Cross-Encoder→LLM→原序），主链路绝不中断；均由 config 开关控制
- **Python 解释器**：自动检测 venv Python，确保 MCP 子进程使用正确环境
- **`.env` 加载**：`config.py` 用 `Path(__file__).resolve().parent / ".env"` 绝对路径

## 重要注意事项

- **嵌入模型预加载**：`mcp_server.py` 启动时加载 `paraphrase-multilingual-MiniLM-L12-v2`（420MB），避免首次查询超时
- **重排模型**：BGE-Reranker-v2-m3（568MB），首次加载需 10-30s，`RERANK_MIN_SCORE=-999.0` 表示不过滤（BGE logits 非 0-1 概率）
- **API 超时**：所有 `OpenAI()` 和 `httpx` 调用设了 timeout；`max_tokens=4000`（安全线，非成本控制）
- **Windows 兼容**：MCP 子进程必须 `async with stdio_client`；文件路径统一正斜杠
- **权限过滤开关**：`KB_PERMISSION_ENABLED=False` 时全量文档可检索（默认关闭，兼容旧行为）
- **filelock 并发控制**：所有 ChromaDB 写操作前获取全局文件锁 `chroma_db/kb.lock`（30s 超时）
- **ChromaDB 稳定 ID**：`chunk_id = {file_hash}_{index:04d}`，保证增量添加天然幂等
- **source 兜底**：查 ChromaDB 时优先 `file_path`，无匹配兜底 `source`（basename），兼容老数据
- **前端代理**：`vite.config.ts` 将 `/api` 代理到 `localhost:8000`，开发时无需跨域配置
- **RAGAS + DeepSeek 兼容**：DeepSeek 只支持 `n=1`，RAGAS 部分指标默认多候选采样(`n>1`)会报 `400 Invalid n value`；解法是 `rag_eval/adapters.py` 里对 `LangchainLLMWrapper` 设 `bypass_n=True`
- **RAGAS 评测清洗**：faithfulness 会把答案里的来源标注/免责声明误判为幻觉；`run_eval.py` 的 `clean_answer_for_eval()` 在打分前清洗这些格式(仅作用于评测副本，不改真实答案)
- **推理模型 max_tokens 陷阱（重要）**：`LLM_MODEL=deepseek-v4-flash` 是推理模型，先输出 `reasoning_content` 再出正式 `content`；`max_tokens` 过小(如 50/120)会导致 token 被推理耗尽、`content` 返回空串。凡是"输出短但要它真回话"的调用(查询翻译/改写等)必须给足 `max_tokens`(≥512)，否则静默返回空
- **RAGAS 打分防超时**：`run_eval.py` 的 `run_ragas()` 传 `RunConfig(timeout=180, max_workers=4, max_retries=3)`——DeepSeek 高并发下易超时，超时的 Job 会以 NaN 污染指标均值(报告显示 N/A)；降并发换干净数据
- **混合检索/查询改写开关**：`HYBRID_ENABLED` / `QUERY_REWRITE_ENABLED`(config.py) 控制是否启用；BM25 索引持久化在 `bm25_index.pkl`，停用词表 `bm25_stopwords.txt`(不存在则不过滤)
- **工具调用统一重试**：`mcp_client_manager.call_tool` 用 tenacity 指数退避，`scheduler` 与 `skill_executor` 两路径共用此层自动统一；只重试瞬时故障(超时/连接)，业务错误(isError)/参数校验失败不重试；`_execute_one` 已去掉外层 `wait_for`(超时+重试下沉到 call_tool)
- **trace_id 贯穿**：`unified_agent._run()` 生成 `uuid4().hex`，透传 Scheduler/SkillExecutor + `token_tracker.set_trace_id()`，一问多工具在审计与 LangFuse 里归并同一链路
- **protobuf 冲突(重要)**：`langfuse` 会把 protobuf 拉到 7.x，但 `paddlepaddle 2.6.2` 要求 `protobuf<=3.20.2`(否则 paddle/OCR 崩)；装完 langfuse 必须 `pip install "protobuf<=3.20.2"` 回退，langfuse 走 HTTP 不受影响
- **LangFuse 默认关**：`LANGFUSE_ENABLED=False`；observability.py 全程降级安全(未启用/缺密钥/服务未起均 no-op，不阻断主链路)
- **长期记忆按用户隔离**：`retriever.set_current_user(username)` 写 `memory_user_context.json`(仿 kb 权限跨进程模式)，`long_term_memory` 按 user_id 过滤；`app.py` 登录/登出时设置。独立于 `clear_memory()`(清会话不清长期记忆)
- **长期记忆检索用距离分**：本地嵌入的 `similarity_search_with_relevance_scores` 会返回负值(同 reranker logits 非概率问题)，改用 `similarity_search_with_score` 距离分转 `1/(1+dist)`；抽取 LLM 调用 `max_tokens≥512`(推理模型陷阱)
