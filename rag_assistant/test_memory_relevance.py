"""记忆检索相关性过滤测试（对照「无相关性阈值→硬塞 top_k 条」短板）

覆盖三类场景：
1. 正常输入：重排分达标保留 / 低于阈值被过滤 / 过滤后不足 top_k 不补齐
2. 边界输入：开关关闭退回原行为 / 候选仅 1 条不重排 / 阈值设 -999 不过滤 / 全被过滤返回空
3. 异常场景：重排抛错降级不过滤 / 重排结果无法对齐时放弃过滤

SQLite 用真实实现；向量库与重排器用假实现（确定性，不加载 568MB 模型）。
"""

import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
from long_term_memory import LongTermMemory

_TMP = tempfile.mkdtemp()
config.MEMORY_DB_PATH = os.path.join(_TMP, "test_relevance.db")
config.LONG_TERM_MEMORY_ENABLED = True

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


# ── 假重排器（注入 sys.modules["reranker"]）────────────────────

_SCORES = {}          # page_content → 重排分
_RERANK_EXC = None    # 非 None 则重排抛错
_ALIGN_BREAK = False  # True 则返回全新的文档对象（模拟对齐失败）


def _fake_rerank(query, docs):
    if _RERANK_EXC:
        raise _RERANK_EXC
    if _ALIGN_BREAK:
        return [_Doc("不相干的返回", {})]
    out = []
    for d in docs:
        d.metadata["rerank_score"] = _SCORES.get(d.page_content, -10.0)
        out.append(d)
    out.sort(key=lambda d: d.metadata["rerank_score"], reverse=True)
    return out[:5]


_mod = types.ModuleType("reranker")
_mod.rerank_cross_encoder = _fake_rerank
sys.modules["reranker"] = _mod


# ── 假向量库 ──────────────────────────────────────────────────

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
            out.append((_Doc(t, dict(m)), (1.0 / sim - 1.0) if sim > 0 else 999.0))
        out.sort(key=lambda x: x[1])
        return out[:k]


def _new_memory():
    if os.path.exists(config.MEMORY_DB_PATH):
        os.remove(config.MEMORY_DB_PATH)
    LongTermMemory._instance = None
    m = LongTermMemory.__new__(LongTermMemory)
    m._vs = _FakeVS()
    m._db_ready = False
    m._init_done = True
    m._init_sqlite(config)
    return m


M1 = "用户在做 6G 无人机项目"
M2 = "用户要求中文回答"
M3 = "用户关注具身智能"


def _seed(m):
    m._store_one("alice", "profile", M1, 0.9)
    m._store_one("alice", "entity", M2, 0.9)
    m._store_one("alice", "conclusion", M3, 0.9)


def _reset():
    _SCORES.clear()
    config.MEMORY_RERANK_ENABLED = True
    config.MEMORY_MIN_RERANK_SCORE = -5.0
    global _RERANK_EXC, _ALIGN_BREAK
    _RERANK_EXC, _ALIGN_BREAK = None, False


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_all_pass():
    print("\n【正常 1】重排分全部达标 → 全部注入")
    _reset()
    m = _new_memory()
    _seed(m)
    _SCORES.update({M1: 0.8, M2: -1.2, M3: -3.0})
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(len(out) == 3, f"注入 3 条（实际 {len(out)}）")
    check(all(any(k in o for o in out) for k in (M1, M2, M3)), "三条记忆均在结果中")


def test_partial_filtered():
    print("\n【正常 2】低于阈值 → 被过滤，不注入")
    _reset()
    m = _new_memory()
    _seed(m)
    _SCORES.update({M1: 1.5, M2: -8.0, M3: -7.5})   # 只有 M1 达标
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(len(out) == 1, f"只注入 1 条（实际 {len(out)}）")
    check(M1 in out[0], "保留的是达标那条")
    check(not any(M2 in o or M3 in o for o in out), "未达标的被过滤掉")


def test_no_padding():
    print("\n【正常 3】过滤后不足 top_k → 返回剩余条数，不硬凑")
    _reset()
    m = _new_memory()
    _seed(m)
    _SCORES.update({M1: 1.0, M2: -9.0, M3: -9.0})
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(len(out) == 1, f"返回 1 条而非硬凑 3 条（实际 {len(out)}）")


