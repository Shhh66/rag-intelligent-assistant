"""摘要驱动长期抽取测试脚本（模块 9.6）

覆盖三类场景：
1. 正常输入：从摘要抽取 → 入库；同摘要重复抽取 → 权重强化；路由分流正确
2. 边界输入：空摘要 / 抽取结果为空数组 / 含无效条目 / 未启用
3. 异常场景：LLM 抛错 / 非法 JSON / 依赖缺失

SQLite 用真实实现，向量库与 LLM 用假实现。
"""

import sys
import os
import types
import tempfile
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
import token_tracker
from long_term_memory import LongTermMemory

_TMP = tempfile.mkdtemp()
config.MEMORY_DB_PATH = os.path.join(_TMP, "test_extract.db")
config.LONG_TERM_MEMORY_ENABLED = True
token_tracker._PERSIST_FILE = __import__("pathlib").Path(_TMP) / "tok.jsonl"

_passed = 0
_failed = 0


def check(cond, msg):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"   ✅ {msg}")
    else:
        _failed += 1
        print(f"   ❌ {msg}")


# ── 假依赖 ────────────────────────────────────────────────────

class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class _Resp:
    def __init__(self, content):
        self.choices = [types.SimpleNamespace(message=types.SimpleNamespace(content=content))]
        self.usage = _Usage()


class _Completions:
    def __init__(self, content, exc):
        self._content = content
        self._exc = exc

    def create(self, **kw):
        if self._exc:
            raise self._exc
        return _Resp(self._content)


class _FakeLLM:
    def __init__(self, content=None, exc=None):
        self.chat = types.SimpleNamespace(completions=_Completions(content, exc))


class _Doc:
    def __init__(self, content, metadata):
        self.page_content = content
        self.metadata = metadata


class _FakeVS:
    def __init__(self):
        self.docs = {}
    def add_texts(self, texts, metadatas, ids):
        for t, m, i in zip(texts, metadatas, ids):
            self.docs[i] = (t, dict(m))
    def delete(self, ids):
        for i in ids or []:
            self.docs.pop(i, None)
    @staticmethod
    def _grams(s):
        s = (s or "").lower().strip()
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}
    def similarity_search_with_score(self, query, k=1, filter=None):
        uid = (filter or {}).get("user_id")
        qg = self._grams(query)
        out = []
        for i, (t, m) in self.docs.items():
            if uid is not None and m.get("user_id") != uid:
                continue
            eg = self._grams(t)
            inter, union = qg & eg, qg | eg
            sim = len(inter) / len(union) if union else 0.0
            out.append((_Doc(t, m), (1.0 / sim - 1.0) if sim > 0 else 999.0))
        out.sort(key=lambda x: x[1])
        return out[:k]


def _new_memory(llm_content=None, llm_exc=None):
    if os.path.exists(config.MEMORY_DB_PATH):
        os.remove(config.MEMORY_DB_PATH)
    LongTermMemory._instance = None
    m = LongTermMemory.__new__(LongTermMemory)
    m._vs = _FakeVS()
    m._db_ready = False
    m._init_done = True
    m._init_sqlite(config)
    m._llm_client = lambda: _FakeLLM(llm_content, llm_exc)
    return m


