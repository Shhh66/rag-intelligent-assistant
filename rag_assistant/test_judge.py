"""独立 Judge 模型测试脚本

测试场景：
1. 正常输入（天气查询）- 信息足够，应返回 Yes
2. 边界输入（复杂对比）- 需要多轮收集
3. 异常场景（无意义输入）- 直接回答
"""

import sys
import os

# 调整路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(os.path.dirname(os.path.abspath(__file__)))


def test_judge_normal():
    """测试 1: 正常输入（天气查询）"""
    print("\n" + "=" * 60)
    print("测试 1: 正常输入（今天北京天气怎么样？）")
    print("=" * 60)

    import config
    config.JUDGE_ENABLED = True

    from agent import Agent
    a = Agent()
    result = a.chat("今天北京天气怎么样？")
    print(f"\n回答: {result}")


def test_judge_complex():
    """测试 2: 边界输入（复杂对比查询）"""
    print("\n" + "=" * 60)
    print("测试 2: 边界输入（帮我查一下北京和上海的天气，然后对比哪个更适合旅游）")
    print("=" * 60)

    import config
    config.JUDGE_ENABLED = True

    from agent import Agent
    a = Agent()
    result = a.chat("帮我查一下北京和上海的天气，然后对比哪个更适合旅游")
    print(f"\n回答: {result}")


def test_judge_nonsensical():
    """测试 3: 异常场景（无意义输入）"""
    print("\n" + "=" * 60)
    print("测试 3: 异常场景（asdfghjkl12345!@#$%^&*()）")
    print("=" * 60)

    import config
    config.JUDGE_ENABLED = True

    from agent import Agent
    a = Agent()
    result = a.chat("asdfghjkl12345!@#$%^&*()")
    print(f"\n回答: {result}")


def test_judge_disabled():
    """测试 4: Judge 关闭（向后兼容）"""
    print("\n" + "=" * 60)
    print("测试 4: Judge 关闭（JUDGE_ENABLED=False）")
    print("=" * 60)

    import config
    config.JUDGE_ENABLED = False

    from agent import Agent
    a = Agent()
    result = a.chat("今天北京天气怎么样？")
    print(f"\n回答: {result}")


def main():
    print("🧪 独立 Judge 模型测试")
    print("=" * 60)

    # 测试 1: 正常输入
    test_judge_normal()

    # 测试 2: 边界输入
    test_judge_complex()

    # 测试 3: 异常场景
    test_judge_nonsensical()

    # 测试 4: Judge 关闭
    test_judge_disabled()

    print("\n" + "=" * 60)
    print("✅ 所有测试完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
