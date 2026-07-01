"""综合查询 Skill —— 并行查天气 + 知识库"""

SKILL = {
    "name": "comprehensive_query",
    "description": "同时查询天气和知识库，适用于用户同时问「天气怎么样 + 某个知识概念」的组合问题",
    "trigger_keywords": ["天气", "知识", "同时", "还有", "以及", "顺便"],
    "exclude_keywords": ["API", "接口", "代码", "开发", "调用"],
    "match_threshold": 0.4,  # 高频通用，适当降低门槛
    "steps": [
        {
            "tool": "query_weather",
            "args": {"city": "{city}"},
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
    "execution_mode": "parallel",
    "post_process": True,
    "final_prompt": (
        "基于以下工具返回的信息，综合回答用户的问题：\n\n"
        "{results}\n\n"
        "注意：\n"
        "1. 如果用户问了天气相关的问题（如穿什么、带伞、冷不冷等），"
        "请基于天气数据给出具体的生活建议\n"
        "2. 如果用户问了知识问题，请清晰准确地回答\n"
        "3. 如果两类问题都问了，请分别回答，用分隔线隔开"
    ),
    "arg_slots": {
        "city": {"description": "城市名", "type": "string", "required": True},
        "query": {"description": "要查询的知识问题", "type": "string", "required": True},
    },
}
