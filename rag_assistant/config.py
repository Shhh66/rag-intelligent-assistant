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
MCP_CALL_TIMEOUT = 180.0           # 单次工具调用超时（秒），首次查询含模型加载+检索+翻译+重排+LLM
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

# ===== 向量库增量管理配置 =====
KB_META_FILE = "./chroma_db/db_meta.json"       # 轻量元数据索引文件
KB_LOCK_FILE = "./chroma_db/kb.lock"            # 全局文件锁路径
KB_SNAPSHOT_DIR = "./chroma_db/snapshots"       # 操作快照目录
SNAPSHOT_MAX_COUNT = 10                          # 最多保留快照数
SOFT_DELETE = False                              # 默认物理删除，True 时标记 is_deleted

# ===== 重排配置 =====
RERANK_ENABLED = True                       # 是否启用重排
RERANK_STRATEGY = "cross-encoder"           # "cross-encoder" | "llm" | "none"
RERANK_TOP_K = 5                            # 重排后送入 Prompt 的片段数（固定条数兜底）
RERANK_MAX_CANDIDATES = 16                  # 重排候选上限（超过则截断）
RERANK_MAX_TOKENS = 2000                    # 动态截断：送入 Prompt 的片段总 Token 上限
RERANK_MIN_SCORE = -999.0                   # 低分过滤阈值（-999=不过滤，设为 -2.0~2.0 启用过滤，BGE logits 通常 -5~5）
RERANK_MODEL = "BAAI/bge-reranker-v2-m3"    # Cross-Encoder 模型名（多语言，568MB）
RERANK_CALIBRATION_MAX_RATIO = 1.2          # 双语校准最大补偿倍数（防止过度补偿）

# ===== 权限控制配置 =====
KB_DEFAULT_GROUP = "default"                 # 默认知识库分组
KB_DEFAULT_VISIBILITY = "internal"           # 默认可见性（public | internal）
KB_PERMISSION_DB = "./permission.db"         # SQLite 权限数据库路径
KB_PERMISSION_SECRET_KEY = os.getenv("KB_PERMISSION_SECRET_KEY", "rag-kb-secret-change-in-production")  # JWT 签名密钥（生产环境用 .env 覆盖）
KB_PERMISSION_TOKEN_EXPIRE_HOURS = 24        # JWT Token 过期时间（小时）

# ===== 模型服务化配置（部署运维改造）=====
# 嵌入/重排模型独立服务地址。留空 = 内嵌进程加载（默认，兼容旧行为）。
# 设置后走 HTTP 调用，支持多实例共享模型、独立扩缩容、模型升级不停服务。
# 支持环境变量覆盖（容器化时由 docker-compose environment 注入）。
# 例：EMBED_SERVER_URL = "http://localhost:8001"
EMBED_SERVER_URL = os.getenv("EMBED_SERVER_URL", "")   # 嵌入模型服务地址（留空=本地加载）
RERANK_SERVER_URL = os.getenv("RERANK_SERVER_URL", "") # 重排模型服务地址（留空=本地加载）
EMBED_SERVER_TIMEOUT = 60.0                  # 嵌入服务单次请求超时（秒，含首次冷启动）
RERANK_SERVER_TIMEOUT = 60.0                 # 重排服务单次请求超时（秒）

# ===== 工具调用熔断器配置（CLOSED/OPEN/HALF_OPEN 状态机）=====
CB_ENABLED = True                            # 熔断总开关
CB_FAILURE_THRESHOLD = 5                     # 连续失败 N 次打开熔断
CB_COOLDOWN_SECONDS = 30                     # 熔断冷却时间（秒），之后放行 1 次试探

# ===== 检索结果缓存配置（部署运维改造 P2）=====
SEARCH_CACHE_ENABLED = True                  # 检索缓存总开关
SEARCH_CACHE_TTL = 600                       # 缓存 TTL（秒），检索结果时效性
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")   # Redis 地址（分布式缓存）
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))   # Redis 端口

# ===== 向量库服务化配置（部署运维改造 P2）=====
# 空 = PersistentClient（本地目录，单机）；非空 = HttpClient（client-server，多实例共享）
# 例：CHROMA_SERVER_URL = "http://localhost:8100"  # ⚠️ 用 8100 避开 api-server 占用的 8000（见 技术文档/启动指南.md）
CHROMA_SERVER_URL = os.getenv("CHROMA_SERVER_URL", "")

# ===== 限流配置（部署运维改造 P3）=====
RATE_LIMIT_ENABLED = True
# 用户请求级（api_server）：每个登录用户限流（防滥用/控成本）
USER_RATE_LIMIT_CAPACITY = 60     # 令牌桶容量（允许突发 60 次）
USER_RATE_LIMIT_REFILL = 1.0      # 每秒补充令牌数（= 60 次/分钟）
# LLM 调用级（call_llm_with_cb）：对 DeepSeek 限流（配合供应商 RPM）
LLM_RATE_LIMIT_CAPACITY = 30      # 令牌桶容量（允许突发 30 次）
LLM_RATE_LIMIT_REFILL = 0.5       # 每秒补充令牌数（= 30 次/分钟）

# ===== 工具鉴权配置（P4）=====
TOOL_PERMISSION_ENABLED = True    # 工具级权限总开关（默认开；启动期校验兜底防漏配）

