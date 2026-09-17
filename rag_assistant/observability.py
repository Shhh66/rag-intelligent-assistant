"""可观测性 —— LangFuse 全链路追踪的降级安全封装。

设计：
- LANGFUSE_ENABLED=False 或未配密钥或 SDK 不可用 → 所有接口变 no-op（绝不阻断主链路）。
- 用同一个 trace_id 贯穿（与 tool_audit 共享），一次打通"结构化审计 + 可视化 trace"。
- span/generation 上报全程 try/except，异常静默。

用法：
    from observability import obs_span, obs_generation, flush_obs
    with obs_span("检索", trace_id=tid, metadata={"retrieve_channel":"hybrid"}, level="DEFAULT"):
        ...
    obs_generation(trace_id=tid, name="rag_answer", model=..., usage=..., input=..., output=...)
"""

import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_client = None
_init_done = False


def _get_client():
    """懒加载 LangFuse client；未启用/未配置/异常 → None（no-op）。"""
    global _client, _init_done
    if _init_done:
        return _client
    _init_done = True
    try:
        import config
        if not getattr(config, "LANGFUSE_ENABLED", False):
            return None
        pk = getattr(config, "LANGFUSE_PUBLIC_KEY", "")
        sk = getattr(config, "LANGFUSE_SECRET_KEY", "")
        host = getattr(config, "LANGFUSE_HOST", "http://localhost:3000")
        if not pk or not sk:
            logger.warning("LangFuse 已启用但缺少密钥，降级为 no-op")
            return None
        from langfuse import Langfuse
        _client = Langfuse(public_key=pk, secret_key=sk, host=host)
        logger.info(f"LangFuse 已连接: {host}")
    except Exception as e:
        logger.warning(f"LangFuse 初始化失败(降级 no-op): {e}")
        _client = None
    return _client


# 当前请求的观测上下文。主进程由 set_obs_context() 显式设置；
# MCP 子进程是 per-request 新建的、拿不到主进程状态，故回落到读环境变量。
_ctx = {"trace_id": "", "user_id": "", "session_id": ""}


def set_obs_context(trace_id="", user_id="", session_id=""):
    """设置当前请求的观测上下文（由主进程 chat 入口调用）。"""
    _ctx["trace_id"] = trace_id or ""
    _ctx["user_id"] = user_id or ""
    _ctx["session_id"] = session_id or ""


def _ctx_value(key: str, env_name: str) -> str:
    """取上下文值：主进程读 _ctx，子进程回落环境变量。

    环境变量兜底的原因：检索链路（retriever 等）跑在 MCP 子进程内，
    拿不到主进程的上下文 —— 不兜底则子进程的 span 会各自变成孤立 trace，
    且丢失 user / session 归因。
    """
    v = _ctx.get(key) or ""
    if v:
        return v
    try:
        return os.getenv(env_name, "") or ""
    except Exception:
        return ""


def _get_trace(client, trace_id):
    """按 trace_id 取/建 trace，并带上 name / user_id / session_id 便于检索归因。

    LangFuse v2 SDK 用 client.trace(id=...) 引用 trace，同 id 幂等（服务端 upsert），
    所以每个 span/generation 各自取一次是安全的，不会产生重复 trace。
    trace_id 为 32 位小写 hex（uuid4().hex），符合服务端要求。
    """
    kwargs = {"name": "chat"}
    tid = trace_id or _ctx_value("trace_id", "MCP_TRACE_ID")
    if tid:
        kwargs["id"] = tid
    uid = _ctx_value("user_id", "MCP_USER_ID")
    if uid:
        kwargs["user_id"] = uid
    sid = _ctx_value("session_id", "MCP_SESSION_ID")
    if sid:
        kwargs["session_id"] = sid
    return client.trace(**kwargs)


