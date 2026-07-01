

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

基于 **MCP 协议**构建的可扩展 LLM 智能体框架。核心理念：Agent 不关心工具内部实现，只需一套标准协议即可将**任意业务系统**接入为可调用的工具。目前已接入 RAG 知识库检索和天气查询作为示例工具，展示了 Agent 自主发现工具、判断意图、编排调用的完整链路。上传文档即可构建私有知识库，自然语言提问，Agent 自动选择最优策略执行。

### 架构演进路线

```
当前（已实现）               第一阶段（实施中）           第二阶段（规划中）
MCP 工具发现                  MCP 工具发现                MCP 工具发现
    │                             │                           │
JSON 结构化决策              ReAct 推理引擎             ReAct 推理引擎
    │                        Thought→Action→Obs          Thought→Action→Obs
    │                             │                           │
MCP 工具执行                  ├─ Skill 匹配（前置）       ├─ Skill 匹配（前置+动态）
                              │  命中 → 1轮执行           │  多意图拆分
                              │  未命中 → ReAct 兜底       │  上下文槽位补全
                              │                             │
                              └─ MCP 工具执行              ├─ Skill 生命周期管理
                                                            ├─ 自动沉淀+审核闭环
                                                            └─ MCP 工具执行
```

**三层架构思想**（类比）：
- **MCP（锅碗瓢盆）**：有什么工具可用 → 自动发现，标准化接入
- **Skills（菜谱）**：这些工具怎么组合用 → 声明式定义，高频组合一次沉淀反复复用
- **ReAct（厨师大脑）**：用户这个问题该用哪个 Skill/工具 → 自主推理，知道何时停

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

- **MCP 通信**：`UnifiedAgent` 通过 stdio 子进程启动 `mcp_server.py`，使用 `async with` 管理子进程生命周期
- **决策引擎演进**：从 JSON 结构化决策（`{"action":"call_tools"}`）→ ReAct 推理（Thought→Action→Observation），LLM 自主决定何时终止，不再依赖外部 `for turn in range(5)` 循环
- **Skills 声明式定义**：每个 Skill 只需一个 `SKILL` 字典（name、steps、arg_slots），无需继承基类。新增 Skill = 新增文件，和 MCP 的 `@mcp.tool()` 一样零摩擦
- **匹配 vs 兜底**：Skill 前置匹配优先（高频场景 1 轮搞定），无匹配走 ReAct 兜底（通用能力不退化），Skill 执行失败无感降级到 ReAct
- **并行保留**：自研轻量 ReAct 保留并行执行能力（vs LangChain ReAct 天然串行），两个无依赖工具仍可同时调用
- **Python 解释器**：自动检测 venv Python，确保 MCP 子进程使用正确环境
- **.env 加载**：`config.py` 使用 `Path(__file__).resolve().parent / ".env"` 绝对路径加载
- **天气翻译**：`Weather_search.py` 优先查本地中英对照表，LLM 翻译失败时自动回退
- **Token 成本追踪**：`token_tracker.py` 会话级累积 + 文件持久化，侧边栏三层展示（本次问答/会话累计/历史总计）

### 文件职责速查

| 文件 | 角色 |
|------|------|
| `mcp_unified_agent/` | **智能体核心**：MCP 客户端、工具注册表、决策引擎、调度器、反思记忆、Skill 注册表与执行器 |
| `skills/` | **技能组合层**：声明式 Skill 定义，自动发现加载 |
| `agent.py` | 薄封装，`class Agent(UnifiedAgent)`，供 `app.py` 使用 |
| `mcp_server.py` | **工具层**：FastMCP 服务，注册所有工具 |
| `Weather_search.py` | 天气工具实现：城市翻译 + OpenWeatherMap API |
| `retriever.py` | RAG 检索实现：双语检索 + Prompt 构建 |
| `vector_store.py` | ChromaDB 向量存储与检索 |
| `token_tracker.py` | Token 用量追踪：会话累积 + 文件持久化 + 成本计算 |
| `app.py` | Streamlit 界面 |
| `config.py` | 全局配置，含 LLM 定价（`MODEL_PRICING`） |


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

## Skills 架构设计（面试重点）

### 三层模型

| 层级 | 解决的问题 | 类比 | 谁定义 | 示例 |
|------|-----------|------|--------|------|
| MCP 工具层 | 有什么能力可用 | 锅碗瓢盆 | 开发者 `@mcp.tool()` | query_weather, ask_knowledge_base |
| Skills 组合层 | 能力怎么组合用 | 菜谱 | 业务人员 + 自动沉淀 | "出差规划"=天气+航班+酒店 |
| ReAct 决策层 | 用户意图用哪个 | 厨师大脑 | LLM 自主推理 | Thought→Action→Obs→Final Answer |

### Skill 定义格式（声明式）

```python
SKILL = {
    "name": "comprehensive_query",
    "description": "同时查询天气和知识库",
    "trigger_keywords": ["天气", "还有", "以及"],
    "exclude_keywords": ["API", "接口", "开发"],    # 负向过滤
    "match_threshold": 0.5,                        # 个性化置信度
    "steps": [
        {"tool": "query_weather", "args": {"city": "{city}"}, "retryable": True, "critical": True},
        {"tool": "ask_knowledge_base", "args": {"query": "{query}"}, "retryable": True, "critical": True},
    ],
    "execution_mode": "parallel",
    "arg_slots": {
        "city": {"description": "城市名", "type": "string", "required": True},
        "query": {"description": "知识问题", "type": "string", "required": True},
    },
}
```

### 核心设计理念

1. **为什么不用 LangChain ReAct 直接替换？**
   LangChain ReAct 是串行的（每步一个工具），会失去并行能力。自研轻量 ReAct 保留并行执行，仅改进 Prompt 让 LLM 输出推理链格式。

2. **为什么 Skill 用声明式而非代码式？**
   声明式 = 零学习成本、可序列化存数据库、LLM 可自动生成（自动沉淀）。代码式 = 灵活性高但门槛高、难维护。

3. **Skill 匹配策略：前置匹配 + ReAct 兜底**
   入口处先匹配 Skill（关键词+向量双重匹配，返回 Top3 由 LLM 确认），匹配到就 1 轮执行。匹配不到或无匹配时走 ReAct 完整推理，通用能力不退化。

4. **多层降级保障**
   参数校验失败 → 反问用户 / LLM 修正 → 仍失败 → 降级 ReAct → Skill 整体失败 → 无感降级 ReAct
