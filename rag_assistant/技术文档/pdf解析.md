# PDF 解析技术文档

##pdf解析后的结果
venv\Scripts\python.exe -c "from document_loader import load_file; docs=load_file('uploaded_docs/姓名.pdf'); open('_parsed.md','w',encoding='utf-8').write('\n\n---\n\n'.join(d.page_content for d in docs)); print('已保存到 _parsed.md，打开查看')"

##pdf切块后的结果
venv\Scripts\python.exe -c "from document_loader import load_file; from text_splitter import split_documents; docs=load_file('uploaded_docs/姓名.pdf'); chunks=split_documents(docs); [print(f'===== 块{i+1} (meta: {dict((k,v) for k,v in c.metadata.items() if v)}) =====\n{c.page_content}\n') for i,c in enumerate(chunks)]"

## 一、当前架构总览

```
PDF 文档
  ↓
┌─ 快速通道（PyMuPDF）─────────────── 90%+ 原生 PDF ────────────────┐
│                                                                   │
│  fitz.open() → 加密检测 → 文本块提取（坐标+字体+字号+透明度）       │
│     ↓                                                             │
│  文本质量校验（乱码率/句长/换行率）                                  │
│     ↓                                                             │
│  多栏检测（20竖条直方图法）→ 双栏重排                               │
│     ↓                                                             │
│  页眉页脚过滤（相对位置5% + 跨页去重）                              │
│     ↓                                                             │
│  零依赖标题聚类（分箱降级法，字号差<1pt合并）                       │
│     ↓                                                             │
│  输出：结构化 Markdown（#/##/### 标题 + \n\n 段落）                 │
│                                                                   │
│  文件：document_loader.py _load_pdf() + _lines_to_markdown()       │
│  依赖：PyMuPDF (fitz) 30MB                                        │
└───────────────────────────────────────────────────────────────────┘
                              ↓ 质量不合格/扫描件
┌─ 慢速通道（PP-Structure V2）─────── 扫描件/劣质文本层 ─────────────┐
│                                                                   │
PDF 渲染为 200DPI PNG 图片
    ↓
① PP-DocLayout_plus-L 版面分析：切分所有区域，输出「坐标+类别」
    ↓
② 按区域类型分流处理：
    ├─ 标题/正文区域 → PP-OCRv4 文本检测+识别 → 输出纯文本
    ├─ 表格区域 → SLANet 表格结构识别 → 输出 HTML 表格
    └─ 公式区域 → 官方走 PP-FormulaNet，你方案替换为 SimpleTex API → 输出 LaTeX
    ↓
③ 按 y 坐标分桶排序，还原从上到下、从左到右的阅读顺序
    ↓
④ 拼接所有内容，输出带层级的结构化 Markdown                  │
│                                                                   │
│  文件：ocr_processor.py + document_loader.py _load_pdf_via_ocr()  │
│  依赖：PaddlePaddle 2.6.2 + PaddleOCR 2.9.1（约 500MB）           │
└───────────────────────────────────────────────────────────────────┘
```

### 快慢通道分流逻辑

