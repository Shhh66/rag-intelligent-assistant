"""工具调用审计 —— 结构化落盘 + trace_id 贯穿。

每次 MCP 工具调用落一条 JSONL 记录，含 trace_id / 工具名 / 入参(脱敏) / 结果摘要 /
耗时 / 成败 / 重试次数。一次 chat 的多次工具调用共享同一 trace_id，可归并为调用链。

设计：单例、实时追加（仿 token_tracker._persist_record）、全程 try/except 不阻断主链路。
"""

import os
import sys
import json
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _cfg():
    """读取审计配置（安全降级）。"""
    try:
        import config
        return (
            getattr(config, "TOOL_AUDIT_ENABLED", True),
            getattr(config, "TOOL_AUDIT_PATH", "./tool_audit.jsonl"),
            getattr(config, "TOOL_AUDIT_ARG_MAXLEN", 500),
            getattr(config, "TOOL_AUDIT_SENSITIVE_KEYS",
                    ["api_key", "secret", "token", "password"]),
        )
    except Exception:
        return True, "./tool_audit.jsonl", 500, ["api_key", "secret", "token", "password"]


def _audit_path(path: str) -> str:
    """相对路径锚定到项目根目录，兼容任意 CWD 启动。"""
    if os.path.isabs(path):
        return path
    return os.path.join(_THIS_DIR, os.path.basename(path))


def _sanitize_args(args: dict, maxlen: int, sensitive: list) -> dict:
    """入参脱敏：敏感键掩码、长文本截断。"""
    if not isinstance(args, dict):
        return {"_raw": str(args)[:maxlen]}
    out = {}
    for k, v in args.items():
        if any(s in k.lower() for s in sensitive):
            out[k] = "***"
        elif isinstance(v, str) and len(v) > maxlen:
            out[k] = v[:maxlen] + f"...(+{len(v) - maxlen})"
        else:
            out[k] = v
    return out


def log_tool_call(
    trace_id: str,
    tool_name: str,
    arguments: dict,
    result_preview: str,
    latency_ms: float,
    success: bool,
    retry_count: int = 0,
    error: str = "",
) -> None:
    """记录一次工具调用审计（失败静默，不阻断主链路）。"""
    enabled, path, maxlen, sensitive = _cfg()
    if not enabled:
        return
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "trace_id": trace_id or "",
            "tool_name": tool_name,
            "arguments": _sanitize_args(arguments, maxlen, sensitive),
            "result_preview": (result_preview or "")[:200],
            "latency_ms": round(latency_ms, 2),
            "success": success,
            "retry_count": retry_count,
            "error": (error or "")[:200],
        }
        with open(_audit_path(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"工具审计写入失败(忽略): {e}")


# ── 自测 ──
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log_tool_call("trace-abc", "query_weather", {"city": "北京", "api_key": "xxx"},
                  "北京晴 25℃", 123.4, True, retry_count=1)
    log_tool_call("trace-abc", "ask_knowledge_base", {"query": "x" * 800},
                  "", 60000, False, error="超时")
    print("✅ 已写两条审计到 tool_audit.jsonl（同 trace-abc）")
