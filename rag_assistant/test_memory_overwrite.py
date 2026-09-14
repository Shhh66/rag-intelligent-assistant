"""记忆改口覆盖测试（对照「显式覆盖」短板）

覆盖三类场景：
1. 正常输入：同实体重复 → 权重强化；改口（语义近似但内容不同）→ 新值优先 + 权重继承
2. 边界输入：开关关闭退回旧行为 / 改口后再次强化 / 空内容 / 跨用户隔离
3. 异常场景：向量库故障不抛出

SQLite 用真实实现，向量库用假实现（字符 bigram 相似度，确定性）。
改口场景通过下调 MEMORY_DEDUP_SIM 触发语义命中路径（生产默认 0.85 需更接近的措辞）。
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
from long_term_memory import LongTermMemory

_TMP = tempfile.mkdtemp()
config.MEMORY_DB_PATH = os.path.join(_TMP, "test_overwrite.db")
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


class _Doc:
    def __init__(self, content, metadata):
        self.page_content = content
        self.metadata = metadata


class _FakeVS:
    """字符 bigram Jaccard 相似度；dist = 1/sim - 1，使 rel == sim。"""

    def __init__(self):
        self.docs = {}
        self.fail = False
        self.fail_delete = False

    def add_texts(self, texts, metadatas, ids):
        if self.fail:
            raise RuntimeError("模拟向量库故障")
        for t, m, i in zip(texts, metadatas, ids):
            self.docs[i] = (t, dict(m))

    def delete(self, ids):
        if self.fail_delete:
            raise RuntimeError("模拟向量库删除故障")
        for i in ids or []:
            self.docs.pop(i, None)

    @staticmethod
    def _grams(s):
        s = (s or "").lower().strip()
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

    def similarity_search_with_score(self, query, k=1, filter=None):
        if self.fail:
            raise RuntimeError("模拟向量库故障")
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


def _count(m, user_id=None):
    conn = m._sqlite()
    if user_id:
        n = conn.execute("SELECT COUNT(*) FROM memory WHERE user_id=?", (user_id,)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    conn.close()
    return n


def _contents(m, user_id):
    conn = m._sqlite()
    rows = conn.execute("SELECT content FROM memory WHERE user_id=?", (user_id,)).fetchall()
    conn.close()
    return [r[0] for r in rows]


def _weight_of(m, user_id, content):
    row = m._get_by_id(m._entity_id(user_id, content))
    return float(row["weight"]) if row else None


OLD = "用户偏好使用天气查询工具"
NEW = "用户偏好使用知识库检索工具"


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_same_entity_reinforce():
    print("\n【正常 1】同一实体重复出现 → 权重强化，内容原样（回归原行为）")
    m = _new_memory()
    m._store_one("alice", "entity", OLD, 0.9)
    m._store_one("alice", "entity", OLD, 0.9)
    check(_count(m, "alice") == 1, f"仍为 1 条，未重复（实际 {_count(m, 'alice')}）")
    check(_weight_of(m, "alice", OLD) == 2.0, f"weight=2.0（实际 {_weight_of(m, 'alice', OLD)}）")
    check(_contents(m, "alice") == [OLD], "内容未被改写")


def test_overwrite_on_change():
    print("\n【正常 2】改口：语义近似但内容不同 → 新值优先 + 权重继承")
    config.MEMORY_DEDUP_SIM = 0.3      # 下调以触发语义命中路径
    try:
        m = _new_memory()
        m._store_one("alice", "entity", OLD, 0.9)     # 新建 weight=1.0
        m._store_one("alice", "entity", OLD, 0.9)     # 强化 weight=2.0
        m._store_one("alice", "entity", NEW, 0.9)     # 改口
        check(_count(m, "alice") == 1, f"旧记录已作废，仅剩 1 条（实际 {_count(m, 'alice')}）")
        check(_contents(m, "alice") == [NEW], f"内容为新事实（实际 {_contents(m, 'alice')}）")
        check(OLD not in _contents(m, "alice"), "旧事实已不存在")
        check(_weight_of(m, "alice", NEW) == 3.0,
              f"累积权重被继承 2.0+1.0=3.0（实际 {_weight_of(m, 'alice', NEW)}）")
    finally:
        config.MEMORY_DEDUP_SIM = 0.85


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_boundary_switch_off():
    print("\n【边界 1】开关关闭 → 退回旧行为（只加权重，内容保留旧值）")
    config.MEMORY_DEDUP_SIM = 0.3
    config.MEMORY_OVERWRITE_ON_CONFLICT = False
    try:
        m = _new_memory()
        m._store_one("alice", "entity", OLD, 0.9)
        m._store_one("alice", "entity", NEW, 0.9)
        check(_count(m, "alice") == 1, "仍为 1 条")
        check(_contents(m, "alice") == [OLD], f"内容仍是旧值（实际 {_contents(m, 'alice')}）")
        check(_weight_of(m, "alice", OLD) == 2.0, f"weight 累加为 2.0（实际 {_weight_of(m, 'alice', OLD)}）")
    finally:
        config.MEMORY_DEDUP_SIM = 0.85
        config.MEMORY_OVERWRITE_ON_CONFLICT = True


def test_boundary_reinforce_after_overwrite():
    print("\n【边界 2】改口后新事实再次出现 → 精确命中，继续强化（不再触发覆盖）")
    config.MEMORY_DEDUP_SIM = 0.3
    try:
        m = _new_memory()
        m._store_one("alice", "entity", OLD, 0.9)
        m._store_one("alice", "entity", NEW, 0.9)     # 改口 → weight 2.0
        m._store_one("alice", "entity", NEW, 0.9)     # 再出现 → 精确命中
        check(_count(m, "alice") == 1, "仍为 1 条（未新增）")
        check(_weight_of(m, "alice", NEW) == 3.0, f"weight=3.0（实际 {_weight_of(m, 'alice', NEW)}）")
        check(_contents(m, "alice") == [NEW], "内容仍为新事实")
    finally:
        config.MEMORY_DEDUP_SIM = 0.85


def test_boundary_empty_content():
    print("\n【边界 3】空内容 / 纯空白 → 不入库")
    m = _new_memory()
    for c in ("", "   ", None):
        m._store_one("alice", "entity", c, 0.9)
    check(_count(m) == 0, f"零入库（实际 {_count(m)}）")


def test_boundary_cross_user():
    print("\n【边界 4】跨用户隔离：同内容不同用户 → 各自独立")
    m = _new_memory()
    m._store_one("alice", "entity", OLD, 0.9)
    m._store_one("bob", "entity", OLD, 0.9)
    check(_count(m) == 2, f"两个用户各 1 条（实际 {_count(m)}）")
    check(_weight_of(m, "alice", OLD) == 1.0 and _weight_of(m, "bob", OLD) == 1.0,
          "两边 weight 均为 1.0，未串户强化")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_vs_failure():
    print("\n【异常 1】向量库整体故障 → 不抛出，语义去重不可用则退化为新增")
    config.MEMORY_DEDUP_SIM = 0.3
    try:
        m = _new_memory()
        m._store_one("alice", "entity", OLD, 0.9)
        m._vs.fail = True          # 检索与写入全部失败
        try:
            m._store_one("alice", "entity", NEW, 0.9)
            ok = True
        except Exception as e:
            ok = False
            print(f"      抛出异常: {e}")
        check(ok, "未抛异常（降级安全）")
        check(NEW in _contents(m, "alice"), "新事实仍已落 SQLite")
        check(_count(m, "alice") == 2,
              f"向量不可用 → 无法识别同实体，退化为新增 2 条（实际 {_count(m, 'alice')}）")
    finally:
        config.MEMORY_DEDUP_SIM = 0.85


def test_exception_vs_delete_failure():
    print("\n【异常 2】向量库删除失败 → 不抛出，SQLite 侧覆盖照常完成")
    config.MEMORY_DEDUP_SIM = 0.3
    try:
        m = _new_memory()
        m._store_one("alice", "entity", OLD, 0.9)
        m._vs.fail_delete = True
        try:
            m._store_one("alice", "entity", NEW, 0.9)
            ok = True
        except Exception as e:
            ok = False
            print(f"      抛出异常: {e}")
        check(ok, "未抛异常（_delete 内部已降级）")
        check(_count(m, "alice") == 1, f"SQLite 侧旧记录已删除（实际 {_count(m, 'alice')}）")
        check(_contents(m, "alice") == [NEW], "内容为新事实")
        check(_weight_of(m, "alice", NEW) == 2.0, f"权重继承为 2.0（实际 {_weight_of(m, 'alice', NEW)}）")
    finally:
        config.MEMORY_DEDUP_SIM = 0.85


def main():
    print("🧪 记忆改口覆盖测试")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_same_entity_reinforce()
    test_overwrite_on_change()

    print("\n─── 二、边界输入 ───")
    test_boundary_switch_off()
    test_boundary_reinforce_after_overwrite()
    test_boundary_empty_content()
    test_boundary_cross_user()

    print("\n─── 三、异常场景 ───")
    test_exception_vs_failure()
    test_exception_vs_delete_failure()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