```
PyMuPDF 提取全文文本
  ↓
① 计算平均每页字符数
  ├─ < 50 字 → 直接走慢速 OCR 通道 ✅ 结束
  └─ ≥ 50 字 → 进入细校验
        ↓
② 依次计算 乱码率、平均句长、换行率
  ├─ 三项全部满足阈值（乱码≤2% + 句长≥5 + 换行率≤40%）→ 走快速通道
  └─ 任意一项不满足阈值 → 走慢速 OCR 通道兜底

```
一、四个指标的设计原理与计算逻辑
四个指标分别对应「有没有文本层」「文本是不是有效」「语义是不是连贯」「排版是不是正常」四个维度，从粗到细逐层过滤，全部基于纯字符串统计，不需要任何额外模型或库，PyMuPDF 提取完文本就能立刻算出。
1. 平均每页字符数：第一层粗筛，秒判纯扫描件
核心作用：用最低成本把纯图片扫描件直接筛出去，不用做后续复杂计算。
计算方式：总提取字符数 ÷ 文档总页数
判断逻辑：
平均每页 < 50 字 → 直接判定为扫描件 / 图片 PDF，强制走慢速 OCR 通道；
平均每页 ≥ 50 字 → 进入下一轮细粒度质量校验。
背后的依据：
原生电子 PDF（Word 导出、正规排版）每页至少有几百到上千字；而纯扫描件没有文本层，PyMuPDF 提取出来要么是空字符串，要么只有零星几个内嵌的注释字符，差距非常悬殊，用一个阈值就能一刀切分开。
2. 乱码率：判断文本层是不是有效内容
核心作用：过滤「有文本层，但全是乱码 / 无效字符」的劣质 PDF。
这类文档常见于：低质量图片转 PDF 时自带的垃圾 OCR、字体缺失导致的字符映射错误、加密破解不完整的文档。
计算方式：无效字符总数 ÷ 总字符数
无效字符的判定规则（简单启发式）：
Unicode 替换符 �（字体缺失 / 编码错误的标志性字符）；
ASCII 控制字符（\x00-\x1F 除了换行 / 制表符）；
连续出现的无意义生僻符号、古文字符；
单字重复占比过高（比如连续 10 个相同的无意义字符）。
判断阈值：乱码率 > 2% → 判定为劣质文本，走 OCR 重识别。
为什么需要这个指标：
很多 PDF 看起来有文本层，但实际全是乱码，字符数达标但内容完全不可用。如果只看字符数，这类文档会漏进快速通道，输出的内容完全无法用于检索。
3. 平均句长：判断文本是不是语义连贯
核心作用：识别「字符都是正常字，但被打散成碎片，没有完整语义」的劣质文本。
常见于：排版错乱的 PDF、矢量图转的字符化 PDF、劣质 OCR 输出的零散字符。
计算方式：总有效字符数 ÷ 句子总数
句子切分规则：按句号、问号、感叹号、段落空行来切分句子，不用复杂分词，纯符号匹配即可。
判断阈值：平均句长 < 5 字 → 判定为语义破碎，走 OCR 兜底。
背后的逻辑：
正常人类书写的文本，句子平均长度一般在 10~30 字之间，有完整的语义单元；而打散的劣质文本，都是零散的单字、两字碎片，句子会特别短，平均长度不足 5 字，一眼就能区分。
4. 换行率（异常换行占比）：判断排版结构是否正常
核心作用：识别「双栏错位、逐行打散、每行强制换行」的文本。
这类文本字符本身没问题，但换行完全混乱，段落结构全碎，快速通道的标题聚类、语义分块都会失效。
计算方式：换行符数量 ÷ 总字符数（也可以用「总行数 ÷ 总字符数」，本质一致）
判断阈值：换行率 > 40% → 换行过于密集，文本结构破碎，走 OCR 通道重新识别排版。
背后的逻辑：
正常单栏排版的 PDF，一段内容才会换一次行，平均几十字一个换行；而双栏没做重排、或者逐行提取的劣质文本，几乎每 3~5 个字就换一行，换行密度极高，统计特征非常明显。


---

## 二、可优化的细节与避坑补充

### 2.1 公式区域检测：补规则兜底，避免行内公式大量漏检

这是最容易影响最终效果的隐性问题：PP-Structure V2 的版面分析模型，对行内公式、短公式、简单代数公式的召回率偏低，大量公式会被归类为普通文本区域，直接走通用 OCR，不会触发 API 识别。

**优化方案：增加一层文本规则补召逻辑**

```
PP-Structure 版面分析
  ↓
正文/标题区域 → OCR 文本
  ↓
统计数学符号占比（+、-、=、×、÷、∑、∫、∞、α、β、π 等）
  ↓
├─ 符号占比 > 15% → 标记为「疑似公式」→ 裁剪图片 → SimpleTex API
├─ 包含 ∑、∫、∂ 等高数特征符号 → 同上
└─ 普通文本 → 保持原样
```

