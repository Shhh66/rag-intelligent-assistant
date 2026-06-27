"""Streamlit Web 界面 —— MCP 统一智能体的前端"""

import os
import time
import streamlit as st

from document_loader import load_file
from text_splitter import split_documents
from vector_store import build_vector_store
from agent import Agent
from evaluation import EvaluationLogger
from token_tracker import get_tracker

# ===== 页面设置 =====
st.set_page_config(
    page_title="MCP 统一智能体",
    page_icon="🤖",
    layout="wide",
)

st.title("🤖 MCP 统一智能体")
st.caption("LLM 自主决策 · MCP 工具调度 · RAG 知识库问答 | 支持 PDF / Word / TXT")

# ===== 初始化会话状态 =====
if "agent" not in st.session_state:
    st.session_state.agent = Agent()
if "logger" not in st.session_state:
    st.session_state.logger = EvaluationLogger()
if "history" not in st.session_state:
    st.session_state.history = []

# ===== 侧边栏：文档上传 =====
with st.sidebar:
    st.header("📄 文档上传")
    uploaded_file = st.file_uploader(
        "上传 PDF / Word / TXT 文件",
        type=["pdf", "docx", "txt"],
        help="支持上传多个文件",
        accept_multiple_files=True,
    )

    if uploaded_file and st.button("🚀 构建知识库", use_container_width=True):
        all_docs = []
        save_dir = "uploaded_docs"
        os.makedirs(save_dir, exist_ok=True)

        for uf in uploaded_file:
            file_path = os.path.join(save_dir, uf.name)
            with open(file_path, "wb") as f:
                f.write(uf.getbuffer())

        with st.spinner("正在处理文档..."):
            for uf in uploaded_file:
                file_path = os.path.join(save_dir, uf.name)
                docs = load_file(file_path)
                all_docs.extend(docs)

            st.info(f"已加载 {len(all_docs)} 个文档段落")

            chunks = split_documents(all_docs)
            st.info(f"已切分为 {len(chunks)} 个文本块")

            build_vector_store(chunks)
            st.success(f"✅ 知识库构建完成！共 {len(chunks)} 个文本块")

    # 显示状态
    db_exists = os.path.exists("chroma_db") and len(os.listdir("chroma_db")) > 0
    if db_exists:
        st.success("📊 知识库状态：已就绪")
    else:
        st.warning("📊 知识库状态：未构建（请先上传文档）")

    # Token 用量展示
    st.divider()
    st.header("💰 Token 用量")
    tracker = get_tracker()

    # ── 层次1：本次问答 ──
    conv = tracker.get_conversation_diff()
    if conv["call_count"] > 0:
        st.subheader("📝 本次问答")
        st.caption(
            f"Token: {conv['total_tokens']:,} "
            f"(入 {conv['total_input']:,} / 出 {conv['total_output']:,})"
        )
        st.caption(f"调用 {conv['call_count']} 次 · ¥{conv['total_cost']:.4f}")

    # ── 层次2：本次会话累计 ──
    sess = tracker.get_session_summary()
    if sess["call_count"] > 0:
        st.subheader("📊 本次会话累计")
        st.metric("LLM 调用次数", sess["call_count"])
        st.metric("总 Token", f"{sess['total_tokens']:,}")
        st.metric("预估费用", f"¥{sess['total_cost']:.4f}")

    # ── 层次3：历史总计 ──
    hist = tracker.get_all_time_summary()
    if hist["call_count"] > 0:
        st.subheader("📈 历史总计")
        st.caption(f"共 {hist['call_count']} 次调用")
        st.caption(f"累计 Token: {hist['total_tokens']:,}")
        st.caption(f"累计费用: ¥{hist['total_cost']:.4f}")
    else:
        st.caption("暂无 LLM 调用记录")

    # 清空对话按钮
    st.divider()
    if st.button("🗑 清空对话历史", use_container_width=True):
        st.session_state.agent.clear_memory()
        st.session_state.history = []
        st.rerun()

# ===== 主区域：对话界面 =====
# 显示历史消息
for msg in st.session_state.history:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

# 输入框
user_input = st.chat_input("输入你的问题...")

if user_input:
    # 显示用户消息
    with st.chat_message("user"):
        st.write(user_input)
    st.session_state.history.append({"role": "user", "content": user_input})

    # 调用 Agent
    with st.chat_message("assistant"):
        start_time = time.time()

        try:
            # 统一走 agent.chat()，内部自动处理有/无知识库的情况
            answer = st.session_state.agent.chat(user_input)

            # 显示回答
            st.write(answer)

            # 显示本次问答的 Token 用量
            tracker = get_tracker()
            conv = tracker.get_conversation_diff()
            if conv["call_count"] > 0:
                st.caption(
                    f"📊 Token: {conv['total_tokens']:,} "
                    f"(入 {conv['total_input']:,} / 出 {conv['total_output']:,}) "
                    f"· 调用 {conv['call_count']} 次 "
                    f"· 💰 ¥{conv['total_cost']:.4f}"
                )

            # 记录日志
            latency = (time.time() - start_time) * 1000
            st.session_state.logger.log(
                user_input=user_input,
                answer=answer,
                intent="knowledge",
                top_docs=[],
                latency_ms=latency,
                token_usage=conv,
            )

        except Exception as e:
            st.error(f"出错了: {str(e)}")
            answer = f"抱歉，处理时出现错误: {str(e)}"

    st.session_state.history.append({"role": "assistant", "content": answer})
    # 立即刷新页面，让侧边栏的 Token 统计同步更新
    st.rerun()
