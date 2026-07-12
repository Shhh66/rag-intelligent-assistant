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

import logging
from contextlib import contextmanager

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


def _trace_ctx(trace_id):
    """把我们的 trace_id 转成 LangFuse trace_context。"""
    if not trace_id:
        return None
    try:
        # LangFuse 要求 trace_id 为 32 位小写 hex，uuid4().hex 正好满足
        return {"trace_id": trace_id}
    except Exception:
        return None


@contextmanager
def obs_span(name, trace_id="", metadata=None, level="DEFAULT", input=None):
    """一个 span 上下文管理器；LangFuse 不可用时是纯 no-op。"""
    client = _get_client()
    if client is None:
        yield None
        return
    span = None
    try:
        span = client.start_observation(
            name=name,
            as_type="span",
            trace_context=_trace_ctx(trace_id),
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
                   input=None, output=None, metadata=None):
    """记录一次 LLM generation（含 token usage），供成本归因。no-op 安全。"""
    client = _get_client()
    if client is None:
        return
    try:
        usage_details = None
        if usage is not None:
            usage_details = {
                "input": getattr(usage, "prompt_tokens", None),
                "output": getattr(usage, "completion_tokens", None),
                "total": getattr(usage, "total_tokens", None),
            }
        gen = client.start_observation(
            name=name,
            as_type="generation",
            trace_context=_trace_ctx(trace_id),
            model=model,
            usage_details=usage_details,
            input=input,
            output=output,
            metadata=metadata or {},
        )
        gen.end()
    except Exception as e:
        logger.debug(f"obs_generation 失败(忽略): {e}")


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