# ===== Markdown 分块配置 =====
CHUNK_MERGE_RATIO = 1 / 3         # 章节长度 < chunk_size 的 1/3 → 合并到相邻同级章节
SPLIT_HEADERS = [
    ("#", "h1"), ("##", "h2"), ("###", "h3"),
]

# ===== 公式识别配置 =====
# 多 API 容灾链：百度智能云（免费额度）→ SimpleTex（备选）
# 百度智能云・数学公式识别（个人实名后免费，每日数百次）
# 密钥统一从 .env 读取，不硬编码到源码
BAIDU_OCR_API_KEY = os.getenv("BAIDU_OCR_API_KEY", "")              # 百度智能云 API Key
BAIDU_OCR_SECRET_KEY = os.getenv("BAIDU_OCR_SECRET_KEY", "")        # 百度智能云 Secret Key
BAIDU_OCR_FORMULA_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/formula"
BAIDU_OCR_GENERAL_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"  # 通用文字识别
BAIDU_OCR_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
# SimpleTex API（备选，免费 1000 次/月）
SIMPLETEX_API_URL = "https://api.simpletex.cn/api/v1/ocr"
SIMPLETEX_API_KEY = os.getenv("SIMPLETEX_API_KEY", "")             # 在 https://simpletex.cn 注册获取

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

# ===== 混合检索配置（BM25 + 向量 双通道 + RRF 融合）=====
HYBRID_ENABLED = True                     # 混合检索总开关（False=退回纯向量）
BM25_TOP_K = 10                           # BM25 单路召回数
VECTOR_TOP_K = 10                         # 向量单路召回数
RRF_K = 60                                # RRF 融合常数（越大越平滑，经验值 60）
BM25_RRF_WEIGHT = 1.0                     # BM25 路 RRF 权重（预留，默认对等）
VECTOR_RRF_WEIGHT = 1.0                   # 向量路 RRF 权重（预留，默认对等）
BM25_INDEX_PATH = "./bm25_index.pkl"      # BM25 索引持久化文件
BM25_STOPWORDS_PATH = "./bm25_stopwords.txt"  # 中文停用词表（可选，不存在则不过滤）

# ===== 查询改写配置（Query Rewrite）=====
QUERY_REWRITE_ENABLED = True              # 查询改写总开关
QUERY_REWRITE_MODE = "clarify"            # clarify（规范化+指代消解）| multi（多查询）| hyde
QUERY_REWRITE_MULTI_N = 3                 # multi 模式生成的 query 数
QUERY_REWRITE_MAX_TOKENS = 512            # 改写输出上限（deepseek-v4-flash 是推理模型，需给 reasoning 留足空间，否则 content 为空）
QUERY_REWRITE_CACHE_SIZE = 100            # 改写结果 LRU 缓存条数（0=关闭）
QUERY_REWRITE_COREF_ROUNDS = 3            # 指代消解引用的最近对话轮次

# ===== MCP 工具调用统一重试（tenacity 指数退避）=====
TOOL_RETRY_ENABLED = True                 # 工具调用重试总开关
TOOL_RETRY_MAX = 3                        # 最大尝试次数（含首次）
TOOL_RETRY_BACKOFF_BASE = 0.5             # 指数退避基数（秒）
TOOL_RETRY_MAX_WAIT = 8.0                 # 单次最大等待（秒）

# ===== 工具调用审计（结构化落盘 + trace_id）=====
TOOL_AUDIT_ENABLED = True                 # 审计开关
TOOL_AUDIT_PATH = "./tool_audit.jsonl"    # 审计日志文件（实时追加）
TOOL_AUDIT_ARG_MAXLEN = 500               # 单个入参值截断长度（脱敏+防膨胀）
TOOL_AUDIT_SENSITIVE_KEYS = ["api_key", "secret", "token", "password"]  # 掩码字段

# ===== 可观测性（LangFuse 自托管）=====
LANGFUSE_ENABLED = False                  # 总开关（默认关，需先起 LangFuse 服务再开）
# 密钥走 .env：LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST", "http://localhost:3000")

# ===== 长期记忆（跨会话实体记忆与检索）=====
LONG_TERM_MEMORY_ENABLED = True           # 长期记忆总开关
MEMORY_DB_PATH = "./memory.db"            # SQLite 结构化存储（实体事实）
MEMORY_COLLECTION = "memory"              # Chroma collection（独立于 langchain 主库）
MEMORY_RETRIEVE_TOP_K = 3                 # 每轮注入的记忆条数
MEMORY_EXTRACT_ENABLED = True             # 实体抽取开关（False=仅存对话摘要，即 V0.5）
MEMORY_EXTRACT_MAX_TOKENS = 512           # 抽取 LLM max_tokens（推理模型需给足，否则 content 空）
MEMORY_DECAY_DAYS = 90                    # 记忆时间衰减：超过此天数显著降权
MEMORY_DEDUP_SIM = 0.85                   # 抽取去重相似度阈值（>此值视为同一记忆，更新而非新增）
# 记忆类型权重（注入排序用）：用户画像 > 项目实体 > 历史结论
MEMORY_TYPE_WEIGHTS = {"profile": 1.0, "entity": 0.7, "conclusion": 0.4}

# ===== 河海大学教务系统 =====
EDU_STUDENT_ID = os.getenv("EDU_STUDENT_ID", "")
EDU_PASSWORD = os.getenv("EDU_PASSWORD", "")
