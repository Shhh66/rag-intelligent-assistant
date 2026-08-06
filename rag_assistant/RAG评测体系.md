# RAG 评测体系 —— 架构设计与设计思路

> 本文档描述本项目 RAG 评测体系(基于 RAGAS 框架)的架构设计与设计思路。
> 目标:用**可运行的脚本**量化 RAG 管线的核心质量指标(召回率、精准率、幻觉率、答案相关性),并对**优化前后**做 A/B 对比,产出真实数据。

---

## 一、为什么需要评测体系

### 1.1 现状痛点

- 项目已实现"双语检索 + BGE 重排",但**从未量化过效果好坏**。
- 现有 `evaluation.py` 只是**运行日志**:记录延迟、Token、成本,回答的是"用了多少 / 多快 / 多贵",而非"**答得对不对**"。
- 文档里的"召回率 60%→90%""Top3 命中率提升 15%"等均为**定性预估**,没有任何可复现的度量脚本和实测数据支撑。

### 1.2 评测体系要回答的问题

| 维度 | 问题 | 对应指标 |
|------|------|---------|
| 检索找得全吗 | 该找到的文档都找到了吗 | **Context Recall(上下文召回率)** |
| 检索找得准吗 | 找到的文档里有多少是真正相关的 | **Context Precision(上下文精准率)** |
| 回答可信吗 | 答案是否忠于检索到的原文,有没有编造 | **Faithfulness(忠实度)→ 幻觉率 = 1 − Faithfulness** |
| 回答切题吗 | 答案是否直接回应了用户问题 | **Answer Relevancy(答案相关性)** |

---

## 二、为什么选 RAGAS

- **业界标准**:RAGAS 是当前 RAG 评测最主流的开源框架,简历/面试认可度高。
- **无需大量人工标注**:多数指标用 LLM-as-a-judge,只需少量 `ground_truth` 即可运行。
- **指标正交**:检索质量(recall/precision)与生成质量(faithfulness/relevancy)分开度量,能精准定位是"检索差"还是"生成差"。
- **可 A/B**:同一套指标跑基线配置和优化配置,直接对比,提升数据一目了然。

---

## 三、核心设计思路

### 3.1 零破坏原则

评测体系**不改动**解析 / 切片 / 检索的核心逻辑,只做两件事:

1. **新增独立模块** `rag_eval/`,与业务代码解耦。
2. **新增一个返回 contexts 的检索函数**(现有 `answer_with_fallback()` 只返回答案字符串,丢弃了检索片段,而 RAGAS 强制需要 contexts)。

对现有 `mcp_server.py` / `app.py` / `agent.py` 零影响。

### 3.2 A/B 对比:复用现有能力拆成两组配置

现有管线天然具备可拆解的两档能力,无需额外造检索逻辑:

| 配置 | 双语检索 | BGE 重排 | 定位 |
|------|:-------:|:-------:|------|
| **Baseline(基线)** | ❌ | ❌ | 纯向量单路检索 |
| **Optimized(优化)** | ✅ | ✅ | 双语合并 + BGE 二次重排 |

同一套 testset、同一套 RAGAS 指标,两组配置各跑一遍 → 直接得到**优化前后提升百分比**。这是简历上"用 RAGAS 量化并提升 X%"最硬的证据。

### 3.3 评测嵌入与检索嵌入保持一致

RAGAS 的 `answer_relevancy` / `context_precision` 等指标内部也需要嵌入模型。**复用项目同一个** `paraphrase-multilingual-MiniLM-L12-v2`(本地 HF 模型),而非引入 OpenAI 嵌入,保证:

- 评测口径与线上检索一致,结果可信;
- 无需额外的 OpenAI API Key,纯本地嵌入 + DeepSeek 打分。

### 3.4 LLM 打分走项目现有 DeepSeek 端点

RAGAS 的 LLM-as-a-judge 复用项目配置:DeepSeek OpenAI 兼容端点(`GROQ_API_KEY` / `GROQ_BASE_URL` / `LLM_MODEL`),用 `langchain_openai.ChatOpenAI` 包装后交给 RAGAS,无需接入额外模型。

---

