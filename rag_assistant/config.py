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
