"""Skill 注册表

存储所有已加载 Skill，提供关键词+向量双重匹配、格式化输出。
"""

import logging
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)


class SkillRegistry:
    """Skill 注册表：加载、查询、匹配。

    匹配策略（双重机制）：
    1. 关键词初筛：用户输入中 trigger_keywords 命中数 + exclude_keywords 排除
    2. 描述向量相似度：对通过初筛的候选做余弦相似度排序
    3. 返回 Top3，由 LLM 最终确认
    """

    def __init__(self):
        self._skills: list[dict] = []
        self._skill_vectors: Optional[np.ndarray] = None
        self._skill_names: list[str] = []
        self._embeddings_ready: bool = False

    # ── 加载 ──────────────────────────────────────────────────

    def load_all(self, reload: bool = False) -> int:
        """从 skills/ 目录加载所有 Skill 定义。"""
        import sys
        import os
        _parent = os.path.dirname(os.path.dirname(__file__))
        if _parent not in sys.path:
            sys.path.insert(0, _parent)
        from skills import discover_skills

        self._skills = discover_skills(reload=reload)
        self._build_vector_index()
        logger.info(f"Skill 注册表已加载 {len(self._skills)} 个 Skill")
        return len(self._skills)

    def _build_vector_index(self):
        """构建 Skill 描述向量索引（用于语义匹配）。"""
        if not self._skills:
            self._embeddings_ready = False
            return

        try:
            from vector_store import get_embeddings

            embeddings = get_embeddings()
            descriptions = [s.get("description", s["name"]) for s in self._skills]
            vectors = embeddings.embed_documents(descriptions)

            self._skill_vectors = np.array(vectors, dtype=np.float32)
            self._skill_names = [s["name"] for s in self._skills]
            self._embeddings_ready = True
            logger.info(f"Skill 向量索引构建完成: {len(self._skill_names)} 个, "
                        f"维度 {self._skill_vectors.shape}")
        except Exception as e:
            logger.warning(f"Skill 向量索引构建失败（将仅用关键词匹配）: {e}")
            self._embeddings_ready = False

    # ── 匹配 ──────────────────────────────────────────────────

    def match(self, user_input: str, max_candidates: int = 3) -> list[dict]:
        """匹配用户输入到最相关的 Skill。

        Args:
            user_input: 用户原始输入
            max_candidates: 返回的候选 Skill 数量

        Returns:
            [{skill, score, match_type}, ...] 按相关度降序排列
        """
        if not self._skills:
            return []

        user_lower = user_input.lower()

        # 第一轮：关键词+负向词过滤
        candidates = []
        for skill in self._skills:
            # 负向词检查（命中任一即排除）
            exclude_words = skill.get("exclude_keywords", [])
            if exclude_words and any(w.lower() in user_lower for w in exclude_words):
                logger.debug(f"Skill {skill['name']} 被负向词排除")
                continue

            # 关键词命中
            keywords = skill.get("trigger_keywords", [])
            if not keywords:
                continue  # 没有触发词的 Skill 不参与自动匹配

            keyword_hits = sum(1 for kw in keywords if kw.lower() in user_lower)
            if keyword_hits > 0:
                candidates.append({
                    "skill": skill,
                    "keyword_hits": keyword_hits,
                    "vector_score": 0.0,
                })

        # 第二轮：向量相似度排序（如果向量索引可用 + 候选 > max_candidates）
        if self._embeddings_ready and len(candidates) > 0:
            try:
                from vector_store import get_embeddings

                embeddings = get_embeddings()
                query_vec = np.array(embeddings.embed_query(user_input), dtype=np.float32)

                for c in candidates:
                    idx = self._skill_names.index(c["skill"]["name"])
                    skill_vec = self._skill_vectors[idx]
                    # 余弦相似度
                    dot = np.dot(query_vec, skill_vec)
                    norm_q = np.linalg.norm(query_vec)
                    norm_s = np.linalg.norm(skill_vec)
                    if norm_q > 0 and norm_s > 0:
                        c["vector_score"] = float(dot / (norm_q * norm_s))
            except Exception as e:
                logger.warning(f"向量匹配失败，降级纯关键词: {e}")

        # 综合排序：关键词命中数 × 0.3 + 向量分数 × 0.7
        for c in candidates:
            keyword_score = min(c["keyword_hits"] / max(len(c["skill"].get("trigger_keywords", [])), 1), 1.0)
            c["score"] = round(keyword_score * 0.3 + c["vector_score"] * 0.7, 4)

        candidates.sort(key=lambda x: x["score"], reverse=True)

        # 过滤低于个人阈值的
        result = []
        for c in candidates[:max_candidates]:
            threshold = c["skill"].get("match_threshold", 0.6)
            if c["score"] >= threshold:
                result.append(c)

        if result:
            logger.info(
                f"Skill 匹配: Top{len(result)} → "
                + ", ".join(f"{c['skill']['name']}({c['score']:.2f})" for c in result)
            )
        else:
            logger.info(f"Skill 匹配: 无结果（共 {len(candidates)} 候选均低于阈值）")

        return result

    # ── 查询 ──────────────────────────────────────────────────

    def get(self, name: str) -> Optional[dict]:
        """按名称获取 Skill 定义。"""
        for s in self._skills:
            if s["name"] == name:
                return s
        return None

    def get_all(self) -> list[dict]:
        return list(self._skills)

    def get_names(self) -> list[str]:
        return [s["name"] for s in self._skills]

    def format_for_prompt(self, skills: list[dict]) -> str:
        """将指定 Skill 列表格式化为 ReAct Prompt 可用的描述文本。

        只放匹配的 Top3 候选，不放全量，避免 Prompt 膨胀。
        """
        if not skills:
            return "（无匹配技能）"

        lines = []
        for s in skills:
            lines.append(f"### {s['name']}")
            lines.append(f"描述：{s['description']}")
            lines.append(f"参数：{', '.join(s.get('arg_slots', {}).keys())}")
            lines.append(f"执行模式：{s.get('execution_mode', 'serial')}")
            lines.append("")
        return "\n".join(lines)

    @property
    def count(self) -> int:
        return len(self._skills)


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== Skill 注册表自测 ===\n")

    registry = SkillRegistry()
    count = registry.load_all(reload=True)
    print(f"加载: {count} 个 Skill\n")

    # 测试匹配
    test_queries = [
        "今天常州天气怎么样，什么是机器学习",
        "北京明天穿什么衣服",
        "详细解释一下RAG的原理，给我原文引用",
        "你好，讲个笑话",
        "天气API怎么调用",
    ]

    for q in test_queries:
        print(f"🔍 查询: {q}")
        results = registry.match(q)
        if results:
            for r in results:
                print(f"   → {r['skill']['name']} (score={r['score']:.2f}, "
                      f"kw={r['keyword_hits']}, vec={r['vector_score']:.2f})")
        else:
            print(f"   → 无匹配（走 ReAct 兜底）")
        print()

    # 测试格式化
    if results:
        print("📋 Prompt 格式输出:")
        print(registry.format_for_prompt([r["skill"] for r in results]))

    print("🎉 Skill 注册表自测完成！")