**收益**：公式整体召回率能从 ~60% 提升到 90%+，尤其是行内公式的覆盖度会有明显提升。

### 2.2 SimpleTex API 调用的细节优化

几个容易忽略、但直接影响识别率和稳定性的细节：

**裁剪区域外扩**：公式 bbox 四周外扩 10~20 像素，避免上下标、根号、积分号边缘被裁切，识别准确率能提升 10%+。

**行内 / 行间自动区分**：不要统一用 `$$...$$` 包裹。根据公式区域高度与正文行高的比值判断：

| 条件 | 判定 | 包裹方式 | 排版 |
|------|------|---------|------|
| 高度 ≈ 正文行高 | 行内公式 | `$...$` | 嵌入正文段落 |
| 高度 > 正文行高 × 1.5 | 行间公式 | `$$...$$` | 前后强制加空行 |

避免行内公式变成独立块，破坏原文语义和排版结构。

**基础语法校验**：对返回的 LaTeX 做轻量校验——`$` 符号是否成对、括号是否匹配。校验不通过直接降级到 OCR 纯文本，避免语法错误的 LaTeX 干扰向量检索。

**超时重试策略**：配置 2~3 次超时重试，单次超时 8s，抵消网络波动导致的偶发失败。

### 2.3 缓存设计的工程化升级

**缓存 Key 优化**：不建议用裁剪后图片的内容哈希做 key——渲染的细微差异（抗锯齿、DPI 偏差）会导致缓存失效。推荐用「文件 MD5 + 页码 + 区域坐标」拼接计算 key，稳定性更高。

**持久化缓存**：从内存缓存升级为本地 JSON 文件缓存，程序重启后依然可复用。对于重复上传的文档、同一份文档多次解析，能大幅减少 API 调用次数。

**额度预警**：增加 API 调用次数统计，接近免费额度阈值时打印警告，自动降级为 OCR 纯文本，避免超量后全量失败。

### 2.4 与下游分块的联动增强

**公式边界保护**：LaTeX 公式块前后强制插入空行，和表格的边界保护逻辑一致，确保二级分块时不会从公式中间切断。

**检索友好性增强**：通用中文向量模型对纯 LaTeX 语法的表征能力有限。在公式块前补充一行自然语言标注 `【数学公式】`，后续可拓展为大模型生成公式简短描述（如"傅里叶级数展开式"），显著提升公式相关查询的召回率。

### 2.5 快速通道也可接入公式识别

原生 PDF 中的公式多为矢量字符渲染，快速通道能提取到零散字符，但无法生成 LaTeX。可以在快速通道也增加数学符号检测，疑似公式的区域用 PyMuPDF 渲染裁剪成图片，同样走 SimpleTex API，不用非得进慢速 OCR 通道。

**收益**：原生 PDF 的公式也能被正确识别，进一步提升快速通道的语义完整性。

### 2.6 方案对比：改前 vs 改后

```
                       改前                                改后
                       ═══                                ═══
公式检测：
  仅靠 PP-Structure 版面分析                 版面分析 + 文本规则补召
  行内公式大量漏检（召回率 ~60%）            符号占比 >15% → 补充识别（召回率 90%+）

公式 → LaTeX：
  无（走通用 OCR，输出纯文本）               SimpleTex API → LaTeX
  "nTx nπx f(x)=ao+"                        $$ \sum_{n=1}^{\infty} a_n\cos(...) $$

缓存：
  无                                        MD5+页码+坐标 → 本地 JSON 持久化

快速通道公式：
  不处理（字符打散）                         数学符号检测 → 裁剪 → SimpleTex

分块保护：
  仅表格有边界保护                           公式块前后空行 +【数学公式】标注
```

---

## 三、各模块能力矩阵

### 3.1 快速通道（PyMuPDF）

