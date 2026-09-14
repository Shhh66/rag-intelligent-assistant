"""长期记忆 —— 跨会话实体记忆与语义检索。

存储双写：
- Chroma collection（独立于主库 langchain）：语义召回，metadata 带 user_id/mem_type/confidence/created_at
- SQLite（memory.db）：结构化镜像，支持按 user_id 精确查/更新/清理

能力：
- extract_and_store：每轮对话后抽取记忆（V0.5=摘要 / V1.0=LLM实体抽取），去重更新
- retrieve：按 user_id 过滤 + query 语义检索 + 权重排序（语义×时间衰减×类型权重×置信度）

**存什么、不存什么**：只存「从单轮问题里读不出来的用户信息」（画像 / 固定约束 / 项目背景 /
已确认结论）。**不存工具使用习惯**——工具由当前问题决定，历史习惯对当前决策无用，
且会占用有限的注入槽位。详见 技术文档/长期记忆.md 第十节。

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
                weight REAL DEFAULT 1.0,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        # 兼容存量库：缺少 weight 列则补上（ALTER 在列已存在时抛错，忽略即可）
        try:
            conn.execute("ALTER TABLE memory ADD COLUMN weight REAL DEFAULT 1.0")
        except Exception:
            pass
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
    @staticmethod
    def _normalize(content: str) -> str:
        """实体文本归一化：去首尾空白、全角空格转半角、连续空白折叠、统一小写。"""
        import re
        s = (content or "").strip().replace("　", " ")
        s = re.sub(r"\s+", " ", s)
        return s.strip().lower()

    @classmethod
    def _entity_id(cls, user_id, content) -> str:
        """实体 ID（稳定）：同用户 + 同归一化内容 → 永远同一个 ID。

        与旧的「事件 ID」区别：**不含时间戳**——同一实体重复出现时能识别为同一条，
        从而支持权重的正向累加（详见 技术文档/长期记忆.md 9.7）。
        """
        norm = cls._normalize(content)
        return hashlib.md5(f"{user_id}|{norm}".encode()).hexdigest()[:16]

    def _mem_id(self, user_id, content):
        """[保留兼容] 旧的「事件 ID」入口，现委托给稳定的实体 ID。"""
        return self._entity_id(user_id, content)

    def _sqlite(self):
        return sqlite3.connect(self._db_path)

    def _upsert(self, mem_id, user_id, mem_type, content, confidence, created_at, weight=1.0):
        now = _now_iso()
        created = created_at or now
        # SQLite：显式列名，兼容存量库 ALTER 后的物理列序
        conn = self._sqlite()
        conn.execute(
            "INSERT OR REPLACE INTO memory "
            "(id, user_id, mem_type, content, confidence, weight, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (mem_id, user_id, mem_type, content, confidence, weight, created, now),
        )
        conn.commit()
        conn.close()
        # Chroma：先删后加，保证同 ID 幂等更新（跨 langchain-chroma 版本安全）
        # 向量写入失败静默降级（SQLite 已落盘，下次强化同实体时会重试 Chroma），
        # 呼应模块文档「全程降级安全，绝不阻断主对话」。
        try:
            self._vs.delete(ids=[mem_id])
        except Exception:
            pass
        try:
            self._vs.add_texts(
                texts=[content],
                metadatas=[{"user_id": user_id, "mem_type": mem_type,
                            "confidence": confidence, "weight": weight,
                            "created_at": created}],
                ids=[mem_id],
            )
        except Exception as e:
            logger.warning(f"向量库写入失败(降级，SQLite 已落盘): {e}")

    def _get_by_id(self, entity_id):
        """按实体 ID 精确查一条记忆，返回 dict 或 None。"""
        try:
            conn = self._sqlite()
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT id, user_id, mem_type, content, confidence, weight, created_at "
                "FROM memory WHERE id=?", (entity_id,),
            ).fetchone()
            conn.close()
            if row:
                return dict(row)
        except Exception:
            pass
        return None

    def _find_similar(self, user_id, content, dedup_sim):
        """语义查同用户最近似的一条记忆（复用向量检索），命中返回 dict 或 None。"""
        try:
            hits = self._vs.similarity_search_with_score(
                content, k=1, filter={"user_id": user_id}
            )
        except Exception as e:
            logger.debug(f"语义去重跳过: {e}")
            return None
        if not hits:
            return None
        dist = float(hits[0][1])
        rel = 1.0 / (1.0 + max(dist, 0.0))
        if rel < dedup_sim:
            return None
        # 用近似内容的归一化结果定位已有实体的 ID
        return self._get_by_id(self._entity_id(user_id, hits[0][0].page_content))

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
    def _store_one(self, user_id, mem_type, content, confidence=1.0, delta=1.0):
        """存一条记忆：实体 ID 去重 + 权重正向强化（替代旧的「删旧存新」）。

        1. 按实体 ID（user_id + 归一化内容）精确查
        2. 未命中则语义查近似实体（MEMORY_DEDUP_SIM 阈值）
        3. 精确命中（同一实体）→ weight += delta（正向强化），内容原样保留
        4. 语义命中但内容不同（用户改口）→ 新值优先：写入新内容 + 继承累积权重，旧记录作废
        5. 都未命中 → 新增记录，weight = 初始值 1.0

        MEMORY_OVERWRITE_ON_CONFLICT=False 时第 4 类退回「只加权重、内容保留旧值」。
        """
        cfg = _cfg()
        dedup_sim = getattr(cfg, "MEMORY_DEDUP_SIM", 0.85)
        overwrite = getattr(cfg, "MEMORY_OVERWRITE_ON_CONFLICT", True)
        content = (content or "").strip()
        if not content:
            return

        entity_id = self._entity_id(user_id, content)
        existing = self._get_by_id(entity_id)
        exact = existing is not None
        if not exact:
            existing = self._find_similar(user_id, content, dedup_sim)

        if existing and (exact or not overwrite):
            # 权重正向强化：保留累积价值，而非删旧存新
            new_weight = float(existing.get("weight") or 1.0) + delta
            self._upsert(
                existing["id"], user_id,
                existing.get("mem_type") or mem_type,
                existing.get("content") or content,
                float(existing.get("confidence") or confidence),
                existing.get("created_at"),
                weight=new_weight,
            )
            _log(f"权重强化: {content[:30]} → weight={new_weight:.2f}")
        elif existing:
            # 用户改口：语义近似但事实已变 → 新值优先，累积权重继承，旧记录作废
            new_weight = float(existing.get("weight") or 1.0) + delta
            self._upsert(entity_id, user_id, mem_type, content, confidence,
                         _now_iso(), weight=new_weight)
            if existing.get("id") and existing["id"] != entity_id:
                self._delete(existing["id"])
            _log(f"记忆覆盖(改口): {str(existing.get('content'))[:24]} → {content[:24]} "
                 f"weight={new_weight:.2f}")
        else:
            self._upsert(entity_id, user_id, mem_type, content, confidence,
                         _now_iso(), weight=1.0)

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

    def extract_from_summary(self, user_id, summary) -> list:
        """从会话摘要抽取长期有效记忆（摘要驱动，替代「每轮原始对话抽取」）。

        与 extract_and_store 的区别：
        - 抽取源是「已过滤的摘要」而非原始对话 → 信噪比更高
        - 抽取口径只保留长期有效信息（偏好/固定约束/业务规则/高频工具经验）
        - 触发频率为每 N 轮一次（随摘要更新），而非每轮 → 更省

        去重与权重强化由 _store_one 承担（已存在则 weight += delta）。
        全程降级安全：任何失败静默返回 []。
        """
        if not self._ensure():
            return []
        summary = (summary or "").strip()
        if not summary:
            return []
        user_id = user_id or "default"

        try:
            from config import LLM_MODEL, MEMORY_EXTRACT_MAX_TOKENS
            from mcp_unified_agent.prompt_templates import build_memory_extract_prompt
        except Exception as e:
            _log(f"摘要抽取依赖缺失(跳过): {e}")
            return []

        try:
            prompt = build_memory_extract_prompt(summary)
            resp = self._llm_client().chat.completions.create(
                model=LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=MEMORY_EXTRACT_MAX_TOKENS,
            )
            try:
                from token_tracker import get_tracker
                get_tracker().record(LLM_MODEL, resp.usage,
                                     call_site="memory.extract_summary")
            except Exception:
                pass
            text = (resp.choices[0].message.content or "").strip()
            items = self._parse_json_array(text)
            for it in items:
                self._store_one(user_id, it.get("mem_type", "entity"),
                                it.get("content", ""), it.get("confidence", 0.8))
            if items:
                _log(f"摘要抽取 {len(items)} 条长期记忆(user={user_id})")
            return items
        except Exception as e:
            _log(f"摘要抽取失败(忽略): {e}")
            return []

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
        """V0.5：生成一句对话摘要（实体抽取为空时的兜底，会真实产生一次 LLM 调用）。"""
        from config import LLM_MODEL, MEMORY_EXTRACT_MAX_TOKENS
        resp = self._llm_client().chat.completions.create(
            model=LLM_MODEL,
            messages=[{"role": "user", "content":
                       f"用一句话概括这轮对话的核心信息(便于日后检索)：\n用户：{user_input}\n助手：{answer[:400]}\n只输出这一句话。"}],
            temperature=0,
            max_tokens=MEMORY_EXTRACT_MAX_TOKENS,
        )
        # 与 _llm_extract 保持一致：记账，否则走兜底路径时成本会漏统计
        try:
            from token_tracker import get_tracker
            get_tracker().record(LLM_MODEL, resp.usage, call_site="memory.summarize")
        except Exception:
            pass
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

    def _rerank_hits(self, query, hits, cfg):
        """对候选记忆做 Cross-Encoder 重排打分。

        返回与 hits 等长的分数列表（未被重排保留的候选记为 -inf）；
        未启用 / 候选不足 / 重排失败 一律返回 None —— 调用方跳过过滤，降级为原行为。
        """
        if not getattr(cfg, "MEMORY_RERANK_ENABLED", True):
            return None
        if len(hits) <= 1:
            return None
        try:
            from reranker import rerank_cross_encoder
            ranked = rerank_cross_encoder(query, [d for d, _ in hits])
            kept = {id(d): float((d.metadata or {}).get("rerank_score", float("-inf")))
                    for d in ranked}
            if not (kept.keys() & {id(d) for d, _ in hits}):
                # 重排返回的文档与候选无一对应：强行过滤会误杀全部记忆，故跳过过滤
                _log("记忆重排结果与候选无法对齐(跳过相关性过滤)")
                return None
            return [kept.get(id(d), float("-inf")) for d, _ in hits]
        except Exception as e:
            _log(f"记忆重排失败(跳过相关性过滤): {e}")
            return None

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
        # 相关性过滤：纯向量分在本嵌入模型上无法区分相关/不相关（实测两组完全重叠，
        # 见 技术文档/长期记忆.md 对照节），改用 Cross-Encoder 重排分做门槛。
        min_score = float(getattr(cfg, "MEMORY_MIN_RERANK_SCORE", -5.0))
        rerank_scores = self._rerank_hits(query, hits, cfg)
        scored = []
        filtered = 0
        for idx, (doc, dist) in enumerate(hits):
            if rerank_scores is not None and min_score > -999 and rerank_scores[idx] < min_score:
                filtered += 1
                continue
            m = doc.metadata or {}
            mtype = m.get("mem_type", "conclusion")
            conf = float(m.get("confidence", 1.0) or 1.0)
            decay = self._decay(m.get("created_at"), decay_days)
            mem_weight = float(m.get("weight", 1.0) or 1.0)  # 累积权重（正向强化）
            rel = 1.0 / (1.0 + max(float(dist), 0.0))   # 距离 → 0~1 相关度
            weight = rel * type_w.get(mtype, 0.4) * conf * decay * mem_weight
            scored.append((weight, mtype, doc.page_content))
        if filtered:
            _log(f"相关性过滤 {filtered} 条(阈值 {min_score})")
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

    # ── 存量迁移：事件 ID → 实体 ID ─────────────────────────
    def migrate_to_entity_id(self, dry_run: bool = False) -> dict:
        """存量迁移：重算实体 ID、合并重复实体、合并权重。

        背景：旧版 ID 含时间戳（「事件 ID」），同一实体重复出现会存成多条，
        权重无法累加。迁移后同实体合并为一条，weight 累加、时间戳取最早。

        Args:
            dry_run: True=只统计不写入

        Returns:
            {"scanned": 扫描数, "merged": 合并掉的重复数, "written": 写入数, "dry_run": bool}
        """
        if not self._ensure():
            return {"scanned": 0, "merged": 0, "written": 0, "error": "记忆未启用"}
        try:
            conn = self._sqlite()
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, user_id, mem_type, content, confidence, weight, created_at "
                "FROM memory"
            ).fetchall()
        except Exception as e:
            return {"scanned": 0, "merged": 0, "written": 0, "error": str(e)}

        merged: dict[str, dict] = {}
        old_ids: list[str] = []
        for row in rows:
            r = dict(row)
            old_ids.append(r["id"])
            nid = self._entity_id(r["user_id"], r["content"])
            if nid in merged:
                m = merged[nid]
                m["weight"] += float(r.get("weight") or 1.0)          # 权重合并
                m["confidence"] = max(m["confidence"], float(r.get("confidence") or 1.0))
                # 时间戳取最早（保留实体的首次出现时间，利于时间衰减语义）
                if r.get("created_at") and (
                    not m["created_at"] or r["created_at"] < m["created_at"]
                ):
                    m["created_at"] = r["created_at"]
            else:
                merged[nid] = {
                    "id": nid,
                    "user_id": r["user_id"],
                    "mem_type": r["mem_type"],
                    "content": r["content"],
                    "confidence": float(r.get("confidence") or 1.0),
                    "weight": float(r.get("weight") or 1.0),
                    "created_at": r.get("created_at"),
                }

        scanned, written = len(rows), len(merged)
        result = {"scanned": scanned, "merged": scanned - written,
                  "written": written, "dry_run": dry_run}
        if dry_run:
            try:
                conn.close()
            except Exception:
                pass
            return result

        try:
            # SQLite：清空后按合并结果重建
            conn.execute("DELETE FROM memory")
            for m in merged.values():
                conn.execute(
                    "INSERT OR REPLACE INTO memory "
                    "(id, user_id, mem_type, content, confidence, weight, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (m["id"], m["user_id"], m["mem_type"], m["content"],
                     m["confidence"], m["weight"], m["created_at"], _now_iso()),
                )
            conn.commit()
            conn.close()

            # Chroma：删旧 ID、写新 ID
            try:
                self._vs.delete(ids=old_ids)
            except Exception:
                pass
            for m in merged.values():
                self._vs.add_texts(
                    texts=[m["content"]],
                    metadatas=[{"user_id": m["user_id"], "mem_type": m["mem_type"],
                                "confidence": m["confidence"], "weight": m["weight"],
                                "created_at": m["created_at"]}],
                    ids=[m["id"]],
                )
            _log(f"迁移完成: 扫描 {scanned} 条 → 合并为 {written} 条"
                 f"（消除重复 {scanned - written} 条）")
        except Exception as e:
            _log(f"迁移写入失败: {e}")
            result["error"] = str(e)
        return result


def get_memory():
    return LongTermMemory.get()


# ── 自测 ──
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")

    # 存量迁移入口：python long_term_memory.py migrate [--dry-run]
    if len(sys.argv) > 1 and sys.argv[1] == "migrate":
        _m = get_memory()
        _res = _m.migrate_to_entity_id(dry_run="--dry-run" in sys.argv)
        print(f"迁移结果: {_res}")
        sys.exit(0)

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