@contextmanager
def obs_span(name, trace_id="", metadata=None, level="DEFAULT", input=None):
    """一个 span 上下文管理器；LangFuse 不可用时是纯 no-op。"""
    client = _get_client()
    if client is None:
        yield None
        return
    span = None
    try:
        span = _get_trace(client, trace_id).span(
            name=name,
            metadata=metadata or {},
            level=level,
            input=input,
        )
    except Exception as e:
        logger.debug(f"obs_span 创建失败(忽略): {e}")
        span = None
    try:
        yield span
    finally:
        if span is not None:
            try:
                span.end()
            except Exception:
                pass


def obs_generation(trace_id="", name="llm", model=None, usage=None,
                   input=None, output=None, metadata=None, latency_ms=None):
    """记录一次 LLM generation（含 token usage 与耗时），供成本归因。no-op 安全。

    注意（langfuse 2.x 专有）：token 必须走 usage= 参数。实测 SDK 2.60.10 上
    usage_details= 会被服务端存成全 0，只有 usage= 能正确落库（unit 需 "TOKENS"）。
    """
    client = _get_client()
    if client is None:
        return
    try:
        usage_payload = None
        if usage is not None:
            raw = {
                "input": getattr(usage, "prompt_tokens", None),
                "output": getattr(usage, "completion_tokens", None),
                "total": getattr(usage, "total_tokens", None),
            }
            raw = {k: v for k, v in raw.items() if v is not None}
            if raw:
                raw["unit"] = "TOKENS"
                usage_payload = raw

        end_t = datetime.now(timezone.utc)
        start_t = end_t - timedelta(milliseconds=latency_ms) if latency_ms else end_t

        gen = _get_trace(client, trace_id).generation(
            name=name,
            model=model,
            usage=usage_payload,
            input=input,
            output=output,
            metadata=metadata or {},
            start_time=start_t,
            end_time=end_t,
        )
        gen.end()
    except Exception as e:
        logger.debug(f"obs_generation 失败(忽略): {e}")


# ── 入参/出参的脱敏截断（上报前的最后一道关）──
# messages 里含基座模板、历史摘要、长期记忆注入、检索结果，全量上传既不安全也撑爆存储，
# 故逐条截断：只保留「模型收到了什么角色、开头说了什么」这一层信息。

def _truncate(text, limit: int) -> str:
    """截断长文本并标注省略量，便于一眼看出被裁了多少。"""
    text = "" if text is None else str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...(+{len(text) - limit})"


def _io_maxlen() -> int:
    try:
        from config import LANGFUSE_IO_MAXLEN
        return LANGFUSE_IO_MAXLEN
    except Exception:
        return 500


def summarize_messages(messages):
    """把 OpenAI messages 压成可上报摘要：逐条截断 + 保留 role。

    system 消息（基座模板 + 【历史摘要】）与其他 role 同样按上限截断——
    只露出开头的角色定义，历史摘要与记忆注入不会被全量带出。
    """
    if not messages:
        return None
    limit = _io_maxlen()
    out = []
    for m in messages:
        if not isinstance(m, dict):
            out.append({"role": "?", "content": _truncate(m, limit)})
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        out.append({"role": m.get("role", "?"), "content": _truncate(content, limit)})
    return out


def extract_output(resp):
    """从回包提取 (正文, 思考过程)，各自截断。

    reasoning_content 是推理模型特有的思维链，单独返回供调用方塞进 metadata，
    不占 output 字段——否则正文会被思考过程挤没。
    返回 (None, None) 表示回包结构异常，调用方按「无输出」处理。
    """
    try:
        msg = resp.choices[0].message
        content = _truncate(getattr(msg, "content", "") or "", _io_maxlen())
        reasoning = getattr(msg, "reasoning_content", None)
        return content, (_truncate(reasoning, _io_maxlen()) if reasoning else None)
    except Exception:
        return None, None


def flush_obs():
    """flush 待上报数据（chat 结束时调用）。no-op 安全。"""
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception:
        pass


# ── 自测 ──
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print("LANGFUSE client:", _get_client())
    with obs_span("test-span", trace_id="a" * 32, metadata={"k": "v"}) as s:
        print("span:", s)
    print("✅ observability no-op/连接自测完成（未启用时应全为 None）")