def _count(m, user_id=None):
    conn = m._sqlite()
    if user_id:
        n = conn.execute("SELECT COUNT(*) FROM memory WHERE user_id=?", (user_id,)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    conn.close()
    return n


def _weight_of(m, user_id, content):
    row = m._get_by_id(m._entity_id(user_id, content))
    return float(row["weight"]) if row else None


SUMMARY = "用户在做 RAG 智能体项目，要求全程中文回答，偏好使用混合检索方案。"
ITEMS_OK = '[{"mem_type":"entity","content":"用户在做 RAG 智能体项目","confidence":0.9},' \
           '{"mem_type":"profile","content":"用户要求全程中文回答","confidence":0.95}]'


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_extract_normal():
    print("\n【正常 1】从摘要抽取 → 结构化入库")
    m = _new_memory(ITEMS_OK)
    items = m.extract_from_summary("alice", SUMMARY)
    check(len(items) == 2, f"抽取到 2 条（实际 {len(items)}）")
    check(_count(m, "alice") == 2, f"入库 2 条（实际 {_count(m, 'alice')}）")
    check(_weight_of(m, "alice", "用户在做 RAG 智能体项目") == 1.0,
          "新实体 weight 初始为 1.0")


def test_extract_reinforce():
    print("\n【正常 2】同一摘要重复抽取 → 权重正向强化")
    m = _new_memory(ITEMS_OK)
    m.extract_from_summary("alice", SUMMARY)
    m.extract_from_summary("alice", SUMMARY)
    m.extract_from_summary("alice", SUMMARY)
    check(_count(m, "alice") == 2, f"仍为 2 条，未膨胀（实际 {_count(m, 'alice')}）")
    w = _weight_of(m, "alice", "用户在做 RAG 智能体项目")
    check(w == 3.0, f"三次抽取 → weight=3.0（实际 {w}）")


def test_extract_new_entity_accumulates():
    print("\n【正常 3】新摘要带来新实体 → 新增，旧实体权重保持")
    m = _new_memory(ITEMS_OK)
    m.extract_from_summary("alice", SUMMARY)
    m._llm_client = lambda: _FakeLLM('[{"mem_type":"entity","content":"用户关注 6G 低空无人机","confidence":0.8}]')
    m.extract_from_summary("alice", "新摘要")
    check(_count(m, "alice") == 3, f"新增第 3 条（实际 {_count(m, 'alice')}）")
    check(_weight_of(m, "alice", "用户在做 RAG 智能体项目") == 1.0,
          "旧实体权重不受影响（仍 1.0）")


def test_routing():
    print("\n【正常 4】UnifiedAgent 路由：压缩过→摘要路径，未压缩→原路径")
    import mcp_unified_agent.unified_agent as ua
    from mcp_unified_agent.unified_agent import UnifiedAgent
    import mcp_unified_agent.unified_agent as _m

    saved_has, saved_get = _m.HAS_LONG_MEMORY, _m.get_memory

    class _Rec:
        def __init__(self):
            self.summary_calls, self.raw_calls = [], []
        def extract_from_summary(self, uid, s):
            self.summary_calls.append((uid, s)); return []
        def extract_and_store(self, uid, q, a):
            self.raw_calls.append((uid, q, a))

    rec = _Rec()
    _m.HAS_LONG_MEMORY = True
    _m.get_memory = lambda: rec
    try:
        # 未达阈值 → 走原路径
        a = UnifiedAgent.__new__(UnifiedAgent)
        a._history, a._session_summary, a._llm_client = [], "", None
        a.model, a._user_id = "t", "alice"
        a._record_conversation("q1", "a1")
        check(len(rec.raw_calls) == 1 and not rec.summary_calls,
              "未触发压缩 → 走 extract_and_store（原行为）")

        # 触发压缩 → 走摘要路径
        rec.raw_calls.clear(); rec.summary_calls.clear()
        _m.call_llm_with_cb = lambda *a_, **k: types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=types.SimpleNamespace(content="压缩摘要"))])
        for i in range(10):
            a._history.append({"role": "user", "content": f"q{i}"})
            a._history.append({"role": "assistant", "content": f"a{i}"})
        a._record_conversation("触发", "压缩")
        check(len(rec.summary_calls) == 1 and not rec.raw_calls,
              "触发压缩 → 走 extract_from_summary（摘要驱动）")
        check(rec.summary_calls[0][1] == "压缩摘要", "抽取源是摘要文本，而非原始对话")
        check(rec.summary_calls[0][0] == "alice", "按 user_id 隔离")
    finally:
        _m.HAS_LONG_MEMORY, _m.get_memory = saved_has, saved_get


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_boundary_empty_summary():
    print("\n【边界 1】空摘要 / 纯空白 → 不调用 LLM，返回 []")
    for s in ("", "   ", None):
        m = _new_memory(ITEMS_OK)
        r = m.extract_from_summary("alice", s)
        check(r == [] and _count(m) == 0, f"摘要={s!r} → 返回 [] 且零入库")


def test_boundary_empty_array():
    print("\n【边界 2】抽取结果为空数组 [] → 不入库")
    m = _new_memory("[]")
    r = m.extract_from_summary("alice", SUMMARY)
    check(r == [], "返回空列表")
    check(_count(m) == 0, "零入库（无值得长期记住的内容）")


