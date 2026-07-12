"""RAG 评测主脚本 —— A/B 对比 + RAGAS 4 指标 + 分层 + Bad Case + 成本 + 报告。

用法：
    python rag_eval/run_eval.py --config both               # 完整评测(基线 vs 优化)
    python rag_eval/run_eval.py --config both --limit 2     # 小样本快跑(省 Token)
    python rag_eval/run_eval.py --config optimized          # 只跑优化配置
    python rag_eval/run_eval.py --config both --save-baseline  # 把优化结果存为基线快照

配置说明：
    Baseline  : use_bilingual=False, use_rerank=False  （纯向量单路检索）
    Optimized : use_bilingual=True,  use_rerank=True   （双语合并 + BGE 重排）

指标(RAGAS)：
    context_recall     上下文召回率
    context_precision  上下文精准率
    faithfulness       忠实度（幻觉率 = 1 - faithfulness）
    answer_relevancy   答案相关性
"""

import os
import sys
import json
import re
import argparse
import warnings
from datetime import datetime
from pathlib import Path

warnings.filterwarnings("ignore", category=DeprecationWarning)

# 允许从 rag_eval/ 导入父目录项目模块
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_THIS_DIR)
sys.path.insert(0, _PROJECT_DIR)

from retriever import retrieve_and_answer
from token_tracker import get_tracker
from adapters import get_ragas_llm, get_ragas_embeddings

TESTSET_PATH = os.path.join(_THIS_DIR, "testset.json")
REPORTS_DIR = os.path.join(_THIS_DIR, "reports")
BASELINE_PATH = os.path.join(_THIS_DIR, "baseline.json")

# A/B 两组配置
CONFIGS = {
    "baseline":  {"use_bilingual": False, "use_rerank": False,
                  "use_hybrid": False, "use_rewrite": False, "label": "Baseline(纯向量)"},
    "optimized": {"use_bilingual": True,  "use_rerank": True,
                  "use_hybrid": True,  "use_rewrite": True,
                  "label": "Optimized(改写+混合检索+双语+重排)"},
}

# 指标中文名 + 是否越低越好
METRIC_NAMES = {
    "context_recall":    "上下文召回率",
    "context_precision": "上下文精准率",
    "faithfulness":      "忠实度",
    "answer_relevancy":  "答案相关性",
}


def clean_answer_for_eval(answer: str) -> str:
    """清洗答案里的引用/免责格式，只保留纯事实内容，供 RAGAS 打分。

    背景：build_prompt 让 LLM 生成带来源标注/参考列表/免责声明的答案，这些
    格式行会被 RAGAS 的 faithfulness 当成"无法被上下文支撑的 claim"而误判为幻觉。
    清洗只作用于喂给评测的副本，不改真实答案。

    清洗三类格式（均由 retriever.build_prompt 规则生成）：
      1. 行内来源标注：（来源：文件名，第X页）/ (来源：...) —— 中英文括号都覆盖
      2. 来源列表块：> 📚 参考来源： 及其后连续的 > 引用行
      3. 免责声明：> ⚠️ 本回答并非基于上传的知识库文档...

    正则保守：宁可漏删也不误删事实；清洗后为空则回退原文，避免喂空串。
    """
    if not answer:
        return answer
    text = answer

    # 2. 来源列表块：从 "> 📚 参考来源" 起，删除该行及其后连续的引用行
    #    （后续以 > 或 - 开头的行、空行都属于引用块，直到遇到正常正文行或结尾）
    text = re.sub(
        r"\n*>?\s*📚\s*参考来源[：:][^\n]*(?:\n[ \t]*(?:>|-)[^\n]*)*",
        "",
        text,
    )

    # 3. 免责声明行：> ⚠️ ... 由大模型直接生成。
    text = re.sub(r"\n*>?\s*⚠️[^\n]*", "", text)

    # 1. 行内来源标注：（来源：...）/ (来源：...)，中英文括号
    text = re.sub(r"[（(]\s*来源[：:][^）)]*[）)]", "", text)

    # 折叠多余空行与行尾空白
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    return text if text else answer


