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



