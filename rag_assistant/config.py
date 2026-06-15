import os

# ===== DeepSeek API 配置 =====
# 填入你的 DeepSeek API Key（从 https://platform.deepseek.com/api_keys 获取）
DEEPSEEK_API_KEY = "sk-bcee7a003b4c4a9e986565fe873ce13b"
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# LLM 模型名称
LLM_MODEL = "deepseek-chat"         # DeepSeek 对话模型，便宜好用

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
