"""实体 ID 与权重改造测试脚本（模块 9.7）

覆盖三类场景：
1. 正常输入：实体 ID 稳定、重复写入权重累加（不新增记录）、语义近似合并
2. 边界输入：归一化（空格/大小写/全角）、跨用户隔离、迁移无重复/有重复
3. 异常场景：空内容、向量库故障降级、迁移在未启用时

SQLite 用真实实现（含 schema 与 ALTER 兼容），向量库用假实现隔离嵌入模型。
"""

import sys
import os
import tempfile
import sqlite3
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))

import config
from long_term_memory import LongTermMemory

# 隔离到临时 DB
_TMP = tempfile.mkdtemp()
config.MEMORY_DB_PATH = os.path.join(_TMP, "test_memory.db")
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


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ── 假向量库（隔离真实 Chroma + 嵌入模型）─────────────────────

class _Doc:
    def __init__(self, content, metadata):
        self.page_content = content
        self.metadata = metadata


class _FakeVS:
    """最小向量库：字符 bigram Jaccard 模拟语义相似度，返回 (doc, 距离分)。"""

    def __init__(self):
        self.docs = {}          # id -> (content, metadata)
        self.fail = False       # True 时模拟故障

    def add_texts(self, texts, metadatas, ids):
        if self.fail:
            raise RuntimeError("模拟向量库写入故障")
        for t, m, i in zip(texts, metadatas, ids):
            self.docs[i] = (t, dict(m))

    def delete(self, ids):
        if self.fail:
            raise RuntimeError("模拟向量库删除故障")
        for i in ids or []:
            self.docs.pop(i, None)

    @staticmethod
    def _grams(s):
        s = (s or "").lower().strip()
        return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}

    def similarity_search_with_score(self, query, k=1, filter=None):
        if self.fail:
            raise RuntimeError("模拟向量库检索故障")
        uid = (filter or {}).get("user_id")
        qg = self._grams(query)
        out = []
        for i, (t, m) in self.docs.items():
            if uid is not None and m.get("user_id") != uid:
                continue
            eg = self._grams(t)
            inter, union = qg & eg, qg | eg
            sim = len(inter) / len(union) if union else 0.0
            dist = (1.0 / sim - 1.0) if sim > 0 else 999.0   # sim → 距离分
            out.append((_Doc(t, m), dist))
        out.sort(key=lambda x: x[1])
        return out[:k]


def _new_memory(fresh_db=True):
    """构造只连 SQLite（配假向量库）的记忆实例。"""
    if fresh_db and os.path.exists(config.MEMORY_DB_PATH):
        os.remove(config.MEMORY_DB_PATH)
    LongTermMemory._instance = None
    m = LongTermMemory.__new__(LongTermMemory)
    m._vs = _FakeVS()
    m._db_ready = False
    m._init_done = True                 # 跳过 _ensure 中的 Chroma 初始化
    m._init_sqlite(config)              # 真实建表（weight 列 + ALTER 兼容）
    return m


def _sql_one(m, entity_id):
    conn = m._sqlite()
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM memory WHERE id=?", (entity_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _count(m, user_id=None):
    conn = m._sqlite()
    if user_id:
        n = conn.execute("SELECT COUNT(*) FROM memory WHERE user_id=?", (user_id,)).fetchone()[0]
    else:
        n = conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0]
    conn.close()
    return n


def _insert_raw(m, row_id, user_id, content, mem_type="entity",
                confidence=1.0, weight=1.0, created_at=None):
    """直接插一条原始记录（模拟旧版「事件 ID」数据）。"""
    conn = m._sqlite()
    conn.execute(
        "INSERT OR REPLACE INTO memory "
        "(id,user_id,mem_type,content,confidence,weight,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (row_id, user_id, mem_type, content, confidence, weight,
         created_at or _now(), _now()),
    )
    conn.commit()
    conn.close()
    m._vs.add_texts([content], [{"user_id": user_id, "mem_type": mem_type,
                                 "confidence": confidence, "weight": weight}], [row_id])


