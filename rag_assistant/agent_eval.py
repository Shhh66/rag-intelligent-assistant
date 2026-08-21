"""Agent 端到端轻量评测 —— 从 tool_audit.jsonl 聚合「工具调用成功率 + 端到端延迟」。

方案 A（详见 技术文档/Agent端到端评测.md）：
读 tool_audit.py 产出的审计日志，按 trace_id 分组聚合，零成本（不调 LLM）产出报告。

指标口径（诚实定义）：
  - 工具调用成功率 = 成功调用数 / 总调用数（聚合 success 字段，精确）
  - 工具调用耗时分布 = latency_ms 的均值/中位/P95（精确）
  - 重试率 = retry_count > 0 的调用占比（精确）
  - 端到端延迟 = 同 trace 内 max(timestamp) - min(timestamp)（近似，不含 LLM 首尾推理）

用法：
    python agent_eval.py                    # 读默认 ./tool_audit.jsonl，打印报告
    python agent_eval.py <path.jsonl>       # 指定审计文件
    python agent_eval.py --report           # 额外写出 agent_eval_report.md
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_AUDIT = os.path.join(_THIS_DIR, "tool_audit.jsonl")
REPORT_PATH = os.path.join(_THIS_DIR, "agent_eval_report.md")

MIN_CALLS = 30          # 工具调用样本数低于此值 → 数据量不足告警
MIN_TRACES = 10         # trace 数低于此值 → 端到端延迟样本不足


def _mean(vals):
    return sum(vals) / len(vals) if vals else float("nan")


def _median(vals):
    if not vals:
        return float("nan")
    s = sorted(vals)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def _p95(vals):
    if not vals:
        return float("nan")
    s = sorted(vals)
    idx = int(len(s) * 0.95)
    return s[min(idx, len(s) - 1)]


def _parse_ts(ts):
    """解析 ISO 时间戳，解析失败返回 None。"""
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def load_records(path):
    """逐行读 jsonl，坏行静默跳过（与 tool_audit 的降级安全一致）。"""
    records = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def analyze(records):
    """聚合审计记录，返回统计 dict。"""
    # 分离两类记录：type=="decision" 是 ReAct 决策，其余是工具调用
    tool_calls = [r for r in records if r.get("type") != "decision"]
    decisions = [r for r in records if r.get("type") == "decision"]

    # 按 trace_id 分组（工具调用 + 决策共用同一 trace_id）
    traces = defaultdict(list)
    for r in records:
        traces[r.get("trace_id", "unknown")].append(r)

    # 1. 工具调用成功率
    total = len(tool_calls)
    success = sum(1 for r in tool_calls if r.get("success"))
    success_rate = success / total if total else float("nan")

    # 2. 工具调用耗时分布（ms）
    latencies = [r["latency_ms"] for r in tool_calls
                 if r.get("latency_ms") is not None]

    # 3. 重试率
    retried = sum(1 for r in tool_calls if r.get("retry_count", 0) > 0)
    retry_rate = retried / total if total else float("nan")

    # 4. 端到端延迟（近似）：同 trace 内 max-min timestamp，单位 ms
    e2e = []
    for tid, recs in traces.items():
        tss = [_parse_ts(r.get("timestamp")) for r in recs]
        tss = [t for t in tss if t is not None]
        if len(tss) >= 2:
            e2e.append((max(tss) - min(tss)).total_seconds() * 1000)
    # 只有单条记录的 trace 无法算跨度，单独计数
    single_event_traces = sum(
        1 for recs in traces.values()
        if len([_parse_ts(r.get("timestamp")) for r in recs
                if _parse_ts(r.get("timestamp")) is not None]) < 2
    )

    # 5. 按工具分组：成功率 + 耗时
    by_tool = {}
    for r in tool_calls:
        name = r.get("tool_name", "unknown")
        t = by_tool.setdefault(name, {"total": 0, "success": 0, "latencies": []})
        t["total"] += 1
        if r.get("success"):
            t["success"] += 1
        if r.get("latency_ms") is not None:
            t["latencies"].append(r["latency_ms"])

    return {
        "total_calls": total,
        "success_calls": success,
        "success_rate": success_rate,
        "latency_mean": _mean(latencies),
        "latency_median": _median(latencies),
        "latency_p95": _p95(latencies),
        "retried": retried,
        "retry_rate": retry_rate,
        "e2e_mean": _mean(e2e),
        "e2e_median": _median(e2e),
        "e2e_p95": _p95(e2e),
        "trace_count": len(traces),
        "e2e_trace_count": len(e2e),
        "single_event_traces": single_event_traces,
        "decision_count": len(decisions),
        "by_tool": by_tool,
    }


def _fmt(v, unit=""):
    return f"{v:.1f}{unit}" if v == v else "N/A"  # NaN 判等


def render_report(stats):
    """生成 markdown 报告文本。"""
    lines = []
    lines.append("# Agent 端到端轻量评测报告\n")
    lines.append(f"- 工具调用总数：**{stats['total_calls']}**"
                 f"（决策记录 {stats['decision_count']} 条）")
    lines.append(f"- 工具调用成功率：**{_fmt(stats['success_rate'] * 100, '%')}**"
                 f"（{stats['success_calls']}/{stats['total_calls']}）")
    lines.append(f"- 工具调用耗时：均值 {_fmt(stats['latency_mean'], 'ms')}"
                 f" / 中位 {_fmt(stats['latency_median'], 'ms')}"
                 f" / P95 {_fmt(stats['latency_p95'], 'ms')}")
    lines.append(f"- 重试率：**{_fmt(stats['retry_rate'] * 100, '%')}**"
                 f"（{stats['retried']} 次重试）")
    lines.append("")
    lines.append("## 端到端延迟（近似）\n")
    lines.append(f"- 有效 trace 数：**{stats['e2e_trace_count']}**"
                 f" / 总 trace {stats['trace_count']}"
                 f"（{stats['single_event_traces']} 条单事件 trace 无法算跨度）")
    lines.append(f"- 均值 {_fmt(stats['e2e_mean'], 'ms')}"
                 f" / 中位 {_fmt(stats['e2e_median'], 'ms')}"
                 f" / P95 {_fmt(stats['e2e_p95'], 'ms')}")
    lines.append("")
    lines.append("> ⚠️ 端到端延迟为**近似值**：`tool_audit` 记录的是事件时间点，"
                 "此处以「同 trace 首事件 → 末事件」跨度估算，不含 LLM 首尾推理时间。\n")
    lines.append("## 按工具分组\n")
    lines.append("| 工具 | 调用数 | 成功率 | 平均耗时 |")
    lines.append("|------|-------|--------|---------|")
    for name, t in sorted(stats["by_tool"].items()):
        rate = t["success"] / t["total"] * 100 if t["total"] else float("nan")
        lines.append(f"| {name} | {t['total']} | {_fmt(rate, '%')}"
                     f" | {_fmt(_mean(t['latencies']), 'ms')} |")
    return "\n".join(lines)


def main():
    sys.stdout.reconfigure(encoding="utf-8")

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    path = args[0] if args else DEFAULT_AUDIT
    write_report = "--report" in sys.argv

    if not os.path.exists(path):
        print(f"❌ 审计文件不存在: {path}")
        print("   请先运行 Agent 产生工具调用，或手动传入路径：python agent_eval.py <path.jsonl>")
        sys.exit(1)

    records = load_records(path)
    stats = analyze(records)

    report = render_report(stats)
    print(report)

    # 数据量不足告警
    if stats["total_calls"] < MIN_CALLS:
        print(f"\n⚠️  工具调用样本仅 {stats['total_calls']} 条（建议 ≥ {MIN_CALLS}），"
              f"当前聚合结果无统计意义，请先积累审计数据。")

    if write_report:
        with open(REPORT_PATH, "w", encoding="utf-8") as f:
            f.write(report + "\n")
        print(f"\n📄 报告已写出: {REPORT_PATH}")


if __name__ == "__main__":
    main()
