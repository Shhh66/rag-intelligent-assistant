"""RAGAS 适配器 —— 把项目的 DeepSeek LLM 与 HF 本地嵌入包装成 RAGAS 可用对象。

设计要点：
- LLM：复用项目 DeepSeek OpenAI 兼容端点(GROQ_API_KEY / GROQ_BASE_URL / LLM_MODEL)，
        用 temperature=0 保证 LLM-as-a-judge 的相对分数稳定，适合 A/B 对比。
- 嵌入：复用 vector_store.get_embeddings() 的同一个 HF 本地模型，
        保证评测口径与线上检索一致，且无需 OpenAI Key。
"""

import sys
import os

# 允许从 rag_eval/ 子目录导入父目录的项目模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import GROQ_API_KEY, GROQ_BASE_URL, LLM_MODEL


def get_ragas_llm():
    """返回 RAGAS 可用的 LLM(DeepSeek，temperature=0)。"""
    from langchain_openai import ChatOpenAI
    from ragas.llms import LangchainLLMWrapper

    chat = ChatOpenAI(
        model=LLM_MODEL,
        api_key=GROQ_API_KEY,
        base_url=GROQ_BASE_URL,
        temperature=0,          # 固定 0，保证打分相对稳定
        timeout=60.0,
        max_retries=2,
    )
    wrapper = LangchainLLMWrapper(chat)
    # DeepSeek API 不支持 n>1（RAGAS 部分指标如 answer_relevancy 默认多候选采样），
    # bypass_n=True 让 wrapper 改为发多次 n=1 请求，规避 "Invalid n value" 400 错误。
    wrapper.bypass_n = True
    return wrapper


def get_ragas_embeddings():
    """返回 RAGAS 可用的嵌入(复用项目 HF 本地模型)。"""
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from vector_store import get_embeddings

    return LangchainEmbeddingsWrapper(get_embeddings())


# ===== 自测 =====
if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    print("=== RAGAS 适配器自测 ===")
    llm = get_ragas_llm()
    print(f"✅ LLM 包装成功: {type(llm).__name__} (model={LLM_MODEL})")
    emb = get_ragas_embeddings()
    print(f"✅ 嵌入包装成功: {type(emb).__name__}")
    print("🎉 适配器自测通过")
