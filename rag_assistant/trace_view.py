"""请求链路查看器 —— 从审计日志还原每一次提问的完整流程。

数据来源：`tool_audit.py` 写入的 JSONL（默认 `runtime_data/tool_audit.jsonl`），
两种记录混在一起，靠字段区分：

- **工具调用**：`tool_name` / `arguments` / `result_preview` / `latency_ms` / `success` / `retry_count` / `error`
- **决策**：`type="decision"`，含 `plan` / `thought` / `evaluation` / `decision` / `turn`

同一次提问的所有记录共享一个 `trace_id`，按它分组即可还原完整链路。

用法：
    python trace_view.py                    # 列出最近 10 次请求
    python trace_view.py -n 20              # 列出最近 20 次
    python trace_view.py --last             # 展开最近一次请求的完整流程
    python trace_view.py -t 77c40d11        # 展开指定请求（trace_id 支持前缀匹配）
    python trace_view.py --failed            # 只列出有工具失败的请求
    python trace_view.py --full             # 展开时不截断长文本
    python trace_view.py --file <path>      # 指定审计文件
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

# 长文本截断长度（--full 时忽略）
_TRUNC = 160


# ── 数据加载 ──────────────────────────────────────────────────

def find_audit_file(explicit: str | None = None) -> Path:
    """定位审计文件：显式指定 > config 配置 > runtime_data/ > 项目根目录。"""
    if explicit:
        return Path(explicit)
    candidates: list[Path] = []
    try:
        import config
        candidates.append(Path(config.TOOL_AUDIT_PATH))
    except Exception:
        pass
    here = Path(__file__).resolve().parent
    candidates += [here / "runtime_data" / "tool_audit.jsonl",
                   here / "tool_audit.jsonl"]
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def load_records(path: Path) -> list[dict]:
    """读取审计 JSONL，坏行跳过。"""
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def is_tool_call(r: dict) -> bool:
    return "tool_name" in r


def group_by_trace(records: list[dict]) -> dict[str, list[dict]]:
    """按 trace_id 分组，组内按时间升序。"""
    groups: dict[str, list[dict]] = {}
    for r in records:
        tid = r.get("trace_id") or "(无 trace_id)"
        groups.setdefault(tid, []).append(r)
    for tid in groups:
        groups[tid].sort(key=lambda r: r.get("timestamp", ""))
    return groups


def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return None


def summarize(recs: list[dict]) -> dict:
    """汇总一个 trace 的关键指标。"""
    tools = [r for r in recs if is_tool_call(r)]
    rounds = [r for r in recs
              if r.get("type") == "decision" and r.get("action") != "judge"]
    failed = [r for r in tools if not r.get("success")]

    times = [t for t in (_parse_ts(r.get("timestamp", "")) for r in recs) if t]
    span = (max(times) - min(times)).total_seconds() if len(times) >= 2 else 0.0

    return {
        "trace_id": recs[0].get("trace_id", "") if recs else "",
        "start": min(times) if times else None,
        "span_s": span,
        "rounds": len(rounds),
        "tools": len(tools),
        "failed": len(failed),
        "ok": not failed,
    }


def _trunc(text: str, full: bool, limit: int = _TRUNC) -> str:
    text = str(text or "").replace("\n", " ⏎ ").strip()
    if full or len(text) <= limit:
        return text
    return text[:limit] + " …"


# ── 列表视图 ──────────────────────────────────────────────────

def cmd_list(groups: dict[str, list[dict]], n: int, only_failed: bool,
             path: Path) -> None:
    all_items = [summarize(recs) for recs in groups.values()]
    all_items.sort(key=lambda s: s["start"] or datetime.min, reverse=True)
    n_failed = sum(1 for s in all_items if not s["ok"])
    items = [s for s in all_items if not s["ok"]] if only_failed else all_items
    items = items[:n]

    print(f"\n审计文件: {path}")
    if only_failed:
        print(f"共 {len(all_items)} 次请求，其中有工具失败的 {n_failed} 次；"
              f"显示最近 {len(items)} 次：\n")
    else:
        print(f"共 {len(all_items)} 次请求（失败 {n_failed} 次），"
              f"显示最近 {len(items)} 次：\n")
    if not items:
        print("  （无匹配记录）\n")
        return

    print(f"  {'#':>2}  {'trace_id':10s}  {'时间':16s}  {'轮次':>4}  {'工具':>4}  "
          f"{'失败':>4}  {'跨度':>8}  状态")
    print("  " + "─" * 72)
    for i, s in enumerate(items, 1):
        ts = s["start"].strftime("%m-%d %H:%M:%S") if s["start"] else "?"
        mark = "✅" if s["ok"] else "❌"
        print(f"  {i:>2}  {s['trace_id'][:10]:10s}  {ts:16s}  {s['rounds']:>4}  "
              f"{s['tools']:>4}  {s['failed']:>4}  {s['span_s']:>7.1f}s  {mark}")
    print(f"\n  展开某次：python trace_view.py -t <trace_id>")
    print(f"  展开最近一次：python trace_view.py --last\n")


# ── 详情视图 ──────────────────────────────────────────────────

def cmd_detail(trace_id: str, recs: list[dict], full: bool, path: Path) -> None:
    s = summarize(recs)
    start = s["start"].strftime("%Y-%m-%d %H:%M:%S") if s["start"] else "?"
    print(f"\n审计文件: {path}")
    print("═" * 76)
    print(f"trace {trace_id}")
    print(f"起点 {start}   跨度 {s['span_s']:.1f}s   "
          f"决策轮次 {s['rounds']}   工具调用 {s['tools']}   "
          f"失败 {s['failed']}")
    print("═" * 76)

    turn = -1
    for r in recs:
        ts = str(r.get("timestamp", ""))[11:19]

        if is_tool_call(r):
            ok = r.get("success")
            retry = r.get("retry_count") or 0
            head = (f"\n  [工具] {ts}  {'✅' if ok else '❌'} "
                    f"{r.get('tool_name')}   {r.get('latency_ms', 0):.0f}ms")
            if retry:
                head += f"   重试 {retry} 次"
            print(head)
            print(f"         入参: {_trunc(json.dumps(r.get('arguments', {}), ensure_ascii=False), full)}")
            if ok:
                print(f"         结果: {_trunc(r.get('result_preview', ''), full)}")
            else:
                print(f"         错误: {_trunc(r.get('error', ''), full)}")
                if r.get("result_preview"):
                    print(f"         返回: {_trunc(r.get('result_preview', ''), full)}")
            continue

        # 决策记录
        action = r.get("action", "")
        if action == "judge":
            print(f"\n  [判定] {ts}  Judge → {r.get('evaluation', '')} "
                  f"(decision={r.get('decision', '')})")
            continue

        turn += 1
        print(f"\n  ── 第 {turn + 1} 轮决策 ──  {ts}")
        print(f"         行动: {action}   decision={r.get('decision', '')}"
              + (f"   tools={r.get('tool_names')}" if r.get("tool_names") else "")
              + (f"   skill={r.get('skill_name')}" if r.get("skill_name") else ""))
        for label, key in (("规划", "plan"), ("推理", "thought"),
                           ("评估", "evaluation")):
            val = r.get(key)
            if val:
                print(f"         {label}: {_trunc(val, full)}")

    print("\n" + "═" * 76 + "\n")


# ── 入口 ──────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="查看每一次提问的完整链路（读 tool_audit.jsonl）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-n", "--num", type=int, default=10, help="列出最近 N 次（默认 10）")
    ap.add_argument("-t", "--trace", help="展开指定 trace_id（支持前缀匹配）")
    ap.add_argument("--last", action="store_true", help="展开最近一次请求")
    ap.add_argument("--failed", action="store_true", help="只列出有工具失败的请求")
    ap.add_argument("--full", action="store_true", help="展开时显示完整文本，不截断")
    ap.add_argument("--file", help="指定审计文件路径")
    args = ap.parse_args()

    path = find_audit_file(args.file)
    records = load_records(path)
    if not records:
        print(f"\n未读到任何记录：{path}")
        print("（提示：重启 app 后审计才会写入该位置；旧数据可能在项目根目录）\n")
        return 1

    groups = group_by_trace(records)

    # 展开模式
    if args.last or args.trace:
        if args.last:
            tid = max(groups, key=lambda k: max(
                (r.get("timestamp", "") for r in groups[k]), default=""))
        else:
            matches = [k for k in groups if k.startswith(args.trace)]
            if not matches:
                print(f"\n找不到匹配的 trace_id：{args.trace}\n")
                return 1
            if len(matches) > 1:
                print(f"\n前缀 {args.trace} 匹配到 {len(matches)} 个，请给更长的前缀：")
                for m in matches:
                    print(f"  {m}")
                print()
                return 1
            tid = matches[0]
        cmd_detail(tid, groups[tid], args.full, path)
        return 0

    cmd_list(groups, args.num, args.failed, path)
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