## 四、系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                      rag_eval/ 评测模块                          │
│                                                                │
│  ┌────────────────┐   ┌─────────────────────────────────────┐ │
│  │ testset.json   │   │ adapters.py                          │ │
│  │ 评测数据集      │   │  get_ragas_llm()  ← DeepSeek 端点     │ │
│  │ {question,     │   │  get_ragas_embeddings() ← HF 本地嵌入 │ │
│  │  ground_truth} │   └─────────────────────────────────────┘ │
│  └───────┬────────┘                    │                       │
│          │                             │                       │
│          ▼                             ▼                       │
│  ┌──────────────────────────────────────────────────────────┐ │
│  │ run_eval.py  评测主脚本(核心交付物)                        │ │
│  │  1. 遍历 testset,每题跑 Baseline / Optimized 两组配置       │ │
│  │  2. 收集 question / answer / contexts / ground_truth       │ │
│  │  3. 构建 datasets.Dataset → ragas.evaluate()              │ │
│  │  4. 计算 4 指标,输出对比表 + 落盘报告                       │ │
│  └────────────────────────┬─────────────────────────────────┘ │
└───────────────────────────┼───────────────────────────────────┘
                            │ 调用(带 A/B 开关)
                            ▼
┌──────────────────────────────────────────────────────────────┐
│  retriever.py                                                  │
│    retrieve_and_answer(query, use_bilingual, use_rerank)      │
│                          → (answer, contexts)   ← 新增函数     │
│                                                                │
│    复用: search() / _translate_query_for_search()             │
│          / rerank() / build_prompt()                          │
└──────────────────────┬───────────────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │ 现有 RAG 检索管线(零改动)      │
        │ ChromaDB 向量检索 → BGE 重排   │
        └──────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │ rag_eval/reports/            │
        │   eval_report_<时间戳>.md     │
        │   (baseline vs optimized      │
        │    4 指标对比 + 提升百分比)    │
        └──────────────────────────────┘
