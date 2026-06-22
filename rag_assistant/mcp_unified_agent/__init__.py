"""MCP 统一智能体

基于 MCP 协议的多工具智能体，实现 LLM 自主完成：
- 工具识别与元数据发现
- 工具选型与参数生成
- MCP 协议调用（串行/并行）
- 结果循环推理与最终回答

对外接口：UnifiedAgent —— 兼容原 Agent 的 chat(user_input) → str 签名。
"""

from .unified_agent import UnifiedAgent

__all__ = ["UnifiedAgent"]