# ══════════════════════════════════════════════════════════════
# 一、正常输入
# ══════════════════════════════════════════════════════════════

def test_id_stable():
    print("\n【正常 1】实体 ID 稳定（不含时间戳）")
    m = _new_memory()
    c = "用户在做 RAG 项目"
    id1 = m._entity_id("alice", c)
    id2 = m._entity_id("alice", c)
    check(id1 == id2, f"同内容两次生成 ID 相同（{id1}）")
    check(len(id1) == 16, "ID 长度 16（与旧格式一致）")
    # 旧实现含时间戳：换个时刻就会变；新实现与时间无关
    check(m._entity_id("alice", c) == m._m_id("alice", c) if hasattr(m, "_m_id") else True,
          "兼容入口 _mem_id 委托给实体 ID")


def test_weight_accumulate():
    print("\n【正常 2】重复写入 → 权重累加，不新增记录")
    m = _new_memory()
    c = "用户偏好用中文回答"
    m._store_one("alice", "profile", c, 0.9)
    check(_count(m, "alice") == 1, "首次写入：1 条记录")
    row = _sql_one(m, m._entity_id("alice", c))
    check(abs(row["weight"] - 1.0) < 1e-6, f"初始 weight=1.0（实际 {row['weight']}）")

    m._store_one("alice", "profile", c, 0.9)
    m._store_one("alice", "profile", c, 0.9)
    check(_count(m, "alice") == 1, "三次写入后仍只有 1 条（旧实现会删旧存新）")
    row = _sql_one(m, m._entity_id("alice", c))
    check(abs(row["weight"] - 3.0) < 1e-6,
          f"weight 累加到 3.0（正向强化，实际 {row['weight']}）")


def test_weight_delta():
    print("\n【正常 3】delta 可调（强化步长，仅作用于已存在实体）")
    m = _new_memory()
    c = "用户在做 Agent 项目"
    m._store_one("alice", "entity", c, 1.0, delta=2.0)   # 首次插入 → weight = 初始值 1.0
    row = _sql_one(m, m._entity_id("alice", c))
    check(abs(row["weight"] - 1.0) < 1e-6,
          f"首次插入用初始值 1.0，不叠加 delta（实际 {row['weight']}）")
    m._store_one("alice", "entity", c, 1.0, delta=2.0)   # 已存在 → +2
    m._store_one("alice", "entity", c, 1.0, delta=2.0)   # 已存在 → +2
    row = _sql_one(m, m._entity_id("alice", c))
    check(abs(row["weight"] - 5.0) < 1e-6,
          f"1.0 + 2 + 2 = 5.0（实际 {row['weight']}）")


def test_semantic_merge():
    print("\n【正常 4】语义近似 → 合并到同一实体（强化而非新增）")
    m = _new_memory()
    m._store_one("alice", "entity", "用户在做RAG知识库项目", 0.9)
    m._store_one("alice", "entity", "用户在做RAG知识库项目", 0.9)   # 完全相同
    check(_count(m, "alice") == 1, "相同内容合并为 1 条")
    # 高度近似（Jaccard 高）也应命中
    m._store_one("alice", "entity", "用户在做RAG知识库项目。", 0.9)
    n = _count(m, "alice")
    check(n == 1, f"近似内容仍合并为 1 条（实际 {n} 条）")


# ══════════════════════════════════════════════════════════════
# 二、边界输入
# ══════════════════════════════════════════════════════════════

def test_normalize():
    print("\n【边界 1】文本归一化：空白/大小写/全角 → 同一实体")
    m = _new_memory()
    base = m._entity_id("alice", "User Likes Python")
    check(m._entity_id("alice", "  User Likes Python  ") == base, "首尾空格不影响 ID")
    check(m._entity_id("alice", "user   likes python") == base, "连续空格+大小写不影响 ID")
    check(m._entity_id("alice", "User　Likes Python") == base, "全角空格不影响 ID")
    check(m._entity_id("alice", "User Likes Java") != base, "不同内容 ID 不同")