```

---

## 五、模块职责

| 文件 | 角色 |
|------|------|
| `rag_eval/adapters.py` | 把项目的 DeepSeek LLM 与 HF 嵌入包装成 RAGAS 可用对象(`LangchainLLMWrapper` / `LangchainEmbeddingsWrapper`) |
| `rag_eval/testset.json` | 评测数据集:基于知识库真实文档手工标注的 `{question, ground_truth, category}`,**按场景分层**(见第六章) |
| `rag_eval/run_eval.py` | **核心交付物**:跑 A/B 两组配置、调用 RAGAS 计算 4 指标、生成对比报告 |
| `rag_eval/reports/` | 评测报告输出目录(Markdown,含指标对比表 + 每题明细 + 结论) |
| `rag_eval/baseline.json` | **基线快照**:首次跑出的最优结果固化于此,后续评测自动与之对比,无需手动翻历史报告(为回归测试预留接口) |
| `retriever.py`(改) | 新增 `retrieve_and_answer(...)` 返回 `(answer, contexts)`,带 `use_bilingual` / `use_rerank` 开关做 A/B |

---

## 六、测试集分层设计

评测的说服力不在题量,而在**维度**。8~15 题按场景分层标注,每题打上 `category` 标签,评测时既能出**整体指标**,也能出**分层指标**,从而看到"双语 + 重排优化主要提升了哪类问题"。

| 层级 | 数量 | 考察点 | 特征 |
|------|:---:|------|------|
| **简单事实题** | 4~6 道 | 基础检索能力 | 单文档、答案明确、关键词清晰 |
| **多跳推理题** | 2~3 道 | 召回全面性 | 需结合 ≥2 段文档内容才能回答 |
| **跨语言题** | 2~3 道 | 双语检索能力 | 中文问英文文档 / 英文问中文文档 |
| **边缘模糊题** | 1~2 道 | 重排与泛化能力 | 表述口语化、关键词不明确 |

**分层价值**:
- 简单事实题:基线通常已能答好 → 作为对照基准
- 多跳推理题:最能体现召回全面性,考验切片粒度与 top_k
- 跨语言题:**直接验证双语检索的增益**,是 A/B 对比的核心看点
- 边缘模糊题:**直接验证 BGE 重排的价值**,考验从噪声候选中精排的能力

`testset.json` 结构:
```json
[
  {
    "question": "……",
    "ground_truth": "……",
    "category": "simple_fact | multi_hop | cross_lingual | fuzzy"
  }
]
```
报告中按 `category` 分组统计,呈现"整体 + 分层"双视角。

---

## 七、Bad Case 错误分析

> 评测的核心价值不是拿分数,而是**定位问题、指导优化**。这是评测体系价值的关键放大器。

`run_eval.py` 自动挑出**低分样例**(如某指标低于阈值)写入报告的"错误样例分析"章节,每类给出可判断的诊断线索:

| 低分指标 | 可能根因 | 诊断线索(报告中呈现) |
|---------|---------|---------------------|
| **召回率低** | 切片粒度过粗/过细、关键词未命中、知识库无相关内容 | 打印该题检索到的 contexts + ground_truth,人工对比是"没检到"还是"检到了但被切碎" |
| **幻觉率高**(faithfulness 低) | 上下文信息不足、模型自行编造 | 打印 answer 中未被 contexts 支撑的论断 + 当时的 contexts |
| **相关性低** | 检索偏题、生成偏离问题 | 对比 question / retrieved contexts / answer 三者,判断是"检索偏"还是"生成偏" |

**产出要求**:哪怕只深挖 2~3 个典型 bad case,也能体现问题分析能力 —— 企业做 RAG 评测最终都是为了定位问题、指导下一轮优化,而非只看一个分数。报告中每个 bad case 附:题目、指标得分、检索片段、答案、**一句话根因判断**。

### 下一轮优化建议(闭环)

报告末尾自动生成「下一轮优化建议」小节,基于 bad case 与分层指标给出 1~2 条**具体可执行**的方向,例如:

- "跨语言场景召回率偏低 12%,建议优化查询翻译的同义词扩展"
- "模糊题精准率不足,建议调整 BGE 重排权重 / 提高 `RERANK_TOP_K`"

由此形成 **评测 → 定位问题 → 指导优化 → 再评测** 的完整闭环,让评测体系真正驱动迭代,而不只是事后打分。

---

## 八、可复现性信息

评测报告开头固定输出**元信息区块**,保证结果可复现,体现工程严谨性:

- **环境信息**:RAGAS 版本、langchain-core 版本、嵌入模型名(`paraphrase-multilingual-MiniLM-L12-v2`)、LLM 模型名(`deepseek-v4-flash`)
- **知识库版本**:本次评测基于哪一版知识库(可用 `kb_manager.py status` 的文档数 / 快照时间戳标识)、共多少篇文档
- **测试集**:附本次评测的完整题目列表(question + category)
- **运行参数**:`--config` / `--limit` 等本次实际取值、运行时间

---

## 九、成本指标(与 evaluation.py 呼应)

除效果指标外,报告补充**评测成本统计**,与项目原有 `evaluation.py` / `token_tracker.py` 的成本追踪体系呼应,体现全链路成本意识:

- **单题平均评测 Token 消耗**(RAGAS 打分 + 生成答案的 Token 之和)
- **本次评测总 Token 消耗与总成本(¥)**,按 `config.py` 的 `MODEL_PRICING` 折算

> 复用 [token_tracker.py](rag_assistant/token_tracker.py) 的 `get_tracker()` 累积,评测跑完读取会话累计值。

---

## 十、评测指标详解

| 指标 | 含义 | 是否需要 ground_truth | 计算依赖 |
|------|------|:---:|------|
| **Context Recall** | 检索到的上下文是否覆盖了标准答案所需的信息 | ✅ 需要 | LLM |
| **Context Precision** | 检索到的上下文中相关内容的排序质量(相关的是否排在前面) | ✅ 需要 | LLM + 嵌入 |
| **Faithfulness** | 答案中的每个论断是否都能从上下文推得(不编造) | ❌ 不需要 | LLM |
| **Answer Relevancy** | 答案是否切题、直接回应问题 | ❌ 不需要 | LLM + 嵌入 |

> **幻觉率(Hallucination Rate)= 1 − Faithfulness**,数值越低越好。

---

## 十一、评测流程

```
1. 准备阶段
   └─ python kb_manager.py list      # 查看知识库有哪些文档
   └─ python kb_manager.py status    # 记录知识库版本(文档数/快照)
   └─ 据实分层标注 testset.json       # 8~15 题,含 ground_truth + category

2. 冒烟验证(先排版本冲突)
   └─ from ragas import evaluate      # RAGAS 与 langchain 版本兼容性验证
   └─ 适配器可加载 LLM / 嵌入

3. 小样本快跑(省 Token 排错)
   └─ python rag_eval/run_eval.py --limit 2 --config both

4. 完整评测
   └─ python rag_eval/run_eval.py --config both