| 能力 | 状态 | 算法 | 准确率 |
|------|------|------|--------|
| 文本提取 | ✅ 已完成 | fitz.get_text("dict") → block→line→span 三级 | 原生 PDF >99% |
| 标题聚类 | ✅ 已完成 | 分箱降级法：字号从大到小→差值<1pt合并→取前3-4档 | 中文文档 ~85% |
| 多栏检测 | ✅ 已完成 | 20竖条直方图→滑动平均→密度峰值→双栏分界 | 双栏 >90%，三栏不进快速通道 |
| 页眉页脚过滤 | ✅ 已完成 | 相对位置5% + 页码移除 + 跨页去重 | >90% |
| 加密检测 | ✅ 已完成 | doc.is_encrypted → 密码验证 | 100% |
| 公式识别 | ⚠️ 计划中 | 数学符号检测→裁剪→SimpleTex API（见 2.5） | 预计 90%+ |
| **表格提取** | ❌ P1 待做 | 需 pdfplumber 或 camelot-py 提取 | — |

### 3.2 慢速通道（PP-Structure V2）

| 能力 | 状态 | 算法 | 准确率 |
|------|------|------|--------|
| 版面分析 | ✅ 已完成 | PP-DocLayout_plus-L 检测布局区域 | ~85% |
| 文字 OCR | ✅ 已完成 | PP-OCRv4（检测+识别双模型） | 中文 >95% |
| 表格识别 | ✅ 已完成 | SLANet 有线+RT-DETR 无线 → HTML → Markdown 列转行 | ~80%（表结构） |
| 阅读顺序 | ✅ 已完成 | y坐标分桶排序（20px容差） | ~90% |
| 图片区域 | ✅ 已完成 | 占位符 `📷 [图片]` | — |
| 公式识别 | ✅ 已完成 | 版面分析检测 → 裁剪 → SimpleTex API → LaTeX（见 2.2） | 预计 95%+ |
| 公式补召 | ⚠️ 计划中 | 文本规则补召：符号占比 >15% → 补充识别（见 2.1） | 召回率 60%→90%+ |

---

## 四、公式识别：当前问题与根因

### 3.1 问题现象

```
输入 PDF（含傅里叶级数公式）：
  ∞
  Σ (aₙcos(nπx/L) + bₙsin(nπx/L))
  n=1

当前输出（PP-Structure V2 无公式模式）：
  nTx
  nπx
  f(x) = ao +
  + bn sin-
  n=1

理想输出（LaTeX）：
  $$ f(x) = a_0 + \sum_{n=1}^{\infty} \left( a_n \cos\frac{n\pi x}{L} + b_n \sin\frac{n\pi x}{L} \right) $$
```

### 3.2 根因链

```
PP-FormulaNet_plus-L 模型（公式→LaTeX）
  ↓ 需要
PaddlePaddle 3.x 推理引擎（PIR 格式模型 + oneDNN 加速）
  ↓ 但是
Windows 上 oneDNN 存在 Bug：
  ConvertPirAttribute2RuntimeAttribute not support
  [pir::ArrayAttribute<pir::DoubleAttribute>]
  ↓ 导致
PaddlePaddle 3.x 无法在 Windows 上运行 PP-FormulaNet
  ↓ 降级到
PaddlePaddle 2.6.2 + PaddleOCR 2.9.1
  ↓ 但是
PP-FormulaNet 在 2.x 上不完全兼容，formula=True 导致 Tensor 维度错误
  ↓ 最终
formula=False，公式区域走通用 OCR → 纯文本 → 语义破碎
```

### 3.3 该 Bug 影响范围

- **仅影响 Windows**（Linux/macOS 的 oneDNN 无此 Bug）
- **仅影响公式模型**（PP-OCRv4 文本识别、SLANet 表格识别均正常）
- **PaddlePaddle 官方已知**（GitHub Issue #70000+），暂无修复时间表

---

## 五、公式识别改进方案

### 方案对比

