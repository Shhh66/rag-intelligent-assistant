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
    """相对路径锚定到项目根目录，兼容任意 CWD 启动。

    注意：**不能对 path 取 basename** —— 那会把子目录吞掉
    （如 RUNTIME_DATA_DIR 产生的 runtime_data/tool_audit.jsonl
     会被压成 tool_audit.jsonl 落回项目根目录，导致日志分裂成两处）。
    """
    if os.path.isabs(path):
        return path
    return os.path.join(_THIS_DIR, path)


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
    user_id: str = "",
) -> None:
    """记录一次工具调用审计（失败静默，不阻断主链路）。

    user_id：请求级用户身份（可选）。带上后才支持「按用户」聚合，
    供短期高频 → 长期记忆的沉淀判定使用（见 count_tool_successes）。
    """
    enabled, path, maxlen, sensitive = _cfg()
    if not enabled:
        return
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "trace_id": trace_id or "",
            "user_id": user_id or "",
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


def log_decision(
    trace_id: str,
    turn: int,
    action: str,
    thought: str = "",
    tool_names: list = None,
    skill_name: str = "",
    plan: str = "",
    evaluation: str = "",
    decision: str = "",
    user_id: str = "",
) -> None:
    """记录一条 ReAct 决策（基于 ReAct 范式自研五阶段，与工具调用审计同一 jsonl）。

    合规要求「每一步思考可回溯」——记录 Agent 每轮决策的行动类型、思考、拟调工具。
    五阶段：Plan → Thought → Action → Observation → Evaluation → Decision
    用 type="decision" 区分工具调用记录（type 缺省 = 工具调用）。

    Args:
        plan: 规划内容（首轮）
        thought: 推理过程
        evaluation: 评估内容（后续轮次）
        decision: continue / answer / abort
    """
    enabled, path, maxlen, sensitive = _cfg()
    if not enabled:
        return
    try:
        entry = {
            "type": "decision",
            "timestamp": datetime.now().isoformat(),
            "trace_id": trace_id or "",
            "user_id": user_id or "",
            "turn": turn,
            "action": action,
            "thought": (thought or "")[:300],
            "tool_names": tool_names or [],
            "skill_name": skill_name or "",
            "plan": (plan or "")[:500],
            "evaluation": (evaluation or "")[:300],
            "decision": decision or "",
        }
        with open(_audit_path(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.warning(f"决策审计写入失败(忽略): {e}")


def _read_tail_records(path: str, limit: int, max_bytes: int = 256 * 1024) -> list[dict]:
    """只读文件尾部 max_bytes 再解析出最后 limit 条记录（坏行跳过）。

    审计日志是只增不减的，全量扫描会随运行时间线性变慢，故只读尾部。
    从中间截断产生的半行 JSON 解析失败会被跳过，不影响结果。
    """
    out: list[dict] = []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            chunk = f.read().decode("utf-8", errors="ignore")
    except Exception:
        return out
    for line in chunk.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out[-limit:]


def count_tool_successes(user_id: str, limit: int = 100) -> dict:
    """统计某用户最近 limit 次工具调用中的成功次数（按工具名聚合）。

    供「短期高频 → 长期沉淀」做频次判定：原先依赖内存态反思记忆（随会话销毁、
    且拿不到用户维度），改读本模块自己的审计日志后，可跨会话累积且按用户隔离。
    失败返回 {}，绝不阻断主链路。
    """
    enabled, path, _maxlen, _sensitive = _cfg()
    if not enabled or not user_id:
        return {}
    counts: dict[str, int] = {}
    try:
        for rec in _read_tail_records(_audit_path(path), limit):
            if "tool_name" not in rec:          # 跳过 decision 记录
                continue
            if rec.get("user_id") != user_id:
                continue
            if rec.get("success"):
                name = rec.get("tool_name")
                counts[name] = counts.get(name, 0) + 1
    except Exception:
        return {}
    return counts


# ── 自测 ──
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log_tool_call("trace-abc", "query_weather", {"city": "北京", "api_key": "xxx"},
                  "北京晴 25℃", 123.4, True, retry_count=1)
    log_tool_call("trace-abc", "ask_knowledge_base", {"query": "x" * 800},
                  "", 60000, False, error="超时")
    print("✅ 已写两条审计到 tool_audit.jsonl（同 trace-abc）")
