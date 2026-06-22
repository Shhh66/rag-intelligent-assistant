"""Agent 核心调度模块 —— 统一检索优先 + LLM 兜底"""

from retriever import answer_with_fallback
from memory_manager import ConversationMemory


class AgentCore:
    """智能体核心：整合检索、记忆、生成"""

    def __init__(self):
        self.memory = ConversationMemory()

    def chat(self, user_input: str) -> str:
        """主入口：接收用户输入，统一走检索优先 + LLM兜底"""
        answer = answer_with_fallback(user_input)

        # 存入对话记忆
        self.memory.add("user", user_input)
        self.memory.add("assistant", answer)

        return answer


