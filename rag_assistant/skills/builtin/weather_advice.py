"""天气出行建议 Skill —— 查天气 + LLM 分析给出穿衣/出行建议"""

SKILL = {
    "name": "weather_advice",
    "description": "查询某城市天气后给出穿衣和出行建议（是否需要带伞、穿什么、出行注意事项）",
    "trigger_keywords": ["穿什么", "穿", "衣服", "带伞", "出行", "旅游", "建议", "天气怎么样", "冷不冷", "热不热", "怎么穿", "穿搭"],
    "exclude_keywords": ["代码", "API", "接口", "开发"],
    "match_threshold": 0.4,
    "steps": [
        {
            "tool": "query_weather",
            "args": {"city": "{city}"},
            "retryable": True,
            "critical": True,
        },
    ],
    "execution_mode": "serial",
    "post_process": True,
    "final_prompt": (
        "基于以下天气信息，用简洁实用的语言给出：\n"
        "1. 穿衣建议（适合什么厚度的衣服）\n"
        "2. 是否需要带伞或雨具\n"
        "3. 出行注意事项（如防晒、防风等）\n\n"
        "天气信息：\n{results}"
    ),
    "arg_slots": {
        "city": {"description": "城市名", "type": "string", "required": True},
    },
}
