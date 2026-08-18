"""Streamlit Web 界面 —— MCP 统一智能体的前端"""

import os
import time
import streamlit as st

from document_loader import load_file, PdfEncryptedError, ScannedPdfError
from text_splitter import split_documents
from vector_store import (
    build_vector_store, add_document, list_documents, get_status, remove_document,
)
from agent import Agent
from evaluation import EvaluationLogger
from token_tracker import get_tracker
import requests as _requests

# 权限后端地址：容器内走 api-server 服务名，本地开发默认 localhost:8000
API_SERVER_URL = os.getenv("API_SERVER_URL", "http://localhost:8000")

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

# ===== 侧边栏：文档管理 =====
with st.sidebar:
    st.header("📄 文档管理")

    # ── 构建模式选择 ──
    build_mode = st.radio(
        "构建模式",
        ["增量添加（追加）", "全量重建（清空旧库）"],
        help="增量添加：新文档追加到现有知识库，不动已有的；全量重建：清空旧库重新构建",
    )
    is_incremental = build_mode.startswith("增量")

    uploaded_file = st.file_uploader(
        "上传 PDF / Word / TXT 文件",
        type=["pdf", "docx", "txt"],
        help="支持上传多个文件",
        accept_multiple_files=True,
    )

    if uploaded_file:
        # ── 上传参数：知识库分组 + 可见性 ──
        col1, col2 = st.columns(2)
        with col1:
            kb_group = st.text_input("知识库分组", value="default",
                                     help="文档归属的知识库分组（如 dept_rd）")
        with col2:
            visibility = st.selectbox("可见性", ["internal", "public"],
                                      help="public=全员可见, internal=仅本组可见")

    if uploaded_file and st.button("🚀 执行", use_container_width=True):
        # 文档上传守卫（fail-closed）：未登录不允许上传，堵死「未授权写」口子
        if "auth_token" not in st.session_state:
            st.warning("🔒 请先在左侧「账户」区域登录后再上传文档。")
            st.stop()
        save_dir = "uploaded_docs"
        os.makedirs(save_dir, exist_ok=True)

        # 保存上传文件
        for uf in uploaded_file:
            file_path = os.path.join(save_dir, uf.name)
            with open(file_path, "wb") as f:
                f.write(uf.getbuffer())

        if is_incremental:
            # ── 增量添加 ──
            with st.spinner("正在增量添加文档..."):
                results = []
                for uf in uploaded_file:
                    file_path = os.path.join(save_dir, uf.name)
                    result = add_document(file_path, skip_duplicate=True,
                                        kb_group=kb_group, visibility=visibility)
                    results.append((uf.name, result))

                # 汇总结果
                success_count = 0
                for name, r in results:
                    if r.get("skipped"):
                        st.info(f"⏭ {name}: 已存在（路径+哈希一致），跳过")
                    elif r.get("error"):
                        st.warning(f"❌ {name}: {r['error']}")
                    else:
                        success_count += 1
                        st.success(f"✅ {name}: +{r['chunks_added']} chunks")

                if success_count > 0:
                    st.success(f"🎉 增量添加完成！共 {success_count} 个文档")
        else:
            # ── 全量重建 ──
            with st.spinner("正在处理文档..."):
                all_docs = []
                load_errors = []
                for uf in uploaded_file:
                    file_path = os.path.join(save_dir, uf.name)
                    try:
                        docs = load_file(file_path)
                        all_docs.extend(docs)
                    except PdfEncryptedError:
                        load_errors.append(f"🔒 {uf.name}: PDF 已加密，请解密后重新上传")
                    except ScannedPdfError:
                        load_errors.append(f"📷 {uf.name}: 检测为扫描件/图片PDF，建议先用 OCR 工具转换")
                    except Exception as e:
                        load_errors.append(f"❌ {uf.name}: {e}")

                if load_errors:
                    for err in load_errors:
                        st.warning(err)
                if all_docs:
                    st.info(f"已加载 {len(all_docs)} 个文档段落")

                    chunks = split_documents(all_docs)
                    st.info(f"已切分为 {len(chunks)} 个文本块")

                    build_vector_store(chunks)
                    st.success(f"✅ 知识库构建完成！共 {len(chunks)} 个文本块")
                elif not load_errors:
                    st.error("未能加载任何文档内容")

    # ── 权限控制（实用落地版：账号密码登录）──
    st.divider()
    st.subheader("🔒 账户")

    if "auth_token" not in st.session_state:
        # 未登录 → 登录表单
        username = st.text_input("用户名", key="login_user")
        password = st.text_input("密码", type="password", key="login_pass")
        if st.button("🚀 登录", use_container_width=True):
            try:
                resp = _requests.post(
                    f"{API_SERVER_URL}/api/auth/login",
                    json={"username": username, "password": password},
                    timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    st.session_state["auth_token"] = data["token"]
                    st.session_state["auth_user"] = data["user"]
                    kb_groups = data["user"].get("kb_groups", [])
                    if not kb_groups:
                        kb_groups = None
                    st.session_state["kb_groups"] = kb_groups  # 存会话态，chat 时注入
                    st.session_state["user_id"] = data["user"].get("username", "default")
                    st.session_state["permissions"] = data["user"].get("permissions", [])  # 工具权限
                    st.rerun()
                else:
                    st.error(f"登录失败: {resp.json().get('detail', '未知错误')}")
            except Exception as e:
                st.warning(f"无法连接权限服务: {e}")
    else:
        # 已登录 → 用户信息
        user = st.session_state["auth_user"]
        st.success(f"👤 {user['username']}")
        perms = user.get("permissions", [])
        if perms:
            st.caption(f"权限: {', '.join(perms)}")
        kb = user.get("kb_groups", [])
        if kb:
            st.caption(f"可访问知识库: {', '.join(kb)}")
        else:
            st.caption("可访问知识库: 全部（管理员）")
        if st.button("🚪 退出登录", use_container_width=True):
            for k in ["auth_token", "auth_user", "kb_groups", "user_id"]:
                st.session_state.pop(k, None)
            st.rerun()

    # ── 知识库状态 ──
    st.divider()
    st.subheader("📊 知识库状态")

    try:
        status = get_status()
        if status["ready"]:
            st.success(f"就绪 · {status['document_count']} 文档 · {status['total_chunks']} chunks")
            st.caption(f"模型: {status['embedding_model']} ({status['embedding_dim']}维)")

            # 文档清单
            with st.expander(f"📋 文档清单（{status['document_count']} 个）"):
                docs = list_documents()
                for d in docs:
                    hash_short = d['file_hash'][:10] + "..." if d['file_hash'] else "(无)"
                    st.caption(f"• {d['file_path']} — {d['chunks']} chunks")
                    if d.get('added_at'):
                        st.caption(f"  _{d['added_at']}_")
        else:
            st.warning("未构建（请先上传文档）")
    except Exception as e:
        st.warning(f"📊 知识库状态：检查中... ({e})")

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
    # 登录守卫（fail-closed）：未登录不允许检索，堵死越权口子
    if "auth_token" not in st.session_state:
        # 先渲染 user 消息，避免用户困惑「我打的话去哪了」
        with st.chat_message("user"):
            st.write(user_input)
        with st.chat_message("assistant"):
            st.warning("🔒 请先在左侧「账户」区域登录后再提问。未登录无法检索知识库。")
        st.stop()  # 停止执行，未登录输入不计入历史、不调用 Agent

    # 显示用户消息
    with st.chat_message("user"):
        st.write(user_input)
    st.session_state.history.append({"role": "user", "content": user_input})

    # 调用 Agent
    with st.chat_message("assistant"):
        start_time = time.time()

        try:
            # 统一走 agent.chat()，内部自动处理有/无知识库的情况
            # 从登录态注入请求级权限分组 + 用户身份（全程参数透传，无文件共享）
            kb_groups = st.session_state.get("kb_groups")
            user_id = st.session_state.get("user_id", "default")
            permissions = st.session_state.get("permissions")
            answer = st.session_state.agent.chat(user_input, kb_groups=kb_groups,
                                                 user_id=user_id, permissions=permissions)

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