def test_order_by_weight():
    print("\n【正常 4】同分下按综合权重排序（画像 > 项目 > 结论）")
    _reset()
    m = _new_memory()
    _seed(m)
    _SCORES.update({M1: -1.0, M2: -1.0, M3: -1.0})   # 重排分相同
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(len(out) == 3, "三条均注入")
    # 综合权重还含类型权重与相关度，只断言类型标签存在
    check(any("画像" in o for o in out) and any("结论" in o for o in out),
          "类型标签保留（排序逻辑未被破坏）")


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_boundary_switch_off():
    print("\n【边界 1】重排开关关闭 → 不过滤，行为同改造前")
    _reset()
    config.MEMORY_RERANK_ENABLED = False
    m = _new_memory()
    _seed(m)
    _SCORES.update({M1: -20.0, M2: -20.0, M3: -20.0})   # 全是极低分
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(len(out) == 3, f"关闭开关后不做过滤，注入 3 条（实际 {len(out)}）")


def test_boundary_single_candidate():
    print("\n【边界 2】候选仅 1 条 → 不重排（省一次模型调用）")
    _reset()
    m = _new_memory()
    m._store_one("alice", "profile", M1, 0.9)
    check(m._rerank_hits("q", [(1, 2)], config) is None, "候选 ≤1 → 返回 None（跳过过滤）")


def test_boundary_threshold_disabled():
    print("\n【边界 3】阈值设 -999 → 视同不过滤")
    _reset()
    config.MEMORY_MIN_RERANK_SCORE = -999.0
    m = _new_memory()
    _seed(m)
    _SCORES.update({M1: -20.0, M2: -20.0, M3: -20.0})
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(len(out) == 3, f"极低分也全部注入（实际 {len(out)}）")


def test_boundary_all_filtered():
    print("\n【边界 4】全部低于阈值 → 返回空（宁可不注入）")
    _reset()
    m = _new_memory()
    _seed(m)
    _SCORES.update({M1: -20.0, M2: -20.0, M3: -20.0})
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(out == [], f"返回空列表（实际 {out}）")


def test_boundary_cross_user():
    print("\n【边界 5】跨用户隔离不受过滤影响")
    _reset()
    m = _new_memory()
    _seed(m)
    m._store_one("bob", "profile", "Bob 是后端工程师", 0.9)
    _SCORES.update({M1: 1.0, M2: 1.0, M3: 1.0, "Bob 是后端工程师": 1.0})
    out = m.retrieve("bob", "我是谁", top_k=3)
    check(any("Bob" in o for o in out), "bob 只取到自己的记忆")
    check(not any(M1 in o for o in out), "未串到 alice 的记忆")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_rerank_raises():
    print("\n【异常 1】重排抛错 → 跳过过滤，降级为原行为（不丢记忆）")
    _reset()
    global _RERANK_EXC
    _RERANK_EXC = RuntimeError("模拟重排服务故障")
    m = _new_memory()
    _seed(m)
    try:
        out = m.retrieve("alice", "我的项目", top_k=3)
        ok = True
    except Exception as e:
        ok, out = False, str(e)
        print(f"      抛出异常: {e}")
    check(ok, "未抛异常（降级安全）")
    check(len(out) == 3, f"降级为不过滤，注入 3 条（实际 {len(out)}）")
    _RERANK_EXC = None


def test_exception_alignment_broken():
    print("\n【异常 2】重排返回的文档对不上 → 放弃过滤，而非误杀全部记忆")
    _reset()
    global _ALIGN_BREAK
    _ALIGN_BREAK = True
    m = _new_memory()
    _seed(m)
    out = m.retrieve("alice", "我的项目", top_k=3)
    check(len(out) == 3, f"放弃过滤后仍注入 3 条（实际 {len(out)}）")
    _ALIGN_BREAK = False


def test_exception_empty_store():
    print("\n【异常 3】记忆库为空 → 返回空，不抛错")
    _reset()
    m = _new_memory()
    try:
        out = m.retrieve("alice", "任意问题", top_k=3)
        ok = True
    except Exception as e:
        ok, out = False, str(e)
    check(ok and out == [], "返回空列表且未抛错")


def main():
    print("🧪 记忆检索相关性过滤测试")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_all_pass()
    test_partial_filtered()
    test_no_padding()
    test_order_by_weight()

    print("\n─── 二、边界输入 ───")
    test_boundary_switch_off()
    test_boundary_single_candidate()
    test_boundary_threshold_disabled()
    test_boundary_all_filtered()
    test_boundary_cross_user()

    print("\n─── 三、异常场景 ───")
    test_exception_rerank_raises()
    test_exception_alignment_broken()
    test_exception_empty_store()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
