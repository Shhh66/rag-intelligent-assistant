"""benchmark_parallel.py — 实测自研并行调度 vs 串行的耗时收益

背景：简历写「优化 LangChain 原生串行缺陷，支持多工具并行执行」。
本脚本量化「并行(asyncio.gather) vs 串行(顺序 await)」的耗时差异。

原理（对齐 scheduler._execute_parallel / _execute_serial）：
- 串行：顺序 await 每个工具，总耗时 = sum(各工具耗时)
- 并行：asyncio.gather 并发，总耗时 = max(各工具耗时)
- 适用前提：工具之间无依赖（本项目 comprehensive_query Skill 的「天气+知识库」即此类）

工具耗时用 asyncio.sleep 模拟（理想化微基准，排除网络/模型波动），
真实场景收益 = sum(真实耗时) - max(真实耗时)，取决于各工具耗时。
"""

import asyncio
import time

# 模拟 3 个无依赖工具的真实耗时（秒），取典型量级：
#   query_weather         ~1.5s（外部 API 网络往返）
#   search_knowledge_base ~1.5s（本地检索 + 可能 LLM）
#   edu_query_schedule    ~0.5s（教务查询）
TOOLS = [
    ("query_weather", 1.5),
    ("search_knowledge_base", 1.5),
    ("edu_query_schedule", 0.5),
]


async def _tool(name, cost):
    await asyncio.sleep(cost)
    return f"{name} done"


async def main():
    N = 10  # 采样次数，取均值平滑调度开销
    serial_times = []
    parallel_times = []

    for _ in range(N):
        # 串行：顺序 await（对齐 _execute_serial）
        t0 = time.perf_counter()
        for name, cost in TOOLS:
            await _tool(name, cost)
        serial_times.append(time.perf_counter() - t0)

        # 并行：gather（对齐 _execute_parallel）
        t0 = time.perf_counter()
        await asyncio.gather(*[_tool(name, cost) for name, cost in TOOLS])
        parallel_times.append(time.perf_counter() - t0)

    avg_s = sum(serial_times) / N
    avg_p = sum(parallel_times) / N
    theory_s = sum(c for _, c in TOOLS)
    theory_p = max(c for _, c in TOOLS)

    print("=== 自研并行调度 vs 串行（理想化微基准）===")
    print(f"工具集: {', '.join(f'{n}({c}s)' for n, c in TOOLS)}")
    print(f"采样 {N} 次\n")
    print(f"串行(顺序 await) 平均: {avg_s*1000:7.1f} ms   (理论 = sum = {theory_s*1000:.0f} ms)")
    print(f"并行(gather)     平均: {avg_p*1000:7.1f} ms   (理论 = max = {theory_p*1000:.0f} ms)")
    print(f"并行省下:          {(avg_s-avg_p)*1000:7.1f} ms   ({(1-avg_p/avg_s)*100:.1f}%)")
    print("=" * 60)
    print("结论：无依赖工具越多、单工具耗时越长，并行收益越大。")
    print("      串行 = sum(耗时)，并行 = max(耗时)，差 = 其余工具耗时之和。")
    print("注意：这是理想化微基准（sleep 模拟），真实收益取决于各工具真实耗时。")


if __name__ == "__main__":
    asyncio.run(main())
