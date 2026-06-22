"""对话记忆模块 —— 管理多轮对话历史"""

import sys
from typing import List, Dict
from config import MAX_MEMORY_ROUNDS


class ConversationMemory:
    """存储和检索对话历史"""

    def __init__(self, max_rounds: int = MAX_MEMORY_ROUNDS):
        self.messages: List[Dict[str, str]] = []
        self.max_rounds = max_rounds

    def add(self, role: str, content: str):
        """添加一条消息（role = 'user' 或 'assistant'）"""
        self.messages.append({"role": role, "content": content})
        # 超过最大轮数，移除最早的
        # 一轮 = 1 user + 1 assistant = 2 条
        max_messages = self.max_rounds * 2
        if len(self.messages) > max_messages:
            removed = self.messages.pop(0)
            print(f"   🧹 记忆已满，移除旧消息: {removed['content'][:30]}...", file=sys.stderr, flush=True)

    def clear(self):
        """清空记忆"""
        self.messages.clear()
        print("   🗑 对话记忆已清空", file=sys.stderr, flush=True)