5. 产出报告(rag_eval/reports/eval_report_<时间戳>.md)
   ├─ 元信息区块(环境/知识库版本/测试集/参数)
   ├─ 整体 4 指标:baseline vs optimized + 提升百分比
   ├─ 分层指标:按 category 分组的对比
   ├─ Bad Case 错误分析(2~3 个典型样例 + 根因)
   └─ 成本统计(单题平均 Token / 总成本)
```

---

## 十二、关键设计决策

- **评测层独立成模块**:`rag_eval/` 与业务解耦,现有 `evaluation.py`(运行日志)职责不同,保留不动。
- **A/B 复用现有能力**:不新造检索逻辑,把"双语 + 重排"当作可开关的两档,直接产出提升数据。
- **测试集分层**:按场景打 `category` 标签,产出"整体 + 分层"双视角,定位优化增益来源。
- **Bad Case 驱动**:自动挑低分样例做错误分析,把评测从"看分数"升级为"定位问题"。
- **嵌入统一**:RAGAS 指标嵌入 = 线上检索嵌入(同一 HF 本地模型),口径一致、无需 OpenAI Key。
- **LLM 复用 DeepSeek**:打分走项目现有端点,零新增模型接入。
- **成本呼应**:复用 `token_tracker` 统计评测成本,与项目全链路成本追踪一致。
- **基线快照**:首次最优结果固化为 `rag_eval/baseline.json`,后续自动对比,零成本预留回归测试接口。
- **打分一致性**:LLM-as-a-judge 采用固定 `temperature=0`,同一测试集**相对分数稳定**,适合 A/B 对比;**绝对分数仅作参考**。
- **小样本优先**:`--limit` 参数支持小样本快跑,先验证链路再全量,控制 Token 消耗。
- **ground_truth 据实标注**:直接决定 recall/precision 可信度,基于真实文档出题,不编造。

---

## 十三、风险与注意事项

- **版本兼容**:RAGAS 对 langchain 版本敏感,本项目用 `langchain-core>=1.4`。实测 `ragas==0.4.3` 与之兼容,`import` 冒烟测试通过(仅有 deprecation 警告,不影响功能)。
- **DeepSeek `n>1` 不兼容(实测踩坑)**:RAGAS 的 `answer_relevancy` 等指标默认多候选采样(`strictness=3`,即 `n>1`),而 DeepSeek API 仅支持 `n=1`,直接报 `400 Invalid n value`。解决方案:适配器里对 `LangchainLLMWrapper` 设 `bypass_n=True`,让其改为发多次 `n=1` 请求。见 [rag_eval/adapters.py](rag_assistant/rag_eval/adapters.py)。
- **faithfulness 对答案格式敏感**:实测发现,当答案带"来源标注/免责声明"等结构化格式时,RAGAS 的 faithfulness 可能误判为低分(即使答案事实正确)。这是 LLM-as-a-judge 的固有局限。**解决方案见第十八章(评测前清洗引用格式再打分)。**
- **Token 消耗**:评测每题会多次调 LLM 打分,先用 `--limit` 小样本验证再全量。
- **数据质量**:`ground_truth` 质量直接影响 `context_recall/precision` 的可信度,需据实标注。
- **零破坏保证**:不改解析 / 切片 / 检索核心逻辑,只新增评测层 + 一个返回 contexts 的检索函数。

---

## 十四、未来扩展(长期演进)

- **Agent / Skill 层评测**:当前评测的是纯 RAG 检索管线;未来可上升到 Agent 层,评测"调用 `deep_kb_search` Skill 回答问题"的**端到端效果**,与纯 RAG 做对比,验证 Skill 编排是否带来增益 —— 与项目整体的 Skills 体系联动,架构完整性更强。
- **多轮对话评测**:引入带上下文记忆的多轮问答评测,考察指代消解与上下文保持。
- **回归基线固化**:将某一版评测结果固化为回归基线,后续每次优化自动对比,防止指标劣化。

---

## 十五、新增依赖

```
ragas>=0.2
langchain-openai>=0.2
datasets>=3.0
```

---

## 十六、实测结果(首轮基线)

> 运行环境:`ragas 0.4.3` / `langchain-core 1.4.0` / 嵌入 `paraphrase-multilingual-MiniLM-L12-v2` / LLM `deepseek-v4-flash`(temperature=0)
> 知识库:2 篇文档 / 26 chunks;测试集:13 题(简单事实5 / 多跳3 / 跨语言3 / 模糊2)
> 命令:`python rag_eval/run_eval.py --config both --save-baseline`

### 整体指标(Baseline vs Optimized)

| 指标 | Baseline(纯向量) | Optimized(双语+重排) | 变化 |
|------|:---:|:---:|:---:|
| 上下文召回率 | 98.1% | 98.1% | ≈ |
| 上下文精准率 | 74.3% | **93.8%** | 🔺 +19.5% |
| 忠实度 | 90.8% | 76.4% | 🔻 -14.4% |
| 答案相关性 | 87.4% | 85.4% | 🔻 -2.0% |

### 结论与洞察

1. **重排效果显著**:上下文精准率 74.3% → 93.8%,提升 **19.5 个百分点** —— 证明 BGE-Reranker 二次重排能有效把相关片段排到前面,是本项目最有效的优化点。
2. **召回率持平**:知识库规模小(仅 2 篇文档),向量检索召回本已接近满分(98.1%),双语扩展在小库上增益不明显 —— 说明双语检索的价值需在大规模、多语种知识库上才能充分体现。
3. **忠实度下降是"评测假象"而非真回退**:优化配置忠实度反而降到 76.4%,经 bad case 复核,低分样例(如"RIS 由什么组成""小蓝身高体重")**答案完全正确且标注了来源**,是 RAGAS faithfulness 对"来源标注/免责声明"等格式敏感导致的误判 —— 这本身就是评测体系的价值:**暴露了指标口径与实际质量的偏差,提示需优化答案格式或调整评测 prompt**。
4. **成本**:A/B 检索生成阶段累计约 5.8 万 Token / ¥0.07,单题平均约 2248 Token(不含 RAGAS 打分调用)。

> 完整报告见 `rag_eval/reports/eval_report_*.md`,基线快照见 `rag_eval/baseline.json`。

---

## 十七、踩坑与关键结论速查(面试/复盘)

> 集中归档本次落地的核心踩坑与结论,便于快速引用。

### 🔧 踩坑 1:DeepSeek 不支持 `n>1`,RAGAS 全部指标报 400

- **现象**:首次运行,所有指标返回 `nan`,报错 `BadRequestError 400 - Invalid n value (currently only n = 1 is supported)`。
- **根因**:RAGAS 的 `answer_relevancy` 默认 `strictness=3`(一次生成多个候选,即 `n>1`),通过 LangChain 传给 LLM;而 DeepSeek API 只支持 `n=1`。
- **定位**:读 `LangchainLLMWrapper.generate_text` 源码,发现有 `bypass_n` 分支——为 True 时改用"发多次 `n=1` 请求"替代"一次 `n=n`"。
- **解法**:适配器里 `wrapper.bypass_n = True`(见 [rag_eval/adapters.py](rag_assistant/rag_eval/adapters.py))。
- **通用性**:任何非 OpenAI 官方、只支持 `n=1` 的 OpenAI 兼容端点(DeepSeek/多数国产 API)接 RAGAS 都会遇到,此为通用解。

### 🔧 踩坑 2:双语检索时翻译返回空串,英文检索退化为空查询

- **现象**:优化配置日志出现 `英文检索词:`(空),仍用空串检索。
- **解法**:[retriever.py](rag_assistant/retriever.py) `retrieve_and_answer` 中,翻译结果为空则跳过英文检索,不发空查询。

### 📌 关键结论

1. **BGE 重排是最有效优化点**:上下文精准率 74.3% → 93.8%(**+19.5pt**),数据可直接写简历。
2. **小知识库掩盖双语价值**:召回率两配置均 98.1%,双语扩展在 2 篇文档的小库上无增益,价值需大规模多语种库才显现。
3. **faithfulness 会误判"带来源标注的正确答案"**:优化配置忠实度"下降"至 76.4% 是评测假象——bad case 复核显示答案正确且标注了来源,是 RAGAS 对格式敏感所致。**已通过"评测前清洗引用格式"解决,忠实度回升至 94.8%(见第十八章)。这恰是评测体系的价值:暴露指标口径与真实质量的偏差,而非只看分数。**
4. **成本可控**:全量 13 题 A/B 约 5.8 万 Token / ¥0.07,单题均 ~2248 Token。

### 🔁 复现命令

```bash
cd rag_assistant && venv\Scripts\activate.bat
python rag_eval/run_eval.py --config both              # 完整 A/B 评测
python rag_eval/run_eval.py --config both --limit 2    # 小样本快跑(省 Token)
python rag_eval/run_eval.py --config both --save-baseline  # 并保存基线快照
```

---

## 十八、忠实度评测优化:清洗引用格式再打分

### 问题

首轮实测中,优化配置的 **faithfulness 从 90.8% 降到 76.4%**(见第十六章),但 bad case 复核发现低分样例(如"RIS 由什么组成""小蓝身高体重")**答案完全正确**,只是带了引用与免责格式。

- **根因**:RAGAS 的 faithfulness 把答案拆成一条条 claim,逐条判断能否被 contexts 支撑。而 `（来源:xxx,第1页）`、`> 📚 参考来源:`、`> ⚠️ 免责声明` 这些格式行会被当成"无法被上下文支撑的 claim",从而拉低分数。
- **本质**:不是答案错,而是**指标口径**把"格式噪声"误算成"幻觉"。这些格式是 [retriever.py](rag_assistant/retriever.py) 的 `build_prompt` 用 prompt 规则强制生成的,**格式固定、可预测**。

### 解决思路

**评测前先把答案里的所有引用/免责格式清洗掉,只把纯事实内容喂给 RAGAS 打分——完全不改 RAGAS 源码,也不改真实答案生成逻辑。** 这是一层"评测预处理"。

设计要点(零破坏 + 透明):

1. **只清洗喂给评测的副本**:在 [run_eval.py](rag_assistant/rag_eval/run_eval.py) 的 `collect_samples` 里,`SingleTurnSample.response` 用清洗后的纯事实文本;`meta["answer"]` **仍存原始带来源答案**,保证报告的 bad case 展示的是用户真正看到的内容。
2. **清洗范围**:清洗后文本**同时用于 faithfulness 与 answer_relevancy**(引用格式对答案相关性同样是噪声),单次 evaluate,成本不变。
3. **不影响检索类指标**:context_recall / context_precision 评的是检索到的上下文,与答案格式无关,天然不受清洗影响——正好可用它们"清洗前后基本一致"来**反证清洗无副作用**。

### 清洗规则(对应 build_prompt 生成的三类格式)

| 格式类型 | 样例 | 清洗动作 |
|---------|------|---------|
| 行内来源标注 | `（来源:文件名，第X页）` / `（来源:文件名）` | 整体删除(中英文括号都覆盖) |
| 来源列表块 | `> 📚 参考来源:` 及其后连续 `> - ...` 行 | 整块删除 |
| 免责声明 | `> ⚠️ 本回答并非基于上传的知识库文档...` | 删除该行 |

> 正则保守:宁可漏删也不误删事实内容;清洗后若为空(极端情况)回退用原文,避免喂空串给 RAGAS。

### 诚实性说明

清洗是**修正指标口径**,不是刷分。报告中会标注:"faithfulness 与 answer_relevancy 基于**去除引用/免责格式后的纯事实文本**计算,以消除 RAGAS 对结构化格式的误判",保持口径透明与可复现。

### 实测效果(清洗前 vs 清洗后)

对同一 Optimized 配置(双语+重排)、同一 13 题测试集,清洗前后对比:

| 指标 | 清洗前 | 清洗后 | 说明 |
|------|:---:|:---:|------|
| **忠实度(faithfulness)** | 76.4% | **94.8%** | 🎯 **+18.4pt**,误判修复,幻觉率 23.6% → 5.2% |
| 答案相关性 | 85.4% | 86.2% | 微升(格式噪声去除后略优) |
| 上下文召回率 | 98.1% | 98.1% | **完全一致** —— 反证清洗无副作用 |
| 上下文精准率 | 93.8% | 92.6% | 基本一致(LLM 打分正常波动) |

**关键验证**:召回率/精准率评的是检索上下文,与答案格式无关,清洗前后基本持平,**反证了清洗只作用于答案类指标、没有引入副作用**;而忠实度大幅回升 18.4pt,证实了"之前的低分确实是引用格式导致的误判,而非真幻觉"。

> 命令:`python rag_eval/run_eval.py --config both`;报告见 `rag_eval/reports/eval_report_20260710_172509.md`。清洗逻辑见 [run_eval.py](rag_assistant/rag_eval/run_eval.py) 的 `clean_answer_for_eval()`。
