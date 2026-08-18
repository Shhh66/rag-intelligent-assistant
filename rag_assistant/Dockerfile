# RAG 智能助手应用镜像
# 主服务（Streamlit/MCP）与模型服务（嵌入/重排）、权限后端（api_server）
# 共用同一镜像，由 docker-compose.yml 用不同 command 启动不同服务。
#
# 构建：docker build -t rag-assistant .
# 模型缓存挂载 D 盘，避免占用 C 盘（见 docker-compose.yml volumes）

FROM python:3.12-slim

WORKDIR /app

# 系统运行时库（slim 镜像不带，需显式声明）：
#   libgomp1      → torch / onnxruntime 的 OpenMP 运行时（主链路必需）
#   libglib2.0-0  → 常见 C 运行时依赖
#   tini          → 正确 PID 1：回收孤儿子进程（mcp_server stdio）+ 转发 SIGTERM
# 注：不装 libgl1（OpenGL）——那是 opencv/paddle 才需要的，二者不在 requirements 主链路，
#     装了反而引入一个 virtual package 的不确定点，可能 build 失败。
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 libglib2.0-0 tini \
    && rm -rf /var/lib/apt/lists/*

# 先复制 requirements 并安装依赖，利用 Docker 构建缓存层（改代码不重装依赖）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目代码（.dockerignore 已排除 venv/chroma_db/缓存等大文件）
COPY . .

# HF 镜像 + 模型缓存目录（volume 挂载宿主机 D 盘）
# HF_HOME 指向挂载点，模型只下载一次，多实例共享
ENV HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/root/.cache/huggingface \
    PYTHONUNBUFFERED=1

# tini 作为 PID 1，正确转发信号、回收孤儿子进程
ENTRYPOINT ["tini", "--"]

# 默认启动（docker-compose 用 command 覆盖为不同服务）
CMD ["python", "mcp_server.py"]