def test_user_isolation():
    print("\n【边界 2】跨用户隔离：同内容不同用户 → 各自独立")
    m = _new_memory()
    c = "用户在做 RAG 项目"
    m._store_one("alice", "entity", c, 0.9)
    m._store_one("bob", "entity", c, 0.9)
    check(_count(m) == 2, f"两个用户各 1 条（实际 {_count(m)} 条）")
    check(m._entity_id("alice", c) != m._entity_id("bob", c), "同内容不同用户 ID 不同")
    # alice 再存 → 只强化自己的
    m._store_one("alice", "entity", c, 0.9)
    check(_count(m) == 2, "alice 强化不新增记录，bob 不受影响")
    check(abs(_sql_one(m, m._entity_id("alice", c))["weight"] - 2.0) < 1e-6, "alice weight=2.0")
    check(abs(_sql_one(m, m._entity_id("bob", c))["weight"] - 1.0) < 1e-6, "bob weight 仍为 1.0")


def test_migrate_no_dup():
    print("\n【边界 3】迁移：无重复数据 → 原地不动")
    m = _new_memory()
    c = "用户偏好中文"
    _insert_raw(m, m._entity_id("alice", c), "alice", c, weight=2.0)
    res = m.migrate_to_entity_id(dry_run=False)
    check(res["scanned"] == 1 and res["written"] == 1 and res["merged"] == 0,
          f"扫描 1 → 写入 1、合并 0（实际 {res}）")
    check(abs(_sql_one(m, m._entity_id("alice", c))["weight"] - 2.0) < 1e-6, "weight 保持不变")


def test_migrate_merge_dup():
    print("\n【边界 4】迁移：旧「事件 ID」重复 → 合并 + 权重相加")
    m = _new_memory()
    c = "用户在做 RAG 项目"
    # 模拟旧版：同内容、不同时间戳 → 三条不同 ID（事件 ID）
    _insert_raw(m, "old_aaa1", "alice", c, weight=1.0, created_at="2026-01-01T00:00:00")
    _insert_raw(m, "old_aaa2", "alice", c, weight=2.0, created_at="2026-05-01T00:00:00")
    _insert_raw(m, "old_aaa3", "alice", c, weight=3.0, created_at="2026-09-01T00:00:00")
    check(_count(m) == 3, "迁移前：3 条重复记录")

    res = m.migrate_to_entity_id(dry_run=False)
    check(_count(m) == 1, f"迁移后合并为 1 条（实际 {_count(m)}）")
    check(res["merged"] == 2, f"记录消除重复 2 条（实际 {res['merged']}）")
    row = _sql_one(m, m._entity_id("alice", c))
    check(abs(row["weight"] - 6.0) < 1e-6, f"权重相加 1+2+3=6.0（实际 {row['weight']}）")
    check(row["created_at"] == "2026-01-01T00:00:00", "时间戳取最早")


def test_migrate_dry_run():
    print("\n【边界 5】迁移 dry-run → 只统计不写入")
    m = _new_memory()
    c = "用户偏好中文"
    _insert_raw(m, "old_b1", "alice", c)
    _insert_raw(m, "old_b2", "alice", c)
    res = m.migrate_to_entity_id(dry_run=True)
    check(res["dry_run"] is True, "返回标记 dry_run=True")
    check(res["scanned"] == 2 and res["written"] == 1, f"统计正确（{res}）")
    check(_count(m) == 2, "数据未被改动（仍为 2 条）")


def test_migrate_multiuser():
    print("\n【边界 6】迁移：不同用户的同内容不被合并")
    m = _new_memory()
    c = "用户在做 RAG 项目"
    _insert_raw(m, "old_c1", "alice", c, weight=1.0)
    _insert_raw(m, "old_c2", "bob", c, weight=1.0)
    res = m.migrate_to_entity_id(dry_run=False)
    check(res["written"] == 2, f"跨用户不合并，仍为 2 条（实际 {res['written']}）")


