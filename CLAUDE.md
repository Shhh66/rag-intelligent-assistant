# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

始终使用中文回复。

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
│  mcp_server.py (5 个 MCP 工具)                            ││
│  query_weather / ask_knowledge_base / search_knowledge_base│
│  check_kb_status / clear_memory / debug_rerank            ││
└──────────┬───────────────────────────────────────────────┘│
           │                                                 │
           ▼                                                 │
┌──────────────────────────────┐                             │
│  RAG 检索管线                 │                             │
│  document_loader → splitter  │                             │
│  → ChromaDB → retriever     │                             │
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
| `retriever.py` | RAG 检索：双语检索 + Prompt 构建 + 权限上下文写入 |
| `reranker.py` | BGE-Reranker-v2-m3 Cross-Encoder 重排 + LLM 降级兜底 |
| `document_loader.py` | 文档解析：PyMuPDF(PDF) + python-docx(DOCX) + PaddleOCR 降级通道 |
| `text_splitter.py` | Markdown 标题切分 + 递归二次切分 + 小章节合并 |
| `ocr_processor.py` | PaddleOCR/PP-Structure 懒加载 + 公式识别三级降级链 |
| `token_tracker.py` | Token 用量追踪：会话累积 + 文件持久化 + 成本计算 |
| `config.py` | 全局配置：LLM/嵌入/重排/权限/PDF 阈值/公式等所有配置项 |

## 关键设计决策

- **MCP 通信**：`UnifiedAgent` 通过 stdio 子进程启动 `mcp_server.py`，用 `async with stdio_client` 管理生命周期（Windows 不能用嵌套 `__aenter__`）
- **ReAct 自研替换 LangChain**：LangChain ReAct 天然串行，自研版本保留并行能力——"查天气+查知识库"同时调
- **Skill 匹配策略**：关键词+向量双重匹配 → Top3 交 LLM 确认 → 命中 1 轮执行 → 未命中走 ReAct → 失败无感降级
- **跨进程权限传递**：主进程 `app.py` 登录后 → `set_current_kb_groups()` 写 `kb_permission_context.json` → MCP 子进程 `vector_store.search()` 实时读取做 ChromaDB where 过滤
- **精准备份而非全库拷贝**：删除/更新/清空前，只备份即将被删的 chunk 数据（ids+documents+metadatas+embeddings），回退时精确恢复到 ChromaDB
- **快慢双通道 PDF 解析**：90% PDF 走 PyMuPDF 快速通道（多栏检测+标题聚类+页眉过滤），质量差的才启动 PaddleOCR（500MB+，懒加载）
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
