# MCP 统一智能体 — 通用 LLM Agent 框架

基于 **MCP 协议**构建的可扩展 LLM 智能体框架。核心理念：Agent 不关心工具内部实现，只需一套标准协议即可将**任意业务系统**接入为可调用的工具。目前已接入 RAG 知识库检索和天气查询作为示例工具，展示了 Agent 自主发现工具、判断意图、编排调用的完整链路。上传文档即可构建私有知识库，自然语言提问，Agent 自动选择最优策略执行。

**换个工具集就能换一个业务场景**：将天气查询换成订单查询 API → 客服助手；接入监控 + 日志系统 → 运维故障自诊断；接入 SQL 引擎 → 业务自助查数。新增工具只需注册 MCP 接口，Agent 代码零改动。

## 架构

```
用户提问 → UnifiedAgent（唯一的智能体大脑）
              │ LLM 自主决策：调用哪个工具？
              │ MCP 协议 (stdio)
              ▼
           mcp_server.py（工具层）
           ├── query_weather        → OpenWeatherMap
           ├── ask_knowledge_base   → RAG 检索 + LLM 生成
           ├── search_knowledge_base → 纯向量语义检索
           ├── check_kb_status      → 知识库状态查询
           └── clear_memory         → 清空对话记忆
```

新增工具只需在 `mcp_server.py` 中加一个 `@mcp.tool()` 装饰器，智能体重启后自动发现。

## 快速开始

```bash
cd rag_assistant
venv\Scripts\activate.bat    # Windows
# 或 source venv/bin/activate  # Mac / Linux

# 安装依赖
pip install -r requirements.txt

# 启动 Web 界面
streamlit run app.py
```

浏览器打开 `http://localhost:8501`，上传 PDF/Word/TXT 文档，点击"构建知识库"即可开始问答。

## 架构演进

项目正在从「MCP + JSON 决策」向「MCP + ReAct + Skills」三层架构演进：

```
当前架构（v1）                    目标架构（v2）
══════════════                    ══════════════
用户输入                          用户输入
  │                                 │
  ▼                                 ▼
JSON 决策引擎                   ┌─ Skill 匹配 ─┐
  │                             │ 命中 → 1轮执行 │
  ▼                             │ 未命中 ↓       │
MCP 工具 串行/并行              │ ReAct 推理循环  │
  │                             └──────┬────────┘
  ▼                                    ▼
LLM 汇总回答                      MCP 工具执行
                                     │
                                     ▼
                                 LLM 汇总回答
```

- **MCP 层**（不变）：标准化工具接入，自动发现
- **Skills 层**（新增）：工具组合声明式定义，高频模式一次沉淀反复复用
- **ReAct 层**（替代 JSON 决策）：Thought→Action→Observation 自然推理，LLM 自主决定何时终止

详见 `CLAUDE.md` 中的「Skills 架构设计」章节。

## 功能

- **智能工具调度**：LLM 根据问题自动判断是否需要工具、用哪个、串行还是并行
- **知识库问答**：上传文档 → ChromaDB 向量化 → 中英双语检索 → LLM 基于原文回答 + 标注来源
- **天气查询**：支持中英文城市名，本地对照表即时翻译，OpenWeatherMap 实时数据
- **对话记忆**：记住上下文，理解"它"、"那个"等指代词
- **反思记忆**：留存工具选择历史，同类问题优先复用成功经验

## 项目结构

```
rag_assistant/
├── app.py                  # Streamlit 界面
├── agent.py                # Agent 入口（继承 UnifiedAgent）
├── mcp_server.py           # MCP 工具注册（FastMCP）
├── config.py               # 全局配置（含 LLM 定价）
├── retriever.py            # RAG 检索：双语检索 + Prompt 构建
├── vector_store.py         # ChromaDB 向量存储
├── Weather_search.py       # 天气工具：翻译 + API
├── document_loader.py      # 文档加载（PDF/Word/TXT）
├── text_splitter.py        # 文本分块（含结构标注）
├── evaluation.py           # 问答日志（含 Token/费用追踪）
├── token_tracker.py        # Token 用量追踪：会话累积 + 持久化
├── skills/                 # 技能组合层（新增）
│   ├── builtin/
│   │   ├── comprehensive_query.py  # 综合查询
│   │   ├── weather_advice.py       # 天气出行建议
│   │   └── deep_kb_search.py       # 深度知识库检索
│   └── __init__.py         # Skill 自动发现
└── mcp_unified_agent/      # 智能体核心
    ├── unified_agent.py    # 编排调度（含 Skill 匹配集成）
    ├── decision_engine.py  # ReAct 决策引擎（Thought→Action→Obs）
    ├── scheduler.py        # 串行/并行工具执行
    ├── tool_registry.py    # 工具元数据缓存 + Schema 校验
    ├── skill_registry.py   # Skill 注册表：加载 + 匹配
    ├── skill_executor.py   # Skill 执行器：参数校验 + 降级
    ├── prompt_templates.py # ReAct Prompt 模板
    ├── tool_vector_filter.py   # 向量预筛选
    ├── reflection_memory.py    # 反思记忆
    └── mcp_client_manager.py   # MCP 会话封装
```

## 配置

复制 `.env.example` 为 `.env`，填入 API Key：

```env
OPENWEATHER_API_KEY=你的OpenWeatherMap密钥
GROQ_API_KEY=你的DeepSeek密钥    # 变量名历史遗留，实际指向 DeepSeek
```

在 `config.py` 中可切换 LLM 后端：

```python
GROQ_BASE_URL = "https://api.deepseek.com"
LLM_MODEL = "deepseek-v4-flash"
```

## 命令行调用

```bash
python -c "from agent import Agent; a = Agent(); print(a.chat('北京天气？'))"
```

## MCP Inspector 调试

```bash
npx @modelcontextprotocol/inspector python mcp_server.py
```
