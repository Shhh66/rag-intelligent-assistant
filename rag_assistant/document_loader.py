"""文档加载模块 —— 支持 PDF、Word、TXT 文件"""

import os
import sys
from typing import List

from langchain_core.documents import Document


def _load_txt(file_path: str) -> List[Document]:
    """加载纯文本文件"""
    with open(file_path, "r", encoding="utf-8") as f:
        text = f.read()
    return [Document(page_content=text, metadata={"source": file_path})]


def _load_docx(file_path: str) -> List[Document]:
    """加载 Word 文件（用轻量 docx2txt，避免拉入 torchvision 等重依赖）"""
    import docx2txt
    text = docx2txt.process(file_path)
    return [Document(page_content=text, metadata={"source": file_path})]


def _load_pdf(file_path: str) -> List[Document]:
    """加载 PDF 文件"""
    from langchain_community.document_loaders import PyPDFLoader
    return PyPDFLoader(file_path).load()


def load_file(file_path: str) -> List[Document]:
    """根据文件类型，加载单个文件并返回文档列表"""
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".pdf":
        docs = _load_pdf(file_path)
    elif ext == ".docx":
        docs = _load_docx(file_path)
    elif ext == ".txt":
        docs = _load_txt(file_path)
    else:
        raise ValueError(f"不支持的文件类型: {ext}（仅支持 PDF、DOCX、TXT）")

    print(f"   ✅ 已加载: {os.path.basename(file_path)}（{len(docs)} 页/段）", file=sys.stderr, flush=True)
    return docs



