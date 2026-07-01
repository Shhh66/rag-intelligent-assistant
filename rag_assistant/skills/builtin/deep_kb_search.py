"""深度知识库检索 Skill —— 先语义检索 + 再 LLM 生成（适合需要精确引用的场景）"""

SKILL = {
    "name": "deep_kb_search",
    "description": "先纯检索找到相关片段，再基于片段生成详细回答，适合需要精确引用或原文出处的场景",
    "trigger_keywords": ["详细", "原文", "引用", "出处", "精准", "具体内容", "深入"],
    "exclude_keywords": [],
    "match_threshold": 0.7,  # 精准类场景，提高门槛
    "steps": [
        {
            "tool": "search_knowledge_base",
            "args": {"query": "{query}", "top_k": 8},
            "retryable": True,
            "critical": True,
        },
        {
            "tool": "ask_knowledge_base",
            "args": {"query": "{query}"},
            "retryable": True,
            "critical": True,
        },
    ],
    "execution_mode": "serial",  # 先搜后答，有依赖
    "arg_slots": {
        "query": {"description": "要查询的问题", "type": "string", "required": True},
    },
}
