# RAG 智能助手应用镜像
# 主服务（Streamlit/MCP）与模型服务（嵌入/重排）、权限后端（api_server）
# 共用同一镜像，由 docker-compose.yml 用不同 command 启动不同服务。
#
# 构建：docker build -t rag-assistant .
# 模型缓存挂载 D 盘，避免占用 C 盘（见 docker-compose.yml volumes）

# ═══════════════════════════════════════════════════════════
# 阶段 1：构建 Vue3 前端
# ═══════════════════════════════════════════════════════════
FROM node:20-slim AS frontend-builder

WORKDIR /frontend

# 先复制 package.json 和 lock 文件（利用 Docker 缓存层）
COPY frontend/package.json frontend/package-lock.json* ./

# 安装依赖
RUN npm ci --prefer-offline || npm install

# 复制前端源码并构建
COPY frontend/ .
RUN npm run build

# ═══════════════════════════════════════════════════════════
# 阶段 2：Python 运行时
# ═══════════════════════════════════════════════════════════
FROM python:3.12-slim

WORKDIR /app

# 系统运行时库（slim 镜像不带，需显式声明）：
#   libgomp1      → torch / onnxruntime 的 OpenMP 运行时（主链路必需）
#   libglib2.0-0  → 常见 C 运行时依赖
#   libgl1        → OpenCV（paddleocr 的传递依赖 opencv-python 需要），
#                   缺它会 `ImportError: libGL.so.1: cannot open shared object file`
#   tini          → 正确 PID 1：回收孤儿子进程（mcp_server stdio）+ 转发 SIGTERM
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 libgl1 tini \
    && rm -rf /var/lib/apt/lists/*

# 先复制 requirements 并安装依赖，利用 Docker 构建缓存层（改代码不重装依赖）
COPY requirements.txt .
# PyPI 源可覆盖：国内构建建议传 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
# 不传时走默认官方源，行为与改造前一致
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt

# 复制项目代码（.dockerignore 已排除 venv/chroma_db/缓存等大文件）
COPY . .

# 从阶段 1 复制前端构建产物到 api_server 静态文件目录
COPY --from=frontend-builder /frontend/dist ./frontend/dist

# HF 镜像 + 模型缓存目录（volume 挂载宿主机 D 盘）
# HF_HOME 指向挂载点，模型只下载一次，多实例共享
ENV HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/root/.cache/huggingface \
    PYTHONUNBUFFERED=1

# tini 作为 PID 1，正确转发信号、回收孤儿子进程
ENTRYPOINT ["tini", "--"]

# 默认启动（docker-compose 用 command 覆盖为不同服务）
CMD ["python", "mcp_server.py"]
