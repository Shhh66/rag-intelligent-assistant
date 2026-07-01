"""Skill 自动发现 —— 扫描 skills/ 目录，加载所有 SKILL 定义

新增 Skill = 在 skills/builtin/ 下新建 .py 文件并定义 SKILL 字典，
Agent 重启时自动加载，和 MCP 的 @mcp.tool() 一样零摩擦。
"""

import importlib
import importlib.util
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 已加载的 Skill 缓存
_skills_cache: Optional[list[dict]] = None


def discover_skills(reload: bool = False) -> list[dict]:
    """扫描 skills/builtin/*.py，提取所有 SKILL 字典。

    Args:
        reload: 是否强制重新扫描（默认使用缓存）

    Returns:
        所有 Skill 定义的列表，每项为 SKILL 字典的浅拷贝
    """
    global _skills_cache
    if _skills_cache is not None and not reload:
        return list(_skills_cache)

    skills: list[dict] = []
    builtin_dir = Path(__file__).resolve().parent / "builtin"

    if not builtin_dir.exists():
        logger.warning(f"Skills 目录不存在: {builtin_dir}")
        _skills_cache = skills
        return skills

    for filepath in builtin_dir.glob("*.py"):
        if filepath.name.startswith("_"):
            continue  # 跳过 __init__.py 等

        module_name = filepath.stem
        try:
            spec = importlib.util.spec_from_file_location(
                f"skills.builtin.{module_name}", filepath
            )
            if spec is None or spec.loader is None:
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            if hasattr(module, "SKILL") and isinstance(module.SKILL, dict):
                skill = module.SKILL
                # 基础校验
                if _validate_skill(skill, module_name):
                    skills.append(skill)
                    logger.info(f"加载 Skill: {skill.get('name', module_name)}")
                else:
                    logger.warning(f"Skill 校验失败，跳过: {module_name}")
            else:
                logger.debug(f"跳过非 Skill 文件: {module_name}（无 SKILL 字典）")

        except Exception as e:
            logger.error(f"加载 Skill 文件失败 {module_name}: {e}")

    logger.info(f"Skill 加载完成: {len(skills)} 个")
    _skills_cache = skills
    return list(skills)


def _validate_skill(skill: dict, module_name: str) -> bool:
    """校验 Skill 定义的完整性。"""
    required_fields = ["name", "description", "steps"]
    for field in required_fields:
        if field not in skill:
            logger.warning(f"Skill {module_name} 缺少必要字段: {field}")
            return False

    if not isinstance(skill["steps"], list) or len(skill["steps"]) == 0:
        logger.warning(f"Skill {module_name} 的 steps 为空或格式错误")
        return False

    for i, step in enumerate(skill["steps"]):
        if "tool" not in step:
            logger.warning(f"Skill {module_name} 的 step[{i}] 缺少 tool 字段")
            return False

    return True


def clear_cache():
    """清空 Skill 缓存（用于测试或热重载）。"""
    global _skills_cache
    _skills_cache = None


# ===== 自测代码 =====
if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")

    print("=== Skill 自动发现测试 ===\n")
    skills = discover_skills(reload=True)

    print(f"加载 {len(skills)} 个 Skill:\n")
    for s in skills:
        print(f"  📋 {s['name']}")
        print(f"     描述: {s['description'][:60]}...")
        print(f"     触发词: {s.get('trigger_keywords', [])[:5]}")
        print(f"     负向词: {s.get('exclude_keywords', [])}")
        print(f"     步骤数: {len(s['steps'])}")
        print(f"     执行模式: {s.get('execution_mode', 'serial')}")
        print(f"     置信度阈值: {s.get('match_threshold', 0.6)}")
        print()

    print("🎉 Skill 自动发现测试完成！")
