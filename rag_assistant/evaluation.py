"""评测日志模块 —— 记录问答质量，支持分析与迭代"""

import json
from datetime import datetime
from pathlib import Path


class EvaluationLogger:
    """记录和评估每次问答的质量"""

    def __init__(self, log_file: str = "qa_log.jsonl"):
        self.log_file = Path(log_file)
        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    def log(
        self,
        user_input: str,
        answer: str,
        intent: str,
        top_docs: list,
        latency_ms: float,
        token_usage: dict | None = None,
    ):
        """记录一条问答日志。

        Args:
            token_usage: TokenTracker.get_last_usage() 的返回值，
                         或 {"prompt_tokens": N, "completion_tokens": N, "total_tokens": N, "cost_rmb": N}
        """
        if token_usage is None:
            token_usage = {}

        entry = {
            "session_id": self.session_id,
            "timestamp": datetime.now().isoformat(),
            "user_input": user_input,
            "answer": answer,
            "intent": intent,
            "retrieved_docs_count": len(top_docs),
            "retrieved_docs_preview": [d.page_content[:80] for d in top_docs],
            "latency_ms": round(latency_ms, 2),
            "prompt_tokens": token_usage.get("prompt_tokens", 0),
            "completion_tokens": token_usage.get("completion_tokens", 0),
            "total_tokens": token_usage.get("total_tokens", 0),
            "cost_rmb": round(token_usage.get("cost_rmb", 0), 6),
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        print(f"   📝 日志已记录: {self.log_file}")

    def get_stats(self) -> dict:
        """统计所有问答的概览数据（含 Token 和费用）。"""
        if not self.log_file.exists():
            return {"total": 0}

        entries = []
        with open(self.log_file, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))

        total = len(entries)
        knowledge_count = sum(1 for e in entries if e.get("intent") == "knowledge")
        chat_count = sum(1 for e in entries if e.get("intent") == "chat")
        avg_latency = sum(e.get("latency_ms", 0) for e in entries) / total if total else 0
        avg_docs = sum(e.get("retrieved_docs_count", 0) for e in entries) / total if total else 0
        total_input_tokens = sum(e.get("prompt_tokens", 0) for e in entries)
        total_output_tokens = sum(e.get("completion_tokens", 0) for e in entries)
        total_cost = sum(e.get("cost_rmb", 0) for e in entries)

        return {
            "total_queries": total,
            "knowledge": knowledge_count,
            "chat": chat_count,
            "avg_latency_ms": round(avg_latency, 2),
            "avg_retrieved_docs": round(avg_docs, 2),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tokens": total_input_tokens + total_output_tokens,
            "total_cost_rmb": round(total_cost, 6),
        }


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== 评测日志模块测试 ===\n")

    logger = EvaluationLogger(log_file="test_qa_log.jsonl")

    # 模拟记录几条问答
    mock_docs = type("Doc", (), {"page_content": "这是一段模拟的检索结果文本"})

    print("记录第1条日志（含 Token 信息）...")
    logger.log(
        "什么是 AI？", "AI 是人工智能的缩写...", "knowledge",
        [mock_docs()], 1500.5,
        token_usage={"prompt_tokens": 500, "completion_tokens": 200, "total_tokens": 700, "cost_rmb": 0.0009},
    )

    print("记录第2条日志...")
    logger.log(
        "你好", "你好！有什么可以帮你的？", "chat",
        [], 800.2,
        token_usage={"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130, "cost_rmb": 0.00016},
    )

    print("记录第3条日志...")
    logger.log(
        "Python 是什么？", "Python 是一种编程语言...", "knowledge",
        [mock_docs(), mock_docs()], 2100.3,
        token_usage={"prompt_tokens": 800, "completion_tokens": 350, "total_tokens": 1150, "cost_rmb": 0.0015},
    )

    # 查看统计
    print("\n📊 统计报告（含 Token 和费用）:")
    stats = logger.get_stats()
    for k, v in stats.items():
        print(f"   {k}: {v}")

    # 清理
    import os
    os.remove("test_qa_log.jsonl")
    print(f"\n🎉 评测日志模块测试完成！")