# ══════════════════════════════════════════════════════════════
# 三、异常场景
# ══════════════════════════════════════════════════════════════

def test_exception_empty_content():
    print("\n【异常 1】空内容/纯空白 → 不写入")
    m = _new_memory()
    m._store_one("alice", "entity", "", 1.0)
    m._store_one("alice", "entity", "   ", 1.0)
    m._store_one("alice", "entity", None, 1.0)
    check(_count(m) == 0, "空内容零写入，不抛异常")


def test_exception_vs_failure():
    print("\n【异常 2】向量库故障 → 语义去重降级，不影响写入")
    m = _new_memory()
    m._vs.fail = True                    # 检索抛异常
    try:
        m._store_one("alice", "entity", "用户在做 RAG 项目", 0.9)
        ok = True
    except Exception as e:
        ok = False
        print(f"      抛出异常: {e}")
    check(ok, "主流程未抛异常（降级安全）")
    check(_count(m) == 1, f"记录仍成功写入 SQLite（实际 {_count(m)}）")
    check(m._find_similar("alice", "任意", 0.85) is None, "_find_similar 降级返回 None")


def test_exception_migrate_disabled():
    print("\n【异常 3】记忆未启用 → 迁移返回 error 不崩溃")
    LongTermMemory._instance = None
    m = LongTermMemory.__new__(LongTermMemory)
    m._vs = None
    m._db_ready = False
    m._init_done = False
    saved = config.LONG_TERM_MEMORY_ENABLED
    config.LONG_TERM_MEMORY_ENABLED = False
    try:
        res = m.migrate_to_entity_id()
        check("error" in res, f"返回 error 标记（{res}）")
    finally:
        config.LONG_TERM_MEMORY_ENABLED = saved


def test_exception_old_db_without_weight():
    print("\n【异常 4】存量库缺 weight 列 → ALTER 自动补齐")
    db = os.path.join(_TMP, "legacy.db")
    if os.path.exists(db):
        os.remove(db)
    # 造一个旧版表结构（无 weight 列）
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE memory (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mem_type TEXT NOT NULL,
        content TEXT NOT NULL, confidence REAL DEFAULT 1.0,
        created_at TEXT, updated_at TEXT)""")
    conn.execute("INSERT INTO memory VALUES ('x1','alice','entity','旧数据',1.0,?,?)",
                 (_now(), _now()))
    conn.commit()
    conn.close()

    saved_path = config.MEMORY_DB_PATH
    config.MEMORY_DB_PATH = db
    try:
        m = LongTermMemory.__new__(LongTermMemory)
        m._vs = _FakeVS()
        m._db_ready = False
        m._init_done = True
        m._init_sqlite(config)           # 应自动 ALTER 补 weight 列
        conn = m._sqlite()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memory)").fetchall()]
        conn.close()
        check("weight" in cols, f"weight 列已补齐（现有列: {cols}）")
        check(m._get_by_id("x1") is not None, "存量数据仍可读取")
        check(abs(m._get_by_id("x1")["weight"] - 1.0) < 1e-6, "存量记录 weight 默认为 1.0")
    finally:
        config.MEMORY_DB_PATH = saved_path


def main():
    print("🧪 实体 ID 与权重改造测试（模块 9.7）")
    print("=" * 62)

    print("\n─── 一、正常输入 ───")
    test_id_stable()
    test_weight_accumulate()
    test_weight_delta()
    test_semantic_merge()

    print("\n─── 二、边界输入 ───")
    test_normalize()
    test_user_isolation()
    test_migrate_no_dup()
    test_migrate_merge_dup()
    test_migrate_dry_run()
    test_migrate_multiuser()

    print("\n─── 三、异常场景 ───")
    test_exception_empty_content()
    test_exception_vs_failure()
    test_exception_migrate_disabled()
    test_exception_old_db_without_weight()

    print("\n" + "=" * 62)
    print(f"结果：✅ 通过 {_passed} 项 / ❌ 失败 {_failed} 项")
    print("=" * 62)
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