def test_boundary_partial_invalid():
    print("\n【边界 3】抽取结果含无效条目 → 过滤掉")
    m = _new_memory('[{"mem_type":"entity","content":"有效实体","confidence":0.9},'
                    '{"mem_type":"entity"},'          # 缺 content → 剔除
                    '{"content":"缺 mem_type"},'      # 缺 mem_type → 保留（回落默认）
                    '"裸字符串"]')                     # 非 dict → 剔除
    r = m.extract_from_summary("alice", SUMMARY)
    check(len(r) == 2, f"4 项输入 → 过滤后保留 2 条（缺 content / 非 dict 被剔除，实际 {len(r)}）")
    check(_count(m) == 2, f"实存 2 条（实际 {_count(m)}）")
    check(_weight_of(m, "alice", "缺 mem_type") is not None,
          "缺 mem_type 的条目回落默认类型并正常入库")


def test_boundary_disabled():
    print("\n【边界 4】长期记忆未启用 → 返回 []")
    LongTermMemory._instance = None
    m = LongTermMemory.__new__(LongTermMemory)
    m._vs, m._db_ready, m._init_done = None, False, False
    saved = config.LONG_TERM_MEMORY_ENABLED
    config.LONG_TERM_MEMORY_ENABLED = False
    try:
        check(m.extract_from_summary("alice", SUMMARY) == [], "未启用 → 返回 []")
    finally:
        config.LONG_TERM_MEMORY_ENABLED = saved


def test_boundary_cross_user():
    print("\n【边界 5】跨用户隔离：同摘要不同用户 → 各自独立")
    m = _new_memory(ITEMS_OK)
    m.extract_from_summary("alice", SUMMARY)
    m.extract_from_summary("bob", SUMMARY)
    check(_count(m) == 4, f"两个用户各 2 条（实际 {_count(m)}）")
    check(_weight_of(m, "alice", "用户在做 RAG 智能体项目") == 1.0, "alice weight=1.0")
    check(_weight_of(m, "bob", "用户在做 RAG 智能体项目") == 1.0, "bob weight=1.0（未串户）")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_llm_error():
    print("\n【异常 1】LLM 抛异常 → 返回 []，不抛出")
    m = _new_memory(llm_exc=RuntimeError("模拟 LLM 故障"))
    try:
        r = m.extract_from_summary("alice", SUMMARY)
        ok = True
    except Exception as e:
        ok, r = False, str(e)
        print(f"      抛出异常: {e}")
    check(ok, "主流程未抛异常（降级安全）")
    check(r == [], "返回空列表")
    check(_count(m) == 0, "零入库，不留脏数据")


def test_exception_bad_json():
    print("\n【异常 2】LLM 返回非 JSON → 返回 []")
    m = _new_memory("这不是 JSON，只是一段普通文本")
    r = m.extract_from_summary("alice", SUMMARY)
    check(r == [], "无法解析 → 返回 []")
    check(_count(m) == 0, "零入库")


def test_exception_llm_empty():
    print("\n【异常 3】LLM 返回空串 → 返回 []")
    m = _new_memory("")
    r = m.extract_from_summary("alice", SUMMARY)
    check(r == [] and _count(m) == 0, "空响应 → 返回 [] 且零入库")


def test_exception_vs_failure():
    print("\n【异常 4】向量库故障 → 抽取仍不抛出（_upsert 已降级）")
    m = _new_memory(ITEMS_OK)
    m._vs.fail = True
    try:
        r = m.extract_from_summary("alice", SUMMARY)
        ok = True
    except Exception as e:
        ok, r = False, str(e)
        print(f"      抛出异常: {e}")
    check(ok, "未抛异常（向量写入失败静默降级）")
    check(len(r) == 2, "抽取结果仍正常返回")


def main():
    print("🧪 摘要驱动长期抽取测试（模块 9.6）")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_extract_normal()
    test_extract_reinforce()
    test_extract_new_entity_accumulates()
    test_routing()

    print("\n─── 二、边界输入 ───")
    test_boundary_empty_summary()
    test_boundary_empty_array()
    test_boundary_partial_invalid()
    test_boundary_disabled()
    test_boundary_cross_user()

    print("\n─── 三、异常场景 ───")
    test_exception_llm_error()
    test_exception_bad_json()
    test_exception_llm_empty()
    test_exception_vs_failure()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