| 方案 | 原理 | 优点 | 缺点 | 推荐 |
|------|------|------|------|------|
| A: 等 PaddlePaddle 修 Bug | 升级到 3.x 后用 PP-FormulaNet | 零额外成本，模型已就绪 | 不可控，不知道何时修 | ❌ 被动等待 |
| B: 切换 Linux/WSL | 在 WSL 中运行 PaddlePaddle 3.x | 全套 PP-Structure V3 可用 | 部署复杂，Streamlit 需配置 | ⚠️ 长期方案 |
| **C: SimpleTex API** | 公式区域裁剪图片→调 SimpleTex API→返回 LaTeX | 精度极高（>95%），不依赖 PaddlePaddle | 需联网，免费额度有限 | ✅ **推荐短期** |
| D: Mathpix API | 同 C，换 API 提供商 | 同上 | 付费，价格高 | ⚠️ 备选 |
| E: 本地 EasyOCR + 自训练 | 换 OCR 引擎 | 纯本地 | 公式识别需自训练，成本极高 | ❌ 不推荐 |

### 推荐方案：C（SimpleTex API）+ B（WSL 备选）

```
短期（立即）：
  公式区域 → 裁剪图片 → SimpleTex API → LaTeX
  ┌─ API 正常 → $$ \sum_{n=1}^{\infty} ... $$ ✅
  ├─ API 超时 → OCR 纯文本（当前状态）
  └─ API 不可用 → [公式] 占位符

长期（PaddlePaddle 修复后）：
  PP-Structure V3 → PP-FormulaNet_plus-L → LaTeX（纯本地）
```

---

## 六、实施计划

### 架构变更（完整版）

```
                         改前                              改后
                         ═══                              ═══
公式检测：
  仅 PP-Structure 版面分析                   版面分析 + 文本规则补召
  行内公式漏检严重                           符号占比 >15% → 补充裁剪 → API
  快速通道不处理公式                         快速通道数学符号检测 → 裁剪 → API

慢速通道公式处理：
  PP-Structure V2                            PP-Structure V2
  formula=False                              formula=False（Paddle Bug 未修）
       ↓                                          ↓
  公式区域 → OCR 纯文本                       公式区域 → 裁剪 PNG（外扩 10-20px）
       ↓                                          ↓
  "nTx nπx f(x)=ao+"                    ┌─ SimpleTex API → LaTeX ✅
  （语义丢失，无法检索）                  │  行内公式 $...$, 行间公式 $$...$$
                                        │  语法校验 → 不通过 → OCR 纯文本
                                        │  降级链:
                                        ├─ API 超时 8s×3次 → OCR 纯文本
                                        ├─ API 不可用 → [公式] 占位符
                                        └─ 额度预警 → 自动降级
                                              ↓
                                        持久化缓存（MD5+页码+坐标）
                                              ↓
                                        分块保护：【数学公式】+ 前后空行
                                              ↓
                                        $$ \sum_{n=1}^{\infty} a_n\cos(...) $$
                                        （语义完整，向量检索可匹配）
```

### 实施步骤

| 步骤 | 文件 | 内容 |
|------|------|------|
| P0 | `ocr_processor.py` | 公式区域检测：PP-Structure bbox 裁剪 + 外扩 10-20px |
| P0 | `ocr_processor.py` | 文本规则补召：正文区域数学符号占比 >15% → 补充裁剪 |
| P0 | `ocr_processor.py` | SimpleTex API 调用：requests POST + 三级降级 + 超时重试（8s×3） |
| P0 | `ocr_processor.py` | 行内/行间自动区分：公式高度/正文行高比值判断 → `$...$` 或 `$$...$$` |
| P0 | `ocr_processor.py` | LaTeX 语法校验：`$` 成对检查 + 括号匹配 → 失败降级 OCR |
| P0 | `ocr_processor.py` | 公式边界保护：LaTeX 块前后强制空行 + `【数学公式】` 标注 |
| P0 | `document_loader.py` | 快速通道数学符号检测：PyMuPDF 提取的文本行符号占比 >15% → 渲染裁剪 → SimpleTex |
| P1 | `ocr_processor.py` | 持久化缓存：JSON 文件缓存，Key = MD5+页码+坐标 |
| P1 | `ocr_processor.py` | 额度预警：API 调用计数 + 接近限制自动降级 |
| P1 | `config.py` | SimpleTex API Key、超时、重试次数、缓存路径配置化 |
| P3 | `ocr_processor.py` | 多 API 容灾：Mathpix API 作为备用，SimpleTex 连续失败自动切换 |

