import os
from pathlib import Path
from dotenv import load_dotenv

# 从 config.py 所在目录加载 .env（兼容任意工作目录启动，如 MCP Inspector）
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

# ===== DeepSeek API 配置 =====
# API Key 存放在 .env 文件中，不提交到 Git
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")

GROQ_BASE_URL = "https://api.deepseek.com"

# LLM 模型名称
LLM_MODEL = "deepseek-v4-flash"
       

# ===== HuggingFace 镜像配置 =====
# 国内访问 huggingface.co 不稳定，使用 hf-mirror.com 镜像下载模型
HF_ENDPOINT = "https://hf-mirror.com"

# ===== 向量嵌入配置 =====
# 使用多语言模型支持中英文跨语言检索（中文问题→英文文档也能匹配）
EMBEDDING_MODEL = "paraphrase-multilingual-MiniLM-L12-v2"  # 多语言，420MB，支持50+语言

# ===== 向量库配置 =====
VECTOR_DB_PATH = "./chroma_db"     # ChromaDB 存储路径

# ===== 分块配置 =====
CHUNK_SIZE = 500                   # 每个文本块最多 500 字
CHUNK_OVERLAP = 50                 # 相邻块之间重叠 50 字

# ===== 对话记忆配置 =====
MAX_MEMORY_ROUNDS = 10             # 最多记住 10 轮对话

# ===== 检索配置 =====
TOP_K = 8                          # 每次检索返回 8 个最相关片段（需覆盖同名Section场景）

# ===== MCP 统一智能体配置 =====
MCP_MAX_TURNS = 5                  # 最大工具调用轮次（防死循环）
MCP_TOOL_TOP_N = 5                 # 向量预筛选工具的 Top-N 数量
MCP_CALL_TIMEOUT = 60.0            # 单次工具调用超时（秒）
MCP_REFLECTION_MAX = 50            # 反思记忆最大条数
MCP_HEARTBEAT_INTERVAL = 30.0      # MCP 心跳间隔（秒）

# ===== Token 计费配置 =====
# 单位：¥ / 1M tokens（DeepSeek 官方定价 2025）
# deepseek-chat:      ¥1  input,  ¥2  output
# deepseek-v4-flash:  ¥1  input,  ¥2  output（采用 deepseek-chat 同价）
# deepseek-reasoner:  ¥4  input, ¥16  output
MODEL_PRICING = {
    "deepseek-chat":       {"input": 1.0,  "output": 2.0},
    "deepseek-v4-flash":   {"input": 1.0,  "output": 2.0},
    "deepseek-reasoner":   {"input": 4.0,  "output": 16.0},
}
# 未在 MODEL_PRICING 中配置的模型使用此默认定价
DEFAULT_PRICING = {"input": 1.0, "output": 2.0}

# 上下文窗口 Token 上限（DeepSeek V4 系列为 128K）
MAX_CONTEXT_TOKENS = 128000
CONTEXT_WARNING_RATIO = 0.8  # Prompt Token 达到窗口 80% 时发出告警

# ===== 文档解析配置 =====
# 文本质量校验阈值
PDF_GARBAGE_RATIO = 0.02          # 乱码率 > 2% → 判定劣质文本层
PDF_MIN_SENTENCE_LEN = 5          # 平均句长 < 5 字符 → 判定语序混乱
PDF_MAX_NEWLINE_RATIO = 0.4       # 换行占比 > 40% → 判定文本稀疏
PDF_REPEAT_GARBAGE_RATIO = 0.05   # 连续重复异常字符 > 5% → 直接降级

# 多栏检测
PDF_COLUMN_STRIPS = 20            # 竖条数量
PDF_COLUMN_PEAK_MIN_RATIO = 0.3   # 峰值区域文本量最低占比
PDF_COLUMN_GAP_MAX_RATIO = 0.05   # 峰间空白区域文本量最高占比

# 页眉页脚
PDF_HEADER_FOOTER_MARGIN = 0.05   # 顶部/底部 5% 标记为候选
PDF_HEADER_FOOTER_REPEAT_PAGES = 3  # 连续 ≥3 页重复 → 判定页眉页脚
PDF_HEADER_FOOTER_SIMILARITY = 0.8  # 文本相似度 > 80% → 判定为重复

# 标题聚类
PDF_TITLE_CLUSTERS = 4            # 预设标题级数（H1-H3 + 正文）
PDF_TITLE_SIZE_MERGE = 1.0        # 相邻字号差 < 1pt 合并为同一级

# ===== Markdown 分块配置 =====
CHUNK_MERGE_RATIO = 1 / 3         # 章节长度 < chunk_size 的 1/3 → 合并到相邻同级章节
SPLIT_HEADERS = [
    ("#", "h1"), ("##", "h2"), ("###", "h3"),
]

# ===== 公式识别配置 =====
# 多 API 容灾链：百度智能云（免费额度）→ SimpleTex（备选）
# 百度智能云・数学公式识别（个人实名后免费，每日数百次）
BAIDU_OCR_API_KEY = "jxxUSajOCsqmJuKJFsWRKvoO"              # 百度智能云 API Key
BAIDU_OCR_SECRET_KEY = "3FesrjvUPU9xweRtk8aSYunEbiCIuXch"           # 百度智能云 Secret Key
BAIDU_OCR_FORMULA_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/formula"
BAIDU_OCR_GENERAL_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"  # 通用文字识别
BAIDU_OCR_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
# SimpleTex API（备选，免费 1000 次/月）
SIMPLETEX_API_URL = "https://api.simpletex.cn/api/v1/ocr"
SIMPLETEX_API_KEY = ""              # 在 https://simpletex.cn 注册获取

FORMULA_TIMEOUT = 8                 # 单次请求超时（秒）
FORMULA_MAX_RETRIES = 3             # 最大重试次数
FORMULA_CACHE_PATH = "./formula_cache.json"  # 公式结果缓存文件

# 公式检测阈值
FORMULA_SYMBOL_RATIO = 0.15          # 文本行数学符号占比 > 15% → 疑似公式
FORMULA_CROP_PADDING = 15            # 公式区域裁剪外扩像素
FORMULA_INLINE_HEIGHT_RATIO = 1.5    # 公式高 / 正文行高 < 1.5 → 行内公式

# 数学符号特征集
MATH_SYMBOLS = {
    '+', '-', '=', '×', '÷', '±', '·',
    '∑', '∫', '∏', '∂', '∞', '√', '∝',
    'α', 'β', 'γ', 'δ', 'ε', 'θ', 'λ', 'μ', 'π', 'σ', 'φ', 'ω',
    '≤', '≥', '≠', '≈', '≡', '∈', '⊂', '⊆',
    '→', '←', '⇒', '⇔',
    '∀', '∃', '∧', '∨', '¬',
    '¹', '²', '³', '₀', '₁', '₂', '₃',
    '⟨', '⟩', '⌊', '⌋', '⌈', '⌉',
}  # 用于判断文本行是否疑似公式
