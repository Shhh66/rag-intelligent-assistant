"""Agent 模块 —— LLM 自主决策：路由 → 工具执行 → 反思 → 回答
工具通过 MCP 服务注册（mcp_server.py），Agent 在同一进程内直接调用。"""

import asyncio
import json
import sys
from openai import OpenAI
from memory_manager import ConversationMemory
from config import GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL
from dotenv import load_dotenv

load_dotenv()


class Agent:
    """智能体：持有工具列表，LLM 自主决定调用哪个工具或直接回答"""

    def __init__(self):
        self.memory = ConversationMemory()
        self.client = OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL)
        self.max_turns = 5
        self._tools = [self._tool_weather(), self._tool_search_kb()]

    # ===== 工具定义（OpenAI function calling 格式）=====

    @staticmethod
    def _tool_weather() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "query_weather",
                "description": "查询指定城市的实时天气信息。当用户询问某地天气、温度、是否下雨时调用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名称，如 Beijing、上海"}
                    },
                    "required": ["city"],
                },
            },
        }

    @staticmethod
    def _tool_search_kb() -> dict:
        return {
            "type": "function",
            "function": {
                "name": "search_knowledge_base",
                "description": (
                    "在私有知识库中搜索文档片段。当用户询问已上传文档中的专业知识时调用。"
                    "闲聊、通用知识、天气查询不需要此工具。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "搜索关键词"}
                    },
                    "required": ["query"],
                },
            },
        }

    # ===== 工具执行（懒加载避免启动时加载模型）=====

    def _run_weather(self, args: dict) -> str:
        from Weather_search import fetch_weather_data, format_weather
        city = args.get("city", "")
        if not city:
            return "错误：未提供城市名"
        data = asyncio.run(fetch_weather_data(city))
        if data is None:
            return "无法获取天气数据，请稍后再试。"
        return format_weather(data)

    def _run_search_kb(self, args: dict) -> str:
        from vector_store import search
        query = args.get("query", "")
        if not query:
            return "错误：未提供搜索关键词"
        try:
            docs = search(query, top_k=5)
        except FileNotFoundError:
            return "知识库尚未构建，没有可用文档。"
        except Exception as e:
            return f"检索出错: {e}"
        if not docs:
            return f"未找到与「{query}」相关的文档。"
        parts = []
        for i, doc in enumerate(docs, 1):
            source = doc.metadata.get("source", "未知")
            parts.append(f"[片段 {i}] 来源: {source}\n{doc.page_content}")
        return "\n\n".join(parts)

    # ===== Agent 主循环 =====

    def chat(self, user_input: str) -> str:
        self.memory.add("user", user_input)

        system_prompt = (
            "你是一个智能助手，可以查询天气和搜索知识库。\n"
            "- 用户问天气、温度、是否下雨 → 调用 query_weather\n"
            "- 用户问知识库内容、已上传文档 → 调用 search_knowledge_base\n"
            "- 用户闲聊、问通用知识 → 直接回答，不要调用任何工具"
        )

        messages = [{"role": "system", "content": system_prompt}]
        for m in self.memory.messages[-20:]:
            messages.append({"role": m["role"], "content": m["content"]})

        for _ in range(self.max_turns):
            response = self.client.chat.completions.create(
                model=LLM_MODEL, messages=messages, tools=self._tools,
                tool_choice="auto", temperature=0.3, max_tokens=800,
            )
            msg = response.choices[0].message

            if not msg.tool_calls:
                answer = msg.content or ""
                self.memory.add("assistant", answer)
                return answer

            messages.append({
                "role": "assistant", "content": msg.content or "",
                "tool_calls": [{
                    "id": tc.id, "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                } for tc in msg.tool_calls],
            })

            for tc in msg.tool_calls:
                tool_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                print(f"   🔧 Agent 调用: {tool_name}({args})", file=sys.stderr, flush=True)

                if tool_name == "query_weather":
                    result = self._run_weather(args)
                elif tool_name == "search_knowledge_base":
                    result = self._run_search_kb(args)
                else:
                    result = f"未知工具: {tool_name}"

                print(f"   ✅ 结果: {result[:100]}...", file=sys.stderr, flush=True)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})

        messages.append({"role": "user", "content": "请基于以上全部工具调用结果，给出最终回答。"})
        final = self.client.chat.completions.create(
            model=LLM_MODEL, messages=messages, temperature=0.3, max_tokens=800,
        )
        answer = final.choices[0].message.content or ""
        self.memory.add("assistant", answer)
        return answer
