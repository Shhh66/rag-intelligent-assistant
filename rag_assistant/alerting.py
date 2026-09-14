"""熔断器告警监控（轻量 MVP）

职责：监控各下游熔断器状态，检测到「熔断打开」立即告警，「熔断恢复」可通知。
告警输出三通道：stderr 日志 + alert.log 落盘 + 可选 webhook（钉钉/飞书）。

设计原则（零破坏）：
- 不修改任何已有模块，仅新增本文件。
- 通过 check_circuit_breakers() 轮询熔断器状态，或 start_monitor() 起后台线程。
- 熔断状态迁移语义：CLOSED/HALF_OPEN → OPEN 告警「熔断打开」；OPEN → CLOSED 告警「熔断恢复」。

用法：
    from alerting import check_circuit_breakers, start_monitor
    check_circuit_breakers()          # 手动检查一次
    start_monitor(interval=5.0)       # 后台每 5s 检查一次

演示：直接 `python alerting.py`（模拟熔断打开→告警→恢复→告警）。
"""

import json
import logging
import os
import threading
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# 先触发 config 加载（内部 load_dotenv 注入 .env），否则本模块先于 config 被导入时
# RUNTIME_DATA_DIR 还没进环境变量，路径会退回项目目录
try:
    import config as _config  # noqa: F401
except Exception:
    pass

# 告警落盘文件：设了 RUNTIME_DATA_DIR 就放进去（Docker 挂载卷持久化），
# 否则用项目目录（原行为）
_ALERT_FILE = Path(
    os.getenv("RUNTIME_DATA_DIR") or Path(__file__).resolve().parent
) / "alert.log"

# 状态跟踪：destination → 上次状态（用于检测状态迁移，避免重复告警）
_prev_state: dict[str, str] = {}
_lock = threading.Lock()


def _get_webhook_url() -> str:
    """懒读取 webhook URL：import config 触发 load_dotenv，再读 .env 配置。"""
    try:
        import config
        return getattr(config, "ALERT_WEBHOOK_URL", "") or ""
    except Exception:
        return ""


def _emit_alert(destination: str, event: str, detail: str):
    """写一条告警：stderr + alert.log + 可选 webhook。失败静默，不阻断主链路。"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"[ALERT] {event}: 下游「{destination}」 {detail}"
    logger.warning(msg)
    try:
        with open(_ALERT_FILE, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass
    _notify_webhook(msg)


def _notify_webhook(msg: str):
    """预留 webhook 通知（钉钉/飞书）。MVP：配置了 URL 就发，失败静默。"""
    url = _get_webhook_url()
    if not url:
        return
    try:
        payload = json.dumps(
            {"msgtype": "text", "text": {"content": msg}}
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=3)
        logger.info("[ALERT] webhook 已发送")
    except Exception as e:
        logger.warning(f"[ALERT] webhook 发送失败: {e}")


def check_circuit_breakers() -> list[tuple[str, str]]:
    """检查所有熔断器，检测状态迁移并告警。返回本次触发的事件列表。

    状态迁移语义：
    - CLOSED/HALF_OPEN → OPEN：告警「熔断打开」（下游连续失败达阈值）
    - OPEN → CLOSED：告警「熔断恢复」（下游恢复正常）
    """
    from mcp_unified_agent.circuit_breaker import (
        get_breaker,
        DESTINATION_MCP_CHANNEL,
        DESTINATION_LLM,
        DESTINATION_WEATHER,
        DESTINATION_EDU,
    )
    destinations = (
        DESTINATION_MCP_CHANNEL,
        DESTINATION_LLM,
        DESTINATION_WEATHER,
        DESTINATION_EDU,
    )
    events: list[tuple[str, str]] = []
    with _lock:
        for dest in destinations:
            try:
                cur = get_breaker(dest).status()["state"]
            except Exception:
                continue
            prev = _prev_state.get(dest)
            if prev is not None and prev != "OPEN" and cur == "OPEN":
                _emit_alert(dest, "熔断打开", f"状态 {prev} → {cur}")
                events.append((dest, "opened"))
            elif prev == "OPEN" and cur == "CLOSED":
                _emit_alert(dest, "熔断恢复", f"状态 {prev} → {cur}")
                events.append((dest, "recovered"))
            _prev_state[dest] = cur
    return events


def start_monitor(interval: float = 5.0) -> threading.Thread:
    """起一个后台守护线程，每 interval 秒检查一次熔断器。返回线程对象。

    集成到 app.py 时用 st.session_state 标志防 Streamlit rerun 重复启动。
    """
    def _loop():
        while True:
            try:
                check_circuit_breakers()
            except Exception:
                pass
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="alerting-monitor")
    t.start()
    return t


# ===== 自测：模拟熔断打开，演示告警 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    from mcp_unified_agent.circuit_breaker import get_breaker, DESTINATION_LLM

    print("=== 熔断器告警 MVP 演示 ===\n")
    breaker = get_breaker(DESTINATION_LLM)

    # 第 0 步：先检查一次，建立状态基线（当前 CLOSED，不告警）
    check_circuit_breakers()
    print(f"[0] 建立基线：熔断器当前状态 = {breaker.status()['state']}")

    # 第 1 步：模拟下游连续失败达到阈值 → 熔断打开
    print(f"\n[1] 模拟下游连续失败 {breaker.failure_threshold} 次 ...")
    for _ in range(breaker.failure_threshold):
        breaker.record_failure()
    print(f"    熔断器状态 = {breaker.status()['state']}")

    # 第 2 步：检查 → 应触发「熔断打开」告警
    print("\n[2] 检查（应告警「熔断打开」）:")
    events = check_circuit_breakers()
    print(f"    触发事件 = {events}")

    # 第 3 步：再查一次（状态未变，不应重复告警）
    print("\n[3] 再次检查（状态未变，不应重复告警）:")
    events = check_circuit_breakers()
    print(f"    触发事件 = {events}（应为空）")

    # 第 4 步：模拟恢复 → 熔断器 CLOSED
    print("\n[4] 模拟下游恢复 ...")
    breaker.record_success()
    print(f"    熔断器状态 = {breaker.status()['state']}")

    # 第 5 步：检查 → 应触发「熔断恢复」告警
    print("\n[5] 检查（应告警「熔断恢复」）:")
    events = check_circuit_breakers()
    print(f"    触发事件 = {events}")

    # 打印落盘文件
    print(f"\n=== 告警落盘文件: {_ALERT_FILE} ===")
    if _ALERT_FILE.exists():
        print(_ALERT_FILE.read_text(encoding="utf-8"))
    else:
        print("（无告警记录）")

    print("\n✅ 熔断器告警 MVP 演示完成")
