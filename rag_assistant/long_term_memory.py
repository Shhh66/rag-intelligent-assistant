"""长期记忆 —— 跨会话实体记忆与语义检索。

存储双写：
- Chroma collection（独立于主库 langchain）：语义召回，metadata 带 user_id/mem_type/confidence/created_at
- SQLite（memory.db）：结构化镜像，支持按 user_id 精确查/更新/清理

能力：
- extract_and_store：每轮对话后抽取记忆（V0.5=摘要 / V1.0=LLM实体抽取），去重更新
- retrieve：按 user_id 过滤 + query 语义检索 + 权重排序（语义×时间衰减×类型权重×置信度）
- 短→长沉淀：高频工具偏好自动沉淀（由 unified_agent 调 record_tool_preference）

全程降级安全：任何失败静默跳过，绝不阻断主对话。按 user_id 隔离防串户。
"""

import os
import sys
import json
import sqlite3
import logging
import hashlib
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))


def _log(msg):
    print(f"   🧠 长期记忆: {msg}", file=sys.stderr, flush=True)


def _cfg():
    import config
    return config


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _abs(path):
    return path if os.path.isabs(path) else os.path.join(_THIS_DIR, os.path.basename(path))


class LongTermMemory:
    """长期记忆单例。懒加载 Chroma + SQLite；不可用时降级 no-op。"""

    _instance = None

    def __init__(self):
        self._vs = None            # langchain Chroma
        self._db_ready = False
        self._init_done = False

    @classmethod
    def get(cls):
        if cls._instance is None:
            cls._instance = LongTermMemory()
        return cls._instance

    # ── 懒加载 ──────────────────────────────────────────────
    def _ensure(self):
        if self._init_done:
            return self._vs is not None and self._db_ready
        self._init_done = True
        cfg = _cfg()
        if not getattr(cfg, "LONG_TERM_MEMORY_ENABLED", False):
            return False
        try:
            self._init_sqlite(cfg)
            self._init_chroma(cfg)
            _log("初始化完成")
            return True
        except Exception as e:
            _log(f"初始化失败(降级 no-op): {e}")
            return False

    def _init_sqlite(self, cfg):
        path = _abs(getattr(cfg, "MEMORY_DB_PATH", "./memory.db"))
        conn = sqlite3.connect(path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS memory (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                mem_type TEXT NOT NULL,
                content TEXT NOT NULL,
                confidence REAL DEFAULT 1.0,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user ON memory(user_id)")
        conn.commit()
        conn.close()
        self._db_path = path
        self._db_ready = True

    def _init_chroma(self, cfg):
        from vector_store import get_embeddings, VECTOR_DB_PATH
        from langchain_chroma import Chroma
        self._vs = Chroma(
            persist_directory=str(VECTOR_DB_PATH),
            embedding_function=get_embeddings(),
            collection_name=getattr(cfg, "MEMORY_COLLECTION", "memory"),
        )

    # ── 内部工具 ────────────────────────────────────────────
    def _mem_id(self, user_id, content):
        return hashlib.md5(f"{user_id}|{content}|{_now_iso()}".encode()).hexdigest()[:16]

    def _sqlite(self):
        return sqlite3.connect(self._db_path)

    def _upsert(self, mem_id, user_id, mem_type, content, confidence, created_at):
        now = _now_iso()
        # SQLite
        conn = self._sqlite()
        conn.execute(
            "INSERT OR REPLACE INTO memory VALUES (?,?,?,?,?,?,?)",
            (mem_id, user_id, mem_type, content, confidence, created_at or now, now),
        )
        conn.commit()
        conn.close()
        # Chroma
        self._vs.add_texts(
            texts=[content],
            metadatas=[{"user_id": user_id, "mem_type": mem_type,
                        "confidence": confidence, "created_at": created_at or now}],
            ids=[mem_id],
        )

    def _delete(self, mem_id):
        try:
            conn = self._sqlite()
            conn.execute("DELETE FROM memory WHERE id=?", (mem_id,))
            conn.commit()
            conn.close()
        except Exception:
            pass
        try:
            self._vs.delete(ids=[mem_id])
        except Exception:
            pass

    # ── 存储（去重更新）──────────────────────────────────────
    def _store_one(self, user_id, mem_type, content, confidence=1.0):
        """存一条记忆，先做同用户去重：相似度高则更新覆盖，否则新增。"""
        cfg = _cfg()
        dedup_sim = getattr(cfg, "MEMORY_DEDUP_SIM", 0.85)
        content = (content or "").strip()
        if not content:
            return
        try:
            # 去重：同 user 语义检索最近似的一条（距离转相关度）
            hits = self._vs.similarity_search_with_score(
                content, k=1, filter={"user_id": user_id}
            )
            if hits:
                dist = float(hits[0][1])
                rel = 1.0 / (1.0 + max(dist, 0.0))
                if rel >= dedup_sim:
                    self._delete_by_content(user_id, hits[0][0].page_content)
                    _log(f"去重更新: {content[:30]}")
        except Exception as e:
            logger.debug(f"去重检查跳过: {e}")
        mem_id = self._mem_id(user_id, content)
        self._upsert(mem_id, user_id, mem_type, content, confidence, _now_iso())

    def _delete_by_content(self, user_id, content):
        """按内容删除旧记忆（去重更新用）。"""
        try:
            conn = self._sqlite()
            rows = conn.execute(
                "SELECT id FROM memory WHERE user_id=? AND content=?",
                (user_id, content),
            ).fetchall()
            conn.close()
            for (mid,) in rows:
                self._delete(mid)
        except Exception:
            pass

    # ── 对外：抽取并存储 ────────────────────────────────────
    def extract_and_store(self, user_id, user_input, answer):
        """每轮对话后调用：抽取记忆并存储。降级安全。"""
        if not self._ensure():
            return
        cfg = _cfg()
        user_id = user_id or "default"
        try:
            if getattr(cfg, "MEMORY_EXTRACT_ENABLED", True):
                items = self._llm_extract(user_input, answer)
                if items:
                    for it in items:
                        self._store_one(user_id, it.get("mem_type", "entity"),
                                        it.get("content", ""), it.get("confidence", 0.8))
                    _log(f"抽取 {len(items)} 条记忆(user={user_id})")
                    return
            # V0.5 兜底 / 抽取为空：存一句摘要
            summary = self._llm_summarize(user_input, answer)
            if summary:
                self._store_one(user_id, "conclusion", summary, 0.6)
        except Exception as e:
            _log(f"抽取存储失败(忽略): {e}")

    def _llm_client(self):
        from openai import OpenAI
        from config import GROQ_API_KEY, GROQ_BASE_URL
        return OpenAI(api_key=GROQ_API_KEY, base_url=GROQ_BASE_URL, timeout=30.0)

    def _llm_extract(self, user_input, answer):
        """LLM 抽取实体记忆，返回 [{mem_type, content, confidence}]。"""
        from config import LLM_MODEL, MEMORY_EXTRACT_MAX_TOKENS
        try:
            from token_tracker import get_tracker
        except Exception:
            get_tracker = None
        prompt = (
            "从下面一轮对话中抽取值得【长期记住】的用户信息，只抽取稳定、跨会话有用的事实，"
            "忽略临时性内容。按 JSON 数组输出，每项 {\"mem_type\":\"profile|entity|conclusion\","
            "\"content\":\"一句话事实\",\"confidence\":0~1}。"
            "profile=用户画像(专业/目标/固定偏好)，entity=项目/关注技术，conclusion=已确认结论。"
            "没有值得记的就输出 []。只输出 JSON，不要解释。\n\n"
            f"用户：{user_input}\n助手：{answer[:500]}"
        )
        resp = self._llm_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=MEMORY_EXTRACT_MAX_TOKENS,
        )
        if get_tracker:
            try:
                get_tracker().record(LLM_MODEL, resp.usage, call_site="memory.extract")
            except Exception:
                pass
        text = (resp.choices[0].message.content or "").strip()
        return self._parse_json_array(text)

    def _llm_summarize(self, user_input, answer):
        """V0.5：生成一句对话摘要。"""
        from config import LLM_MODEL, MEMORY_EXTRACT_MAX_TOKENS
        resp = self._llm_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content":
                       f"用一句话概括这轮对话的核心信息(便于日后检索)：\n用户：{user_input}\n助手：{answer[:400]}\n只输出这一句话。"}],
            temperature=0,
            max_tokens=MEMORY_EXTRACT_MAX_TOKENS,
        )
        return (resp.choices[0].message.content or "").strip()

    @staticmethod
    def _parse_json_array(text):
        import re
        if not text:
            return []
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
            return [d for d in data if isinstance(d, dict) and d.get("content")]
        except Exception:
            return []

    # ── 对外：检索注入 ──────────────────────────────────────
    def retrieve(self, user_id, query, top_k=None):
        """按 user_id + query 语义检索，权重排序后返回格式化字符串列表。降级返回 []。"""
        if not self._ensure():
            return []
        cfg = _cfg()
        user_id = user_id or "default"
        top_k = top_k or getattr(cfg, "MEMORY_RETRIEVE_TOP_K", 3)
        type_w = getattr(cfg, "MEMORY_TYPE_WEIGHTS",
                         {"profile": 1.0, "entity": 0.7, "conclusion": 0.4})
        decay_days = getattr(cfg, "MEMORY_DECAY_DAYS", 90)
        try:
            # 用距离分（越小越近），转成 0~1 相关度 rel=1/(1+dist)，避免不同后端相关性分为负
            hits = self._vs.similarity_search_with_score(
                query, k=max(top_k * 3, 6), filter={"user_id": user_id}
            )
        except Exception as e:
            _log(f"检索失败(忽略): {e}")
            return []

        label = {"profile": "画像", "entity": "项目", "conclusion": "结论"}
        scored = []
        for doc, dist in hits:
            m = doc.metadata or {}
            mtype = m.get("mem_type", "conclusion")
            conf = float(m.get("confidence", 1.0) or 1.0)
            decay = self._decay(m.get("created_at"), decay_days)
            rel = 1.0 / (1.0 + max(float(dist), 0.0))   # 距离 → 0~1 相关度
            weight = rel * type_w.get(mtype, 0.4) * conf * decay
            scored.append((weight, mtype, doc.page_content))
        scored.sort(key=lambda x: x[0], reverse=True)

        out = []
        for weight, mtype, content in scored[:top_k]:
            if weight <= 0:
                continue
            out.append(f"[长期记忆·{label.get(mtype, '结论')}] {content}")
        if out:
            _log(f"注入 {len(out)} 条(user={user_id})")
        return out

    @staticmethod
    def _decay(created_at, decay_days):
        """时间衰减权重：越旧越低，超过 decay_days 显著降权。"""
        if not created_at:
            return 1.0
        try:
            t = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
            age_days = (datetime.now(timezone.utc) - t).total_seconds() / 86400
            if age_days <= decay_days:
                return 1.0
            # 超过阈值后线性衰减到最低 0.3
            return max(0.3, 1.0 - (age_days - decay_days) / (decay_days * 2))
        except Exception:
            return 1.0

    # ── 短→长沉淀：高频工具偏好 ────────────────────────────
    def record_tool_preference(self, user_id, tool_name):
        """连续高频使用某工具时沉淀为用户偏好（由 unified_agent 判定触发）。"""
        if not self._ensure():
            return
        self._store_one(user_id or "default", "profile",
                        f"用户偏好优先使用工具「{tool_name}」", 0.7)

    # ── 管理 ────────────────────────────────────────────────
    def clear(self, user_id):
        """清空某用户的长期记忆（独立于会话 clear_memory）。"""
        if not self._ensure():
            return 0
        try:
            conn = self._sqlite()
            rows = conn.execute("SELECT id FROM memory WHERE user_id=?", (user_id,)).fetchall()
            conn.close()
            for (mid,) in rows:
                self._delete(mid)
            return len(rows)
        except Exception as e:
            _log(f"清理失败: {e}")
            return 0


def get_memory():
    return LongTermMemory.get()


# ── 自测 ──
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("=== 长期记忆自测 ===")
    m = get_memory()
    # 直接存(绕过 LLM 抽取)
    m._ensure()
    m._store_one("alice", "profile", "Alice 是通信工程大三学生，求职 AI 方向", 0.9)
    m._store_one("alice", "entity", "Alice 在做 6G 低空无人机 ISAC 项目", 0.85)
    m._store_one("bob", "profile", "Bob 是后端工程师", 0.9)
    print("\n[alice 检索 '我的项目']")
    for s in m.retrieve("alice", "我的项目是什么", top_k=3):
        print(" ", s)
    print("\n[bob 检索 '项目'(应看不到alice的)]")
    for s in m.retrieve("bob", "我的项目", top_k=3):
        print(" ", s)
    print("\n[去重更新测试: 再存一条相似的 alice 项目记忆]")
    m._store_one("alice", "entity", "Alice 正在做 6G 低空无人机 ISAC 的调研项目", 0.9)
    print("  alice 当前记忆条数:", len(m.retrieve("alice", "项目 学生 方向", top_k=10)))
    print("\n🎉 自测完成（清理测试数据）")
    m.clear("alice"); m.clear("bob")
