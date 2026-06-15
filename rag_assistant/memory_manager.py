"""对话记忆模块 —— 管理多轮对话历史"""

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
            print(f"   🧹 记忆已满，移除旧消息: {removed['content'][:30]}...")

    def clear(self):
        """清空记忆"""
        self.messages.clear()
        print("   🗑 对话记忆已清空")


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== 对话记忆模块测试 ===\n")

    memory = ConversationMemory(max_rounds=2)  # 只保留2轮，方便测试

    # 模拟一轮对话
    memory.add("user", "什么是 Python？")
    memory.add("assistant", "Python 是一种高级编程语言。")
    print("第1轮完成")

    # 再一轮
    memory.add("user", "它有什么优点？")
    memory.add("assistant", "Python 语法简洁，拥有丰富的第三方库。")
    print("第2轮完成")

    # 第3轮——触发记忆清理
    memory.add("user", "和 Java 比呢？")
    memory.add("assistant", "Python 更简洁，Java 性能更强。")
    print("第3轮完成")

    # 清空测试
    memory.clear()
    print(f"清空后消息数: {len(memory.messages)}")

    print("\n🎉 对话记忆模块测试完成！")
