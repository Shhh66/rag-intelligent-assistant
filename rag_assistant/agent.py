"""Agent 模块 —— MCP 统一智能体

基于 MCP 协议的多工具自主决策智能体：
- 会话初始化时自动发现 MCP 工具（无需硬编码）
- LLM 自主决定调用哪个工具 / 直接回答
- 支持串行和并行 MCP 工具调用
- 向量预筛选 + 反思记忆优化

对外接口保持向后兼容：Agent().chat(user_input) → str
"""

from mcp_unified_agent import UnifiedAgent


class Agent(UnifiedAgent):
    """向后兼容的 Agent 别名。

    委托给 MCP 统一智能体实现，保持原有 Agent 的公开接口不变。
    app.py 中的 `st.session_state.agent = Agent()` 行为完全兼容。
    """

    def __init__(self):
        # 使用 UnifiedAgent 的默认配置
        super().__init__()
