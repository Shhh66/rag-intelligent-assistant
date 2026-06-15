"""评测日志模块 —— 记录问答质量，支持分析与迭代"""

import json
from datetime import datetime
from pathlib import Path


class EvaluationLogger:
    """记录和评估每次问答的质量"""

    def __init__(self, log_file: str = "qa_log.jsonl"):
        self.log_file = Path(log_file)
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    def log(self, user_input: str, answer: str, intent: str, top_docs: list, latency_ms: float):
        """记录一条问答日志"""
        entry = {
            "session_id": self.session_id,
            "timestamp": datetime.now().isoformat(),
            "user_input": user_input,
            "answer": answer,
            "intent": intent,
            "retrieved_docs_count": len(top_docs),
            "retrieved_docs_preview": [d.page_content[:80] for d in top_docs],
            "latency_ms": round(latency_ms, 2),
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"   📝 日志已记录: {self.log_file}")


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== 评测日志模块测试 ===\n")

    logger = EvaluationLogger(log_file="test_qa_log.jsonl")

    # 模拟记录几条问答
    mock_docs = type("Doc", (), {"page_content": "这是一段模拟的检索结果文本"})

    print("记录第1条日志...")
    logger.log("什么是 AI？", "AI 是人工智能的缩写...", "knowledge", [mock_docs()], 1500.5)

    print("记录第2条日志...")
    logger.log("你好", "你好！有什么可以帮你的？", "chat", [], 800.2)

    print("记录第3条日志...")
    logger.log("Python 是什么？", "Python 是一种编程语言...", "knowledge", [mock_docs(), mock_docs()], 2100.3)

    # 查看日志文件内容
    print(f"\n📄 日志文件内容预览:")
    with open("test_qa_log.jsonl", "r", encoding="utf-8") as f:
        for line in f:
            print(f"   {line.strip()[:100]}...")

    # 清理
    import os
    os.remove("test_qa_log.jsonl")
    print(f"\n🎉 评测日志模块测试完成！")