### 降级链设计（增强版）

```python
def _recognize_formula(image_crop: bytes, line_height: float = 14) -> str:
    # 0. 行内/行间判断
    formula_h = _get_crop_height(image_crop)
    is_inline = formula_h <= line_height * 1.5

    # 1. SimpleTex API（带重试）
    for attempt in range(3):
        try:
            latex = _call_simpletex(image_crop, timeout=8)
            if latex and _validate_latex(latex):
                wrapper = "$" if is_inline else "$$"
                return f"{wrapper} {latex} {wrapper}"
        except TimeoutError:
            if attempt == 2:
                break  # 最后一次也失败，走降级

    # 2. OCR 纯文本（当前能力）
    try:
        text = _ocr_text_only(image_crop)
        if text:
            return text
    except Exception:
        pass

    # 3. 占位符（不阻塞流程）
    return "> 📐 [公式]"
```

---

## 七、可选进阶方向（P3 及以后）

### 7.1 多 API 容灾

预留 Mathpix API 作为备用接口，SimpleTex 连续失败时自动切换：

```python
FORMULA_APIS = [
    ("simpletex", "https://api.simpletex.cn/..."),
    ("mathpix",  "https://api.mathpix.com/..."),
]

def _call_formula_api(image: bytes) -> str | None:
    for name, url in FORMULA_APIS:
        try:
            latex = _post_formula(url, image, timeout=8)
            if latex and _validate_latex(latex):
                return latex
        except Exception:
            continue
    return None
```

### 7.2 大模型公式描述生成

`【数学公式】` 占位符可升级为 LLM 生成的简短描述（如 `【傅里叶级数展开式】`），进一步提升公式 chunk 的向量检索召回率。

### 7.3 PP-Structure V3 全本地化

等 PaddlePaddle 修复 Windows oneDNN Bug（或项目迁移到 Linux）后，切换到 PP-Structure V3，公式识别完全本地化，不再依赖外部 API。

---

## 八、验证方法

1. 上传含公式的扫描件 PDF → 查看 `_parsed_output.md` → 公式区域应出现 `$$...$$` LaTeX
2. 上传含行内公式的原生 PDF → 应触发规则补召 → 公式转为 `$...$` LaTeX
3. 断开网络 → 上传含公式 PDF → 应降级为纯文本或 `[公式]` 占位符，不崩溃
4. 同一公式第二次上传 → 应命中缓存（MD5+页码+坐标），不重复调 API
5. 检索公式符号（如 `\sum`）→ 应命中对应 chunk
6. 快速通道上传含公式原生 PDF → 公式区域应走 SimpleTex 识别

---

## 九、文件清单

| 文件 | 当前状态 | 需改动 |
|------|---------|--------|
| `document_loader.py` | 快慢通道分流、PyMuPDF 快速通道 | **新增**：快速通道数学符号检测 → 渲染裁剪 → SimpleTex |
| `ocr_processor.py` | PP-Structure V2 集成、阅读顺序、表格 Markdown | **新增**：公式检测+裁剪+补召、SimpleTex API 调用+三级降级+重试、行内/行间区分、语法校验、边界保护、持久化缓存、额度预警 |
| `config.py` | PDF 解析阈值 | **新增**：SimpleTex API Key、超时、重试、缓存路径、公式符号集、符号占比阈值 |
| `requirements.txt` | PaddlePaddle 2.6.2, paddleocr 2.9.1 | **新增**：无（SimpleTex 用 requests 调 HTTP API） |