def load_testset(limit=None):
    with open(TESTSET_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if limit:
        # 尽量覆盖多个 category：按 category 轮流取
        by_cat = {}
        for item in data:
            by_cat.setdefault(item.get("category", "other"), []).append(item)
        picked, pools = [], list(by_cat.values())
        while len(picked) < limit and any(pools):
            for pool in pools:
                if pool and len(picked) < limit:
                    picked.append(pool.pop(0))
        return picked
    return data


def collect_samples(testset, cfg):
    """对每题跑指定配置，收集 RAGAS 所需字段。"""
    from ragas import SingleTurnSample

    samples, meta = [], []
    for i, item in enumerate(testset, 1):
        q = item["question"]
        print(f"   [{i}/{len(testset)}] ({item.get('category','?')}) {q[:36]}...",
              file=sys.stderr)
        try:
            answer, contexts = retrieve_and_answer(
                q,
                use_bilingual=cfg["use_bilingual"],
                use_rerank=cfg["use_rerank"],
                use_hybrid=cfg.get("use_hybrid", False),
                use_rewrite=cfg.get("use_rewrite", False),
            )
        except Exception as e:
            print(f"      ⚠️ 生成失败: {e}", file=sys.stderr)
            answer, contexts = f"生成失败: {e}", []

        # 清洗引用/免责格式后再喂给 RAGAS（faithfulness / answer_relevancy），
        # 消除格式噪声导致的误判；meta 仍存原始答案供报告展示。
        answer_clean = clean_answer_for_eval(answer)

        samples.append(SingleTurnSample(
            user_input=q,
            retrieved_contexts=contexts if contexts else ["(未检索到内容)"],
            response=answer_clean or "(空回答)",
            reference=item["ground_truth"],
        ))
        meta.append({
            "question": q,
            "category": item.get("category", "other"),
            "answer": answer,                 # 原始答案（含来源标注），报告展示用
            "answer_cleaned": answer_clean,   # 清洗后（喂 RAGAS），调试对比用
            "contexts": contexts,
            "ground_truth": item["ground_truth"],
        })
    return samples, meta


def run_ragas(samples):
    """调用 RAGAS 计算 4 指标，返回逐题分数 DataFrame。"""
    from ragas import evaluate, EvaluationDataset
    from ragas.metrics import (
        faithfulness, answer_relevancy, context_precision, context_recall,
    )

    dataset = EvaluationDataset(samples=samples)
    # DeepSeek 端点在高并发下易超时，调大单请求超时、降低并发，避免 Job 超时污染均值(NaN)
    from ragas.run_config import RunConfig
    run_config = RunConfig(timeout=180, max_workers=4, max_retries=3)
    result = evaluate(
        dataset=dataset,
        metrics=[context_recall, context_precision, faithfulness, answer_relevancy],
        llm=get_ragas_llm(),
        embeddings=get_ragas_embeddings(),
        run_config=run_config,
    )
    return result.to_pandas()


def summarize(df):
    """整体平均分。"""
    out = {}
    for key in METRIC_NAMES:
        if key in df.columns:
            out[key] = float(df[key].mean(skipna=True))
    return out


def summarize_by_category(df, meta):
    """分层平均分：category -> {metric: mean}。"""
    cats = {}
    for i, m in enumerate(meta):
        cats.setdefault(m["category"], []).append(i)
    result = {}
    for cat, idxs in cats.items():
        sub = df.iloc[idxs]
        result[cat] = {k: float(sub[k].mean(skipna=True))
                       for k in METRIC_NAMES if k in df.columns}
    return result


def pick_bad_cases(df, meta, threshold=0.5, max_n=3):
    """挑低分样例(任一指标 < threshold)。"""
    bad = []
    for i, m in enumerate(meta):
        row = df.iloc[i]
        low = {k: float(row[k]) for k in METRIC_NAMES
               if k in df.columns and row[k] == row[k] and float(row[k]) < threshold}
        if low:
            worst = min(low.values())
            bad.append((worst, i, low, m))
    bad.sort(key=lambda x: x[0])
    return bad[:max_n]


def fmt_pct(v):
    return f"{v*100:.1f}%" if v == v else "N/A"


def build_report(results, meta_map, args, ts):
    """生成 Markdown 报告。results: {config_key: {'overall':..,'by_cat':..,'df':..}}"""
    import ragas as _ragas
    lines = []
    lines.append(f"# RAG 评测报告\n")
    lines.append(f"> 生成时间：{ts}\n")

    # ── 元信息(可复现性) ──
    lines.append("## 一、评测元信息(可复现性)\n")
    try:
        import langchain_core
        lc_ver = langchain_core.__version__
    except Exception:
        lc_ver = "?"
    from config import EMBEDDING_MODEL, LLM_MODEL
    lines.append(f"- RAGAS 版本：`{_ragas.__version__}`")
    lines.append(f"- langchain-core 版本：`{lc_ver}`")
    lines.append(f"- 嵌入模型：`{EMBEDDING_MODEL}`")
    lines.append(f"- 评测 LLM：`{LLM_MODEL}`（temperature=0）")
    # 知识库版本
    try:
        from vector_store import get_status
        _s = get_status()
        kb_info = f"{_s.get('document_count','?')} 篇文档 / {_s.get('total_chunks','?')} chunks"
    except Exception:
        kb_info = "见 kb_manager.py status"
    lines.append(f"- 知识库规模：{kb_info}")
    lines.append(f"- 运行参数：--config {args.config} --limit {args.limit or '全部'}")
    lines.append(f"- 评测题数：{len(next(iter(meta_map.values())))}\n")

    # ── 打分一致性说明 ──
    lines.append("> **打分一致性**：LLM-as-a-judge 采用固定 `temperature=0`，"
                 "同一测试集相对分数稳定，适合 A/B 对比；绝对分数仅作参考。\n")
    lines.append("> **答案清洗**：faithfulness 与 answer_relevancy 基于**去除引用/免责格式后的"
                 "纯事实文本**计算，以消除 RAGAS 对结构化格式的误判；召回率/精准率评的是检索上下文，"
                 "不受答案格式影响。\n")

    # ── 整体指标对比 ──
    lines.append("## 二、整体指标对比\n")
    keys = list(results.keys())
    if len(keys) == 2 and "baseline" in results and "optimized" in results:
        b, o = results["baseline"]["overall"], results["optimized"]["overall"]
        lines.append("| 指标 | Baseline | Optimized | 提升 |")
        lines.append("|------|:---:|:---:|:---:|")
        for k, name in METRIC_NAMES.items():
            bv, ov = b.get(k, float("nan")), o.get(k, float("nan"))
            delta = (ov - bv) if (bv == bv and ov == ov) else float("nan")
            arrow = "🔺" if delta > 0.001 else ("🔻" if delta < -0.001 else "≈")
            lines.append(f"| {name} | {fmt_pct(bv)} | {fmt_pct(ov)} | {arrow} {fmt_pct(delta) if delta==delta else 'N/A'} |")
        # 幻觉率
        bh = 1 - b.get("faithfulness", float("nan"))
        oh = 1 - o.get("faithfulness", float("nan"))
        dh = (oh - bh) if (bh == bh and oh == oh) else float("nan")
        arrow = "🔻(改善)" if dh < -0.001 else ("🔺(恶化)" if dh > 0.001 else "≈")
        lines.append(f"| **幻觉率**(1-忠实度) | {fmt_pct(bh)} | {fmt_pct(oh)} | {arrow} {fmt_pct(dh) if dh==dh else 'N/A'} |")
    else:
        for k in keys:
            lines.append(f"\n**{CONFIGS[k]['label']}**\n")
            lines.append("| 指标 | 得分 |")
            lines.append("|------|:---:|")
            for mk, name in METRIC_NAMES.items():
                lines.append(f"| {name} | {fmt_pct(results[k]['overall'].get(mk, float('nan')))} |")
    lines.append("")

    # ── 分层指标 ──
    lines.append("## 三、分层指标(按题型)\n")
    cat_label = {"simple_fact": "简单事实", "multi_hop": "多跳推理",
                 "cross_lingual": "跨语言", "fuzzy": "边缘模糊", "other": "其他"}
    for k in keys:
        lines.append(f"\n**{CONFIGS[k]['label']}**\n")
        by_cat = results[k]["by_cat"]
        header = "| 题型 | " + " | ".join(METRIC_NAMES.values()) + " |"
        lines.append(header)
        lines.append("|------|" + "|".join([":---:"] * len(METRIC_NAMES)) + "|")
        for cat, scores in by_cat.items():
            row = f"| {cat_label.get(cat, cat)} | " + \
                  " | ".join(fmt_pct(scores.get(mk, float("nan"))) for mk in METRIC_NAMES) + " |"
            lines.append(row)
    lines.append("")

    # ── Bad Case ──
    lines.append("## 四、Bad Case 错误分析\n")
    focus = "optimized" if "optimized" in results else keys[-1]
    bad = pick_bad_cases(results[focus]["df"], meta_map[focus])
    if not bad:
        lines.append(f"✅ {CONFIGS[focus]['label']} 配置下无明显低分样例(所有指标 ≥ 50%)。\n")
    else:
        diag = {
            "context_recall": "召回不足：检索未覆盖标准答案所需信息，可能是切片粒度或 top_k 问题",
            "context_precision": "精准不足：相关内容排序靠后，可考虑调整重排权重",
            "faithfulness": "存在幻觉：答案含未被上下文支撑的论断",
            "answer_relevancy": "答非所问：生成偏离了问题",
        }
        for rank, (worst, i, low, m) in enumerate(bad, 1):
            lines.append(f"### Bad Case {rank}（{cat_label.get(m['category'], m['category'])}）\n")
            lines.append(f"- **问题**：{m['question']}")
            lines.append(f"- **低分指标**：" +
                         "，".join(f"{METRIC_NAMES[k]}={fmt_pct(v)}" for k, v in low.items()))
            lines.append(f"- **答案(节选)**：{(m['answer'] or '')[:120]}...")
            lines.append(f"- **检索片段数**：{len(m['contexts'])}")
            worst_metric = min(low, key=low.get)
            lines.append(f"- **根因判断**：{diag.get(worst_metric, '需人工复核')}\n")

    # ── 下一轮优化建议 ──
    lines.append("## 五、下一轮优化建议\n")
    suggestions = gen_suggestions(results)
    for s in suggestions:
        lines.append(f"- {s}")
    lines.append("")

    # ── 成本 ──
    lines.append("## 六、评测成本\n")
    stats = get_tracker().get_session_summary()
    total_tok = stats.get("total_tokens", 0)
    total_cost = stats.get("total_cost", 0)
    n_q = sum(len(m) for m in meta_map.values())
    lines.append(f"- 累计 Token(A/B 检索生成阶段)：{total_tok}")
    lines.append(f"- 累计成本(A/B 检索生成阶段)：¥{total_cost}")
    lines.append(f"- 说明：RAGAS 打分调用经独立 LLM 包装，未纳入本地 token_tracker 统计")
    if isinstance(total_tok, (int, float)) and n_q:
        lines.append(f"- 单题平均 Token(检索生成)：{total_tok / n_q:.0f}")
    lines.append("")

    # ── 附录：测试集 ──
    lines.append("## 七、附录：本次测试集\n")
    lines.append("| # | 题型 | 问题 |")
    lines.append("|---|------|------|")
    for i, m in enumerate(next(iter(meta_map.values())), 1):
        lines.append(f"| {i} | {cat_label.get(m['category'], m['category'])} | {m['question']} |")
    lines.append("")

    return "\n".join(lines)


def gen_suggestions(results):
    """基于分层指标自动生成优化建议。"""
    focus = "optimized" if "optimized" in results else list(results.keys())[-1]
    by_cat = results[focus]["by_cat"]
    sug = []
    cat_label = {"simple_fact": "简单事实", "multi_hop": "多跳推理",
                 "cross_lingual": "跨语言", "fuzzy": "边缘模糊"}
    for cat, scores in by_cat.items():
        rec = scores.get("context_recall", 1.0)
        prec = scores.get("context_precision", 1.0)
        faith = scores.get("faithfulness", 1.0)
        if rec == rec and rec < 0.7:
            sug.append(f"{cat_label.get(cat, cat)}题召回率偏低({fmt_pct(rec)})，"
                       f"建议提高 TOP_K 或优化切片粒度"
                       + ("；跨语言可增强查询翻译的同义词扩展" if cat == "cross_lingual" else ""))
        if prec == prec and prec < 0.7:
            sug.append(f"{cat_label.get(cat, cat)}题精准率偏低({fmt_pct(prec)})，"
                       f"建议调整 BGE 重排权重或提高 RERANK_TOP_K 过滤强度")
        if faith == faith and faith < 0.7:
            sug.append(f"{cat_label.get(cat, cat)}题忠实度偏低({fmt_pct(faith)})，"
                       f"建议在 Prompt 中强化“仅依据上下文作答”的约束")
    # A/B 对比建议
    if "baseline" in results and "optimized" in results:
        b, o = results["baseline"]["overall"], results["optimized"]["overall"]
        rec_gain = o.get("context_recall", 0) - b.get("context_recall", 0)
        if rec_gain > 0.05:
            sug.append(f"双语+重排使整体召回率提升 {fmt_pct(rec_gain)}，验证优化有效，建议保留并继续调参")
    if not sug:
        sug.append("各项指标表现良好，可扩充测试集规模或引入更难的多跳题进一步压测")
    return sug


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="RAG 评测(RAGAS)")
    parser.add_argument("--config", choices=["baseline", "optimized", "both"],
                        default="both", help="评测哪些配置")
    parser.add_argument("--limit", type=int, default=None, help="只评测前 N 题(小样本快跑)")
    parser.add_argument("--save-baseline", action="store_true",
                        help="把 optimized 整体结果存为基线快照 baseline.json")
    args = parser.parse_args()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts_file = datetime.now().strftime("%Y%m%d_%H%M%S")

    testset = load_testset(args.limit)
    config_keys = ["baseline", "optimized"] if args.config == "both" else [args.config]

    print(f"📋 测试集 {len(testset)} 题，配置：{config_keys}\n", file=sys.stderr)

    results, meta_map = {}, {}
    for ck in config_keys:
        cfg = CONFIGS[ck]
        print(f"\n===== 评测配置：{cfg['label']} =====", file=sys.stderr)
        samples, meta = collect_samples(testset, cfg)
        print(f"   ⏳ RAGAS 打分中...", file=sys.stderr)
        df = run_ragas(samples)
        results[ck] = {
            "overall": summarize(df),
            "by_cat": summarize_by_category(df, meta),
            "df": df,
        }
        meta_map[ck] = meta
        print(f"   ✅ {cfg['label']} 完成：{results[ck]['overall']}", file=sys.stderr)

    # 生成报告
    os.makedirs(REPORTS_DIR, exist_ok=True)
    report = build_report(results, meta_map, args, ts)
    report_path = os.path.join(REPORTS_DIR, f"eval_report_{ts_file}.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"\n📄 报告已生成：{report_path}")

    # 基线快照
    if args.save_baseline and "optimized" in results:
        with open(BASELINE_PATH, "w", encoding="utf-8") as f:
            json.dump({"timestamp": ts, "overall": results["optimized"]["overall"]},
                      f, ensure_ascii=False, indent=2)
        print(f"📌 基线快照已保存：{BASELINE_PATH}")

    # 终端摘要
    print("\n===== 整体指标摘要 =====")
    for ck in config_keys:
        print(f"\n{CONFIGS[ck]['label']}:")
        for k, name in METRIC_NAMES.items():
            print(f"  {name}: {fmt_pct(results[ck]['overall'].get(k, float('nan')))}")


if __name__ == "__main__":
    main()
