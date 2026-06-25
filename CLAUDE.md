

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

MCP 统一智能体系统。核心是一个 LLM 驱动的自主决策引擎，RAG 知识库检索和天气查询都是它通过 MCP 协议调用的**工具**，而非独立的处理管线。用户上传文档构建知识库后，智能体自动判断问题类型、选择合适的工具、执行调用并汇总回答。

## 常用命令

```bash
cd rag_assistant
venv\Scripts\activate.bat

# Streamlit Web 界面
streamlit run app.py

# MCP Inspector 自测工具
npx @modelcontextprotocol/inspector python mcp_server.py

# 命令行调用
python -c "from agent import Agent; a = Agent(); print(a.chat('北京天气？'))"
```

## 架构：一个智能体 + 多个 MCP 工具

```
用户提问
    │
    ▼
┌─────────────────────────────┐
│  UnifiedAgent (唯一入口)     │
│  agent.py → mcp_unified_agent/ │
│                              │
│  ① 启动 mcp_server.py 子进程  │
│  ② 自动发现全部 MCP 工具      │
│  ③ LLM 结构化 JSON 决策      │
│  ④ 串行/并行调度执行工具      │
│  ⑤ 结果回填 → 二次推理       │
│  ⑥ 汇总输出最终回答          │
└──────────┬──────────────────┘
           │ MCP 协议 (stdio)
           ▼
┌─────────────────────────────┐
│  mcp_server.py (工具层)      │
│                              │
│  query_weather       → OpenWeatherMap │
│  ask_knowledge_base  → RAG 检索 + LLM │
│  search_knowledge_base → 纯向量检索    │
│  check_kb_status     → 知识库状态     │
│  clear_memory        → 清空对话记忆   │
└─────────────────────────────┘
```

**核心理念**：智能体不关心工具内部实现。新增工具只需在 `mcp_server.py` 中用 `@mcp.tool()` 注册，智能体下次启动时自动发现并学会使用。

### 关键设计决策

- **MCP 通信**：`UnifiedAgent` 通过 stdio 子进程启动 `mcp_server.py`，使用 `async with` 管理子进程生命周期（Windows 上不能用嵌套 `__aenter__`）
- **LLM 决策格式**：使用结构化 JSON（`{"action":"call_tools","tools":[{"tool_name":"...","arguments":{...}}]}`），而非 OpenAI function calling
- **Python 解释器**：自动检测 venv Python，确保 MCP 子进程使用正确环境
- **.env 加载**：`config.py` 使用 `Path(__file__).resolve().parent / ".env"` 绝对路径加载，兼容任意工作目录
- **天气翻译**：`Weather_search.py` 优先查本地中英对照表（`_CITY_MAP`），LLM 翻译失败时自动回退

### 文件职责速查

| 文件 | 角色 |
|------|------|
| `mcp_unified_agent/` | **智能体核心**：MCP 客户端、工具注册表、决策引擎、调度器、反思记忆 |
| `agent.py` | 薄封装，`class Agent(UnifiedAgent)`，供 `app.py` 使用 |
| `mcp_server.py` | **工具层**：FastMCP 服务，注册所有工具 |
| `Weather_search.py` | 天气工具实现：城市翻译 + OpenWeatherMap API |
| `retriever.py` | RAG 检索实现：双语检索 + Prompt 构建 |
| `vector_store.py` | ChromaDB 向量存储与检索 |
| `app.py` | Streamlit 界面 |
| `config.py` | 全局配置，从 `.env` 加载 Key |


### 环境变量（.env）

```
OPENWEATHER_API_KEY=<OpenWeatherMap Key>
GROQ_API_KEY=<DeepSeek API Key>     # 变量名保留历史原因，实际指向 DeepSeek
```

`config.py` 中 `GROQ_BASE_URL` 和 `LLM_MODEL` 控制 LLM 后端（当前 `api.deepseek.com` + `deepseek-v4-flash`）。

## 重要注意事项

- **API 超时**：所有 `OpenAI()` 客户端和 `httpx` 调用都设置了 `timeout`，防止 API 挂起导致无限阻塞
- **嵌入模型预加载**：`mcp_server.py` 启动时自动加载 `paraphrase-multilingual-MiniLM-L12-v2`（420MB），避免首次查询时阻塞超时
- **RAG 回答长度**：`retriever.py` 中 `max_tokens=2000`，确保多知识点回答不被截断
- **单工具成功直接返回**：`_pipeline` 中工具成功后直接返回结果，不再调 `final_answer`（避免多余的 LLM 调用失败导致"(LLM 未返回内容)"）
- **Windows 兼容**：MCP 子进程通信必须用 `async with stdio_client` 直接管理，不能用嵌套 `__aenter__`
- **城市翻译**：优先本地对照表，DeepSeek `deepseek-v4-flash` 对极简翻译指令可能返回空字符串
