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


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    from document_loader import load_file
    from text_splitter import split_documents
    from vector_store import build_vector_store
    import os

    # 1. 构建知识库
    test_file = "test_agent_sample.txt"
    with open(test_file, "w", encoding="utf-8") as f:
        f.write(
            "蚂蚁集团成立于2014年，前身是支付宝。"
            "蚂蚁集团的使命是让天下没有难做的生意。\n\n"
            "智能体（Agent）是能够自主感知环境、制定计划并执行动作的AI系统。"
            "LangChain是构建Agent的流行框架之一。\n\n"
            "2024年是大模型应用爆发的一年，RAG和Agent成为最受关注的两大技术方向。"
        )

    print("=== 构建知识库 ===")
    docs = load_file(test_file)
    chunks = split_documents(docs)
    build_vector_store(chunks)

    # 2. 测试 Agent
    print("\n=== Agent 交互测试 ===\n")
    agent = AgentCore()

    test_inputs = [
        "蚂蚁集团什么时候成立的？",
        "你好，今天心情怎么样？",
        "什么是智能体？",
        "它是用来做什么的？",    # 追问，依赖上下文记忆
    ]

    for text in test_inputs:
        print(f"👤 用户: {text}")
        answer = agent.chat(text)
        print(f"🤖 Agent: {answer}\n")
        print("-" * 50 + "\n")

    print("🎉 Agent 核心模块测试完成！")
    os.remove(test_file)
