"""工具注册表

缓存 MCP 工具元数据，提供查询、Schema 校验、Prompt 格式化能力。

从 MCP list_tools() 返回的 Tool 对象中提取标准化元数据：
- name: 工具名
- description: 工具描述
- input_schema: JSON Schema dict（用于参数校验）
"""

import logging
from dataclasses import dataclass, field

import jsonschema

logger = logging.getLogger(__name__)


@dataclass
class ToolMeta:
    """标准化的工具元数据，从 MCP Tool 对象提取。"""
    name: str
    description: str
    input_schema: dict           # JSON Schema dict，用于参数校验
    source_server: str = "mcp_server"  # 预留多服务器扩展

    @classmethod
    def from_mcp_tool(cls, tool) -> "ToolMeta":
        """从 MCP Tool 对象构造 ToolMeta。"""
        return cls(
            name=tool.name,
            description=tool.description or "",
            input_schema=tool.inputSchema or {},
        )


class ToolRegistry:
    """工具注册表：缓存工具元数据，提供查询、校验、格式化能力。"""

    def __init__(self):
        self._tools: dict[str, ToolMeta] = {}

    # ── 加载与查询 ────────────────────────────────────────────

    def load(self, tools: list) -> None:
        """从 MCP Tool 或 ToolMeta 对象列表加载到内存。

        自动检测对象类型：MCP Tool 使用 from_mcp_tool 转换，
        ToolMeta 直接使用。
        """
        self._tools.clear()
        for t in tools:
            if isinstance(t, ToolMeta):
                meta = t
            else:
                meta = ToolMeta.from_mcp_tool(t)
            self._tools[meta.name] = meta
        logger.info(f"工具注册表已加载 {len(self._tools)} 个工具: "
                     f"{list(self._tools.keys())}")

    def get_all(self) -> list[ToolMeta]:
        """返回全部工具元数据列表。"""
        return list(self._tools.values())

    def get(self, name: str) -> ToolMeta | None:
        """根据名称获取单个工具元数据。"""
        return self._tools.get(name)

    def get_names(self) -> list[str]:
        """返回全部工具名称列表。"""
        return list(self._tools.keys())

    def get_by_names(self, names: list[str]) -> list[ToolMeta]:
        """根据名称列表批量获取工具元数据。"""
        return [self._tools[n] for n in names if n in self._tools]

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    @property
    def count(self) -> int:
        return len(self._tools)

    # ── Schema 校验 ───────────────────────────────────────────

    def validate(self, name: str, arguments: dict) -> tuple[bool, str | None]:
        """用 jsonschema 库校验 arguments 是否符合工具的 inputSchema。

        Args:
            name: 工具名称
            arguments: 待校验的参数字典

        Returns:
            (是否通过, 错误信息) —— 通过时错误信息为 None
        """
        tool = self._tools.get(name)
        if tool is None:
            return False, f"工具不存在: {name}"

        schema = tool.input_schema
        if not schema:
            # 没有 Schema 定义，跳过校验（允许通过）
            return True, None

        try:
            jsonschema.validate(instance=arguments, schema=schema)
            return True, None
        except jsonschema.ValidationError as e:
            return False, str(e)
        except Exception as e:
            return False, f"Schema 校验异常: {e}"

    # ── Prompt 格式化 ─────────────────────────────────────────

    def format_for_prompt(self, tool_names: list[str] | None = None) -> str:
        """将所有（或指定）工具的元数据格式化为 LLM Prompt 可用的字符串。

        格式：
        ### query_weather
        描述：查询指定城市的实时天气信息
        参数：
          - city (string, 必填): 城市名称
        """
        if tool_names is not None:
            tools = self.get_by_names(tool_names)
        else:
            tools = self.get_all()

        if not tools:
            return "（无可用工具）"

        lines = []
        for t in tools:
            lines.append(f"### {t.name}")
            lines.append(f"描述：{t.description}")

            props = t.input_schema.get("properties", {})
            required = t.input_schema.get("required", [])

            if props:
                lines.append("参数：")
                for param_name, param_schema in props.items():
                    param_type = param_schema.get("type", "string")
                    param_desc = param_schema.get("description", "")
                    is_required = "必填" if param_name in required else "可选"
                    lines.append(
                        f"  - {param_name} ({param_type}, {is_required}): {param_desc}"
                    )

            lines.append("")  # 空行分隔

        return "\n".join(lines)
